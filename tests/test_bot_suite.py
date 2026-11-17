from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import edge_tts
import pytest

# Set dummy env before importing config/bot
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_VALUE")
os.environ.setdefault("OWNER_ID", "999888777")

from modules.textvoice.audio import (
    JobCancelled,
    MAX_CHARS,
    MAX_UTF8_BYTES,
    generate_audio_async,
    split_text,
)
from modules.textvoice.epub import Chapter, extract_book, extract_epub, extract_txt
from modules.textvoice.handler import (
    _build_book_text,
    _parse_range,
    _safe_name,
    _starts_with_heading,
)
from modules.textvoice.tts import (
    LANGUAGES,
    _format_pitch,
    _format_rate,
    has_speakable_content,
    sanitize_tts_text,
    synthesize_with_retry,
)


def _create_sample_epub3(path: Path) -> Path:
    """Create a realistic EPUB3 file with <header><h1> titles, nav.xhtml TOC, and percent-encoded paths."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles>
                <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
              </rootfiles>
            </container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
              <manifest>
                <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
                <item id="ch1" href="Text/ch%201.xhtml" media-type="application/xhtml+xml"/>
                <item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine>
                <itemref idref="nav"/>
                <itemref idref="ch1"/>
                <itemref idref="ch2"/>
              </spine>
            </package>""",
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
              <body>
                <nav epub:type="toc">
                  <ol>
                    <li><a href="Text/ch%201.xhtml">Chapter 1: Dawn</a></li>
                    <li><a href="Text/ch2.xhtml">Chapter 2: Horizon</a></li>
                  </ol>
                </nav>
              </body>
            </html>""",
        )
        # Chapter 1 wraps <h1> inside <header> (EPUB3 pattern that previously lost its title!)
        zf.writestr(
            "OEBPS/Text/ch 1.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <html xmlns="http://www.w3.org/1999/xhtml">
              <head><title>Unknown</title></head>
              <body>
                <header><h1>Chapter 1: Dawn</h1></header>
                <p>The morning <em>sun</em> rose slowly over the quiet mountains.</p>
              </body>
            </html>""",
        )
        # Chapter 2 has no <h1> tag inside body — title must come from nav.xhtml TOC!
        zf.writestr(
            "OEBPS/Text/ch2.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <html xmlns="http://www.w3.org/1999/xhtml">
              <head><title>Untitled</title></head>
              <body>
                <p>Short prologue line.</p>
              </body>
            </html>""",
        )
    return path


def test_epub3_header_and_nav_toc_extraction(tmp_path: Path):
    epub_path = _create_sample_epub3(tmp_path / "novel.epub")
    chapters = extract_epub(epub_path)

    # nav.xhtml must NOT be spoken as Chapter 1
    assert len(chapters) == 2
    assert chapters[0].title == "Chapter 1: Dawn"
    assert "morning sun rose" in chapters[0].text
    assert chapters[1].title == "Chapter 2: Horizon"
    assert chapters[1].text == "Short prologue line."


def test_txt_hindi_and_english_chapters(tmp_path: Path):
    hindi_txt = tmp_path / "hindi_novel.txt"
    hindi_txt.write_text(
        "यह उपन्यास की भूमिका है।\n\n"
        "अध्याय 1: नई शुरुआत\n"
        "राम ने खिड़की से बाहर देखा। बारिश हो रही थी।\n\n"
        "अध्याय 2: रहस्य\n"
        "अचानक दरवाजे पर दस्तक हुई।\n",
        encoding="utf-8",
    )
    chapters = extract_txt(hindi_txt)
    assert len(chapters) == 3
    assert chapters[0].title == "Introduction"
    assert chapters[1].title == "अध्याय 1: नई शुरुआत"
    assert chapters[2].title == "अध्याय 2: रहस्य"


def test_split_text_hindi_utf8_byte_budget_and_dividers():
    # 2000 Hindi chars = ~5500 UTF-8 bytes (>3900 limit), must split cleanly at danda (।)
    sentence = "यह एक बहुत ही रोचक हिंदी वाक्य है जो परीक्षा के लिए लिखा गया है। "
    long_hindi = "***\n\n" + (sentence * 45) + "\n\n---\n\n***"

    chunks = split_text(long_hindi)
    assert len(chunks) >= 2
    for ch in chunks:
        assert len(ch) <= MAX_CHARS
        assert len(ch.encode("utf-8")) <= MAX_UTF8_BYTES
        assert has_speakable_content(ch)
        assert "***" not in ch


def test_duplicate_heading_suppression():
    ch = Chapter(
        number=1,
        title="Chapter 1: Dawn",
        text="Chapter 1 - Dawn\n\nThe morning sun rose slowly.",
    )
    assert _starts_with_heading(ch.text, ch.title)
    built = _build_book_text([ch])
    assert built.count("Dawn") == 1


def test_range_parser():
    assert _parse_range("all", 12) == (1, 12)
    assert _parse_range("3", 12) == (3, 3)
    assert _parse_range("2 - 7", 12) == (2, 7)
    assert _parse_range("2–7", 12) == (2, 7)
    assert _parse_range("2 to 5", 12) == (2, 5)
    with pytest.raises(ValueError):
        _parse_range("8-3", 12)
    with pytest.raises(ValueError):
        _parse_range("1-20", 12)


def test_rate_and_pitch_formatting():
    assert _format_rate(0) == "+0%"
    assert _format_rate(-15) == "-15%"
    assert _format_rate("+10%") == "+10%"
    assert _format_pitch(-2) == "-2Hz"
    assert _format_pitch("+1Hz") == "+1Hz"
    assert sanitize_tts_text("Hello\x00World\x07!") == "Hello World !"
    assert _safe_name("../../My Book: Vol. 1?.epub") == "My_Book_Vol_1epub"


@pytest.mark.asyncio
async def test_all_configured_voices_exist_on_edge_tts():
    """Verify 100% of voices in LANGUAGES exist on Microsoft's live Edge TTS service."""
    live_voices = await edge_tts.list_voices()
    live_names = {v["ShortName"] for v in live_voices}

    missing = []
    for lang_code, info in LANGUAGES.items():
        for voice_id in info["voices"]:
            if voice_id not in live_names:
                missing.append((lang_code, voice_id))

    assert not missing, f"Configured voices missing from Edge TTS: {missing}"


@pytest.mark.asyncio
async def test_end_to_end_async_audio_generation_and_part_splitting(tmp_path: Path):
    """Test real Edge TTS synthesis + FFmpeg lossless concatenation + multi-part splitting."""
    text = (
        "Chapter 1. Welcome to Oracle Cloud audiobook testing.\n\n"
        "***\n\n"
        "नमस्ते! यह दूसरा पैराग्राफ है जिसमें हिंदी और English दोनों शामिल हैं।"
    )
    out = tmp_path / "sample_audiobook.mp3"
    work = tmp_path / "work"

    parts = await generate_audio_async(
        text=text,
        output_path=out,
        voice="en-US-AndrewMultilingualNeural",
        rate="+5%",
        pitch="+0Hz",
        work_dir=work,
        concurrency=2,
    )

    assert len(parts) == 1
    assert parts[0].exists()
    assert parts[0].stat().st_size > 5000
    assert not work.exists()


@pytest.mark.asyncio
async def test_cancellation_aborts_cleanly(tmp_path: Path):
    text = ("This is a test sentence for cancellation behavior. " * 120)
    out = tmp_path / "cancel_book.mp3"
    work = tmp_path / "work_cancel"

    with pytest.raises(JobCancelled):
        await generate_audio_async(
            text=text,
            output_path=out,
            voice="en-US-AriaNeural",
            rate="+0%",
            pitch="+0Hz",
            work_dir=work,
            should_stop=lambda: True,
        )

    assert not work.exists()
