from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .tts import has_speakable_content, sanitize_tts_text, synthesize_sync, synthesize_with_retry

logger = logging.getLogger(__name__)

# Characters per Edge TTS request.
# Important: Devanagari (Hindi) uses 3 UTF-8 bytes per character. Keeping MAX_CHARS
# at 2800 ensures every chunk stays safely within Edge TTS's 4096-byte frame limit
# for English and minimizes mid-sentence internal splitting for Hindi.
MAX_CHARS = 2800
MAX_UTF8_BYTES = 3900

# Telegram lets bots upload files up to 50 MB. Each part stays safely below that.
MAX_PART_BYTES = int(45e6)

# Sentence endings: Latin . ! ? plus Devanagari danda (।), double danda (॥),
# and ellipsis (…), including optional closing quotes/brackets right after punctuation.
_SENTENCE_SPLIT_TOKEN = re.compile(r'([.!?।॥…]+["\')\]»”’]*)\s+')


class JobCancelled(Exception):
    """Raised when the user cancels a conversion that is running."""


def _fits(text: str, max_chars: int, max_bytes: int = MAX_UTF8_BYTES) -> bool:
    return len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes


def _hard_split(text: str, max_chars: int, max_bytes: int = MAX_UTF8_BYTES) -> list[str]:
    """
    Split a piece that exceeds max_chars or max_bytes.
    Cuts happen at the last whitespace/punctuation inside both limits.
    """
    pieces: list[str] = []
    remaining = text.strip()

    while remaining and not _fits(remaining, max_chars, max_bytes):
        # Determine upper character bound that also satisfies UTF-8 byte budget
        upper = min(len(remaining), max_chars)
        while upper > 1 and len(remaining[:upper].encode("utf-8")) > max_bytes:
            # Step back proportionally
            over_ratio = max_bytes / max(1, len(remaining[:upper].encode("utf-8")))
            upper = max(1, min(upper - 1, int(upper * over_ratio)))

        cut = max(
            remaining.rfind(" ", 0, upper),
            remaining.rfind("\n", 0, upper),
            remaining.rfind("।", 0, upper) + 1 if "।" in remaining[:upper] else -1,
            remaining.rfind(",", 0, upper) + 1 if "," in remaining[:upper] else -1,
        )

        if cut <= 0:
            cut = upper

        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)

        remaining = remaining[cut:].lstrip()

    if remaining:
        pieces.append(remaining)

    return pieces


def _sentences(paragraph: str, max_chars: int) -> list[str]:
    result: list[str] = []
    parts = _SENTENCE_SPLIT_TOKEN.split(paragraph)
    merged_sentences: list[str] = []

    idx = 0
    while idx < len(parts):
        seg = parts[idx]
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        combined = f"{seg}{punct}".strip()
        if combined:
            merged_sentences.append(combined)
        idx += 2

    for sentence in merged_sentences:
        if not _fits(sentence, max_chars):
            result.extend(_hard_split(sentence, max_chars))
        else:
            result.append(sentence)

    return result


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """
    Divide text into speakable chunks respecting both character and UTF-8 byte limits.
    Drops decorative-only chunks (like '***' or '---') so Edge TTS never errors out.
    """
    text = sanitize_tts_text(text)
    if not text:
        return []

    raw_chunks: list[str] = []

    if _fits(text, max_chars):
        raw_chunks = [text]
    else:
        current = ""

        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if not paragraph or not has_speakable_content(paragraph):
                continue

            if not _fits(paragraph, max_chars):
                if current:
                    raw_chunks.append(current)
                    current = ""

                for sentence in _sentences(paragraph, max_chars):
                    if not has_speakable_content(sentence):
                        continue
                    candidate = f"{current} {sentence}" if current else sentence
                    if current and _fits(candidate, max_chars):
                        current = candidate
                    else:
                        if current:
                            raw_chunks.append(current)
                        current = sentence
                continue

            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if current and _fits(candidate, max_chars):
                current = candidate
            else:
                if current:
                    raw_chunks.append(current)
                current = paragraph

        if current:
            raw_chunks.append(current)

    return [chunk for chunk in raw_chunks if has_speakable_content(chunk)]


def _ffmpeg_concat_cmd(list_file: Path, output: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-vn",
        "-c:a",
        "copy",
        str(output),
    ]


def _write_concat_list(files: list[Path], list_file: Path) -> None:
    with list_file.open("w", encoding="utf-8") as handle:
        for file in files:
            safe = str(file.resolve()).replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")


def _concat_mp3(files: list[Path], output: Path) -> None:
    """Join MP3 files synchronously without re-encoding them."""
    list_file = output.with_suffix(".concat.txt")
    _write_concat_list(files, list_file)

    try:
        result = subprocess.run(
            _ffmpeg_concat_cmd(list_file, output),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    finally:
        list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg failed while joining audio:\n"
            + (result.stderr or "")[-2000:]
        )


async def _concat_mp3_async(files: list[Path], output: Path) -> None:
    """Join MP3 files asynchronously without blocking the event loop."""
    list_file = output.with_suffix(".concat.txt")
    _write_concat_list(files, list_file)

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_concat_cmd(list_file, output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        list_file.unlink(missing_ok=True)

    if proc.returncode != 0:
        err_text = (stderr_bytes or b"").decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"FFmpeg failed while joining audio:\n{err_text}")


def _join(files: list[Path], output: Path) -> None:
    if len(files) == 1:
        shutil.move(str(files[0]), str(output))
        return

    _concat_mp3(files, output)

    for file in files:
        file.unlink(missing_ok=True)


async def _join_async(files: list[Path], output: Path) -> None:
    if len(files) == 1:
        shutil.move(str(files[0]), str(output))
        return

    await _concat_mp3_async(files, output)

    for file in files:
        file.unlink(missing_ok=True)


def _cleanup_work_dir(work_dir: Path) -> None:
    """Remove only the files this module creates, never anything else."""
    if not work_dir.exists():
        return

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


def _finalize_parts(joined: list[Path], output_path: Path) -> list[Path]:
    if not joined:
        raise RuntimeError("No audio was produced from the selected text.")

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


async def generate_audio_async(
    text: str,
    output_path: str | Path,
    voice: str,
    rate: str | int,
    pitch: str | int,
    work_dir: str | Path,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    max_part_bytes: Optional[int] = None,
    concurrency: int = 3,
) -> list[Path]:
    """
    High-performance async audio generator designed for Oracle Cloud Linux servers.

    Synthesizes chunks in bounded parallel batches (default 3 concurrent streams)
    so network latency is pipelined (~3x faster books) while keeping disk & RAM
    usage minimal, and immediately aborts active network calls on Cancel.
    """
    output_path = Path(output_path)
    work_dir = Path(work_dir)
    budget = max_part_bytes or MAX_PART_BYTES
    batch_size = max(1, min(8, int(concurrency)))

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = split_text(text)
    if not chunks:
        raise RuntimeError("There is no speakable text to convert.")

    if len(chunks) > 1 and not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg was not found on the server. Install it with: sudo apt install -y ffmpeg"
        )

    total_chunks = len(chunks)
    done_count = 0
    joined: list[Path] = []
    group: list[Path] = []
    group_bytes = 0

    async def close_group() -> None:
        nonlocal group, group_bytes
        if not group:
            return
        target = work_dir / f"joined_{len(joined) + 1:03d}.mp3"
        await _join_async(group, target)
        joined.append(target)
        group, group_bytes = [], 0

    try:
        for batch_start in range(0, total_chunks, batch_size):
            if should_stop and should_stop():
                raise JobCancelled()

            batch_slice = chunks[batch_start : batch_start + batch_size]
            batch_files = [
                work_dir / f"chunk_{batch_start + offset + 1:05d}.mp3"
                for offset in range(len(batch_slice))
            ]

            async def _synth_one(idx_in_book: int, chunk_text: str, target_file: Path) -> Path | None:
                nonlocal done_count
                if should_stop and should_stop():
                    raise JobCancelled()

                wrote = await synthesize_with_retry(
                    text=chunk_text,
                    output_path=target_file,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                )
                done_count += 1
                if progress:
                    progress(done_count, total_chunks)

                if not wrote or not target_file.exists():
                    return None
                if target_file.stat().st_size == 0:
                    target_file.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"No audio was returned for chunk {idx_in_book} of {total_chunks}."
                    )
                return target_file

            tasks = [
                asyncio.create_task(
                    _synth_one(batch_start + offset + 1, c_text, c_file)
                )
                for offset, (c_text, c_file) in enumerate(zip(batch_slice, batch_files))
            ]

            try:
                results = await asyncio.gather(*tasks)
            except Exception:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            # Append produced files strictly in chapter/chunk order
            for chunk_file in results:
                if chunk_file is None:
                    continue
                size = chunk_file.stat().st_size
                if group and group_bytes + size > budget:
                    await close_group()
                group.append(chunk_file)
                group_bytes += size

        await close_group()

        if should_stop and should_stop():
            raise JobCancelled()

        return _finalize_parts(joined, output_path)

    finally:
        _cleanup_work_dir(work_dir)


def generate_audio(
    text: str,
    output_path: str | Path,
    voice: str,
    rate: str | int,
    pitch: str | int,
    work_dir: str | Path,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    max_part_bytes: Optional[int] = None,
) -> list[Path]:
    """
    Synchronous wrapper kept for backward compatibility and unit tests.
    """
    output_path = Path(output_path)
    work_dir = Path(work_dir)
    budget = max_part_bytes or MAX_PART_BYTES

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = split_text(text)
    if not chunks:
        raise RuntimeError("There is no speakable text to convert.")

    if len(chunks) > 1 and not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg was not found. Install it first (Ubuntu/Oracle: sudo apt install -y ffmpeg)."
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

            wrote = synthesize_sync(
                text=chunk,
                output_path=chunk_file,
                voice=voice,
                rate=rate,
                pitch=pitch,
            )

            if not wrote:
                if progress:
                    progress(index, len(chunks))
                continue

            size = chunk_file.stat().st_size if chunk_file.exists() else 0
            if size == 0:
                raise RuntimeError(
                    f"No audio was returned for part {index} of {len(chunks)}."
                )

            if group and group_bytes + size > budget:
                close_group()

            group.append(chunk_file)
            group_bytes += size

            if progress:
                progress(index, len(chunks))

        close_group()

        if should_stop and should_stop():
            raise JobCancelled()

        return _finalize_parts(joined, output_path)

    finally:
        _cleanup_work_dir(work_dir)
