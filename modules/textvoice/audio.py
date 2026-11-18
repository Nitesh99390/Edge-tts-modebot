from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from .tts import has_speakable_content, sanitize_tts_text, synthesize_with_retry

logger = logging.getLogger(__name__)

# Single WebSocket frame per chunk (~3300 chars / <=3900 UTF-8 bytes) = zero mid-chunk WS delay
MAX_CHARS = 3300
MAX_UTF8_BYTES = 3900
MAX_PART_BYTES = int(45e6)
DEFAULT_WORKERS = 8

_SENTENCE_SPLIT_TOKEN = re.compile(r'([.!?।॥…]+["\')\]»”’]*)\s+')


class JobCancelled(Exception):
    """Raised when the user cancels a running conversion."""


def _fits(text: str, max_chars: int, max_bytes: int = MAX_UTF8_BYTES) -> bool:
    return len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes


def _hard_split(text: str, max_chars: int, max_bytes: int = MAX_UTF8_BYTES) -> list[str]:
    pieces: list[str] = []
    remaining = text.strip()

    while remaining and not _fits(remaining, max_chars, max_bytes):
        upper = min(len(remaining), max_chars)
        while upper > 1 and len(remaining[:upper].encode("utf-8")) > max_bytes:
            ratio = max_bytes / max(1, len(remaining[:upper].encode("utf-8")))
            upper = max(1, min(upper - 1, int(upper * ratio)))

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
    parts = _SENTENCE_SPLIT_TOKEN.split(paragraph)
    result: list[str] = []

    idx = 0
    while idx < len(parts):
        seg = parts[idx]
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        sentence = f"{seg}{punct}".strip()
        if sentence:
            if _fits(sentence, max_chars):
                result.append(sentence)
            else:
                result.extend(_hard_split(sentence, max_chars))
        idx += 2

    return result


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    text = sanitize_tts_text(text)
    if not text:
        return []

    if _fits(text, max_chars):
        return [text] if has_speakable_content(text) else []

    raw_chunks: list[str] = []
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


async def _concat_mp3_async(files: list[Path], output: Path) -> None:
    """Lossless MP3 stream-copy concatenation via FFmpeg (<1 second per 45 MB part)."""
    if len(files) == 1:
        shutil.move(str(files[0]), str(output))
        return

    list_file = output.with_suffix(".concat.txt")
    with list_file.open("w", encoding="utf-8") as handle:
        for file in files:
            safe = str(file.resolve()).replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
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
        err_text = (stderr_bytes or b"").decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"FFmpeg join failed:\n{err_text}")

    for file in files:
        file.unlink(missing_ok=True)


def _cleanup_work_dir(work_dir: Path) -> None:
    if not work_dir.exists():
        return
    for pattern in ("chunk_*.mp3*", "joined_*.mp3", "*.concat.txt"):
        for leftover in work_dir.glob(pattern):
            leftover.unlink(missing_ok=True)
    try:
        work_dir.rmdir()
    except OSError:
        pass


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
    concurrency: int = DEFAULT_WORKERS,
) -> list[Path]:
    """
    Continuous Sliding-Window Worker Pool + Streaming Ordered Merger.

    - Keeps `concurrency` (default 8) Edge TTS streams continuously active
      with zero straggler idle wait (~85-115x realtime: 4-hour book in ~2.5-3.5 min).
    - Merges contiguous completed chunks into <=45 MB parts on-the-fly and deletes
      raw chunk files immediately so disk & RAM stay tiny even on 20-hour books.
    """
    output_path = Path(output_path)
    work_dir = Path(work_dir)
    budget = max_part_bytes or MAX_PART_BYTES
    worker_count = max(1, min(12, int(concurrency)))

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = split_text(text)
    if not chunks:
        raise RuntimeError("There is no speakable text to convert.")

    if len(chunks) > 1 and not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg was not found. Install it with: sudo apt install -y ffmpeg"
        )

    total_chunks = len(chunks)
    if should_stop and should_stop():
        _cleanup_work_dir(work_dir)
        raise JobCancelled()

    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for idx, chunk in enumerate(chunks):
        queue.put_nowait((idx, chunk))
    for _ in range(worker_count):
        queue.put_nowait(None)

    # State shared between workers and the streaming ordered merger
    completed: dict[int, Path | None] = {}
    ready_event = asyncio.Event()
    done_count = 0
    fatal_error: BaseException | None = None

    async def _worker() -> None:
        nonlocal done_count, fatal_error
        while True:
            item = await queue.get()
            if item is None:
                return

            idx, chunk_text = item
            if fatal_error is not None or (should_stop and should_stop()):
                if fatal_error is None:
                    fatal_error = JobCancelled()
                ready_event.set()
                return

            target = work_dir / f"chunk_{idx + 1:05d}.mp3"
            try:
                wrote = await synthesize_with_retry(
                    text=chunk_text,
                    output_path=target,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                )
                if wrote and target.exists() and target.stat().st_size > 0:
                    completed[idx] = target
                else:
                    target.unlink(missing_ok=True)
                    completed[idx] = None

                done_count += 1
                if progress:
                    progress(done_count, total_chunks)
                ready_event.set()

            except BaseException as exc:
                target.unlink(missing_ok=True)
                if fatal_error is None:
                    fatal_error = exc
                ready_event.set()
                return

    joined: list[Path] = []
    group: list[Path] = []
    group_bytes = 0

    async def _flush_group() -> None:
        nonlocal group, group_bytes
        if not group:
            return
        part_target = work_dir / f"joined_{len(joined) + 1:03d}.mp3"
        await _concat_mp3_async(group, part_target)
        joined.append(part_target)
        group, group_bytes = [], 0

    worker_tasks = [
        asyncio.create_task(_worker())
        for _ in range(min(worker_count, total_chunks))
    ]

    try:
        next_idx = 0
        while next_idx < total_chunks:
            if should_stop and should_stop():
                raise JobCancelled()
            if fatal_error is not None:
                raise fatal_error

            while next_idx in completed:
                chunk_file = completed.pop(next_idx)
                next_idx += 1
                if chunk_file is None:
                    continue
                size = chunk_file.stat().st_size
                if group and group_bytes + size > budget:
                    await _flush_group()
                group.append(chunk_file)
                group_bytes += size

            if next_idx < total_chunks:
                ready_event.clear()
                if next_idx not in completed and fatal_error is None:
                    await ready_event.wait()

        await asyncio.gather(*worker_tasks)
        if fatal_error is not None:
            raise fatal_error

        await _flush_group()

        if should_stop and should_stop():
            raise JobCancelled()

        if not joined:
            raise RuntimeError("No audio was produced from the selected text.")

        if len(joined) == 1:
            finals = [output_path]
        else:
            finals = [
                output_path.with_name(
                    f"{output_path.stem}_part{num:02d}{output_path.suffix}"
                )
                for num in range(1, len(joined) + 1)
            ]

        for src, dst in zip(joined, finals):
            shutil.move(str(src), str(dst))

        return finals

    except BaseException:
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        raise

    finally:
        _cleanup_work_dir(work_dir)
