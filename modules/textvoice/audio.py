from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .tts import synthesize_sync


# Characters per Edge TTS request. edge-tts splits text internally at 4096
# bytes, so this only decides how the book is divided.
MAX_CHARS = 3500

# Telegram lets bots upload files up to 50 MB. Each part stays below that.
# Edge TTS audio is about 21.6 MB per hour (48 kbps), so a part holds roughly 2 hours.
MAX_PART_BYTES = int(45e6)

# Sentence endings: Latin . ! ? plus the Devanagari danda (।) used in Hindi
# and the double danda (॥).
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+")


class JobCancelled(Exception):
    """Raised when the user cancels a conversion that is running."""


def _hard_split(text: str, max_chars: int) -> list[str]:
    """
    Split a piece that is longer than max_chars.

    Cuts happen at the last whitespace inside the limit. A word is cut in
    half only when that word is longer than max_chars by itself.
    """
    pieces: list[str] = []
    start = 0
    length = len(text)

    while length - start > max_chars:
        window_end = start + max_chars
        cut = max(
            text.rfind(" ", start, window_end),
            text.rfind("\n", start, window_end),
        )

        if cut <= start:
            cut = window_end

        piece = text[start:cut].strip()
        if piece:
            pieces.append(piece)

        start = cut
        while start < length and text[start].isspace():
            start += 1

    tail = text[start:].strip()
    if tail:
        pieces.append(tail)

    return pieces


def _sentences(paragraph: str, max_chars: int) -> list[str]:
    result: list[str] = []

    for sentence in _SENTENCE_END.split(paragraph):
        sentence = sentence.strip()

        if not sentence:
            continue

        if len(sentence) > max_chars:
            result.extend(_hard_split(sentence, max_chars))
        else:
            result.append(sentence)

    return result


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """
    Divide text into chunks of at most max_chars characters.

    Paragraphs stay together when they fit. A paragraph that is too long is
    packed sentence by sentence, so a chunk does not end mid-sentence.
    Hindi text works through the danda (।) sentence marker.
    """
    text = text.strip()

    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        if len(paragraph) > max_chars:
            # Start the long paragraph on a fresh chunk, then pack its sentences.
            if current:
                chunks.append(current)
                current = ""

            for sentence in _sentences(paragraph, max_chars):
                if current and len(current) + 1 + len(sentence) <= max_chars:
                    current = f"{current} {sentence}"
                else:
                    if current:
                        chunks.append(current)
                    current = sentence

            continue

        if current and len(current) + 2 + len(paragraph) <= max_chars:
            current = f"{current}\n\n{paragraph}"
        else:
            if current:
                chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def _concat_mp3(files: list[Path], output: Path) -> None:
    """Join MP3 files without re-encoding them."""
    list_file = output.with_suffix(".concat.txt")

    with list_file.open("w", encoding="utf-8") as handle:
        for file in files:
            safe = str(file.resolve()).replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg failed while joining audio:\n"
            + result.stderr[-2000:]
        )


def _join(files: list[Path], output: Path) -> None:
    if len(files) == 1:
        shutil.move(str(files[0]), str(output))
        return

    _concat_mp3(files, output)

    for file in files:
        file.unlink(missing_ok=True)


def _cleanup_work_dir(work_dir: Path) -> None:
    """Remove only the files this module creates, never anything else."""
    leftovers = (
        list(work_dir.glob("chunk_*.mp3"))
        + list(work_dir.glob("joined_*.mp3"))
        + list(work_dir.glob("*.concat.txt"))
    )

    for leftover in leftovers:
        leftover.unlink(missing_ok=True)

    try:
        work_dir.rmdir()
    except OSError:
        pass


def generate_audio(
    text: str,
    output_path: str | Path,
    voice: str,
    rate: str,
    pitch: str,
    work_dir: str | Path,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    max_part_bytes: Optional[int] = None,
) -> list[Path]:
    """
    Convert text to MP3 and return the finished file(s).

    A short text gives one file, output_path. A long book is split into
    <name>_part01.mp3, <name>_part02.mp3 and so on, each small enough to
    upload to Telegram. progress(done, total) runs after every chunk.
    should_stop() is checked between chunks, so a job can be cancelled.
    """
    output_path = Path(output_path)
    work_dir = Path(work_dir)
    budget = max_part_bytes or MAX_PART_BYTES

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = split_text(text)

    if not chunks:
        raise RuntimeError("There is no text to convert.")

    if len(chunks) > 1 and not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg was not found. Install it first (Termux: pkg install ffmpeg)."
        )

    joined: list[Path] = []
    group: list[Path] = []
    group_bytes = 0

    def close_group() -> None:
        nonlocal group, group_bytes

        if not group:
            return

        target = work_dir / f"joined_{len(joined) + 1:03d}.mp3"
        _join(group, target)
        joined.append(target)
        group, group_bytes = [], 0

    try:
        for index, chunk in enumerate(chunks, start=1):
            if should_stop and should_stop():
                raise JobCancelled()

            chunk_file = work_dir / f"chunk_{index:05d}.mp3"

            synthesize_sync(
                text=chunk,
                output_path=chunk_file,
                voice=voice,
                rate=rate,
                pitch=pitch,
            )

            size = chunk_file.stat().st_size if chunk_file.exists() else 0

            if size == 0:
                raise RuntimeError(
                    f"No audio was returned for part {index} of {len(chunks)}."
                )

            # Start a new part when this chunk would push the current one over budget.
            if group and group_bytes + size > budget:
                close_group()

            group.append(chunk_file)
            group_bytes += size

            if progress:
                progress(index, len(chunks))

        close_group()

        if should_stop and should_stop():
            raise JobCancelled()

        if len(joined) == 1:
            finals = [output_path]
        else:
            finals = [
                output_path.with_name(
                    f"{output_path.stem}_part{number:02d}{output_path.suffix}"
                )
                for number in range(1, len(joined) + 1)
            ]

        for source, destination in zip(joined, finals):
            shutil.move(str(source), str(destination))

        return finals

    finally:
        _cleanup_work_dir(work_dir)
