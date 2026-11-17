# Changelog — Oracle Cloud Server & Complete Bug-Fix Release

## All Bugs Found & Fixed

### 1. Broken / Non-Existent Voice IDs (`modules/textvoice/tts.py`) — **CRITICAL**
- **7 out of 9 Hindi voices** (`hi-IN-AaravNeural`, `hi-IN-AnanyaNeural`, `hi-IN-AartiNeural`, `hi-IN-ArjunNeural`, `hi-IN-KavyaNeural`, `hi-IN-KunalNeural`, `hi-IN-RehaanNeural`) and **`en-AU-WilliamNeural`** do not exist on Microsoft Edge TTS endpoints — selecting any of them caused every audiobook job to fail after 3 retries (`NoAudioReceived`).
- **Fixed**: Verified every voice live against Microsoft's endpoint. Kept native `hi-IN-SwaraNeural` & `hi-IN-MadhurNeural`, updated `en-AU-WilliamMultilingualNeural`, added `en-IN-NeerjaExpressiveNeural`, and added Multilingual Neural voices (`Andrew`, `Ava`, `Brian`, `Emma`, `Remy`, `Vivienne`) that fluently speak pure Devanagari Hindi as well as mixed Hindi + English (Hinglish).

### 2. Bot Still Froze During Generation (`bot.py`) — **CRITICAL**
- Even though `_run_job` was launched via `application.create_task(...)`, `Application.builder()` did **not** enable `.concurrent_updates(True)`. Moreover, `asyncio.to_thread` spawned `asyncio.run()` on every single chunk, creating hundreds of temporary event loops per book.
- **Fixed**: Enabled `.concurrent_updates(True)` in `bot.py`, switched the entire TTS + FFmpeg pipeline (`generate_audio_async`, `synthesize_with_retry`, `_concat_mp3_async`) to native `asyncio` with bounded concurrency (`TTS_CONCURRENCY=3`), and cancelled active tasks immediately when `/cancel` or **🛑 Cancel** is pressed.

### 3. EPUB3 Chapter Titles Lost & Nav TOC Spoken as Chapter 1 (`modules/textvoice/epub.py`) — **HIGH**
- `_extract_html_text()` decomposed `<header>` BEFORE calling `_extract_title()`, wiping out `<h1>`/`<h2>` titles wrapped inside `<header>` in standard EPUB3 novels.
- EPUB3 `nav.xhtml` (`properties="nav"`) was read as a regular chapter, making the bot read the entire Table of Contents aloud.
- Chapters without `<h1>–<h4>` tags fell back to generic `"Chapter N"` instead of reading titles from `toc.ncx` or EPUB3 `nav`.
- Short prologues/epilogues (<20 chars) were silently dropped, and duplicate spine hrefs could duplicate chapters.
- **Fixed**: Preserved `<header>` for title extraction, skipped standalone `nav` documents from chapter bodies while parsing both EPUB3 `nav` and EPUB2 `toc.ncx` for accurate chapter titles, added `lxml` fast HTML/XML parsing, deduplicated spine paths, and added zip-bomb size guards.

### 4. Hindi UTF-8 Byte Limit & Decorative Divider Crash (`modules/textvoice/audio.py` & `tts.py`) — **HIGH**
- Devanagari characters take **3 bytes each in UTF-8**, so a 3,500-char Hindi chunk was ~10,500 bytes (nearly 3× Edge TTS's 4,096-byte frame limit), causing mid-word splits.
- Punctuation-only paragraphs (scene dividers like `***`, `---`, `• • •`) or XML control characters (`\x00–\x1f`) caused Edge TTS to return `NoAudioReceived` and abort the entire novel.
- Sentences ending with closing quotes (`"..."`, `'...'`, `।”`) were not split cleanly.
- **Fixed**: Enforced both character (`2800`) and UTF-8 byte (`3900`) chunk budgets, added `_SPEAKABLE` filtering and SSML control-character sanitization, and improved sentence-boundary regex for quoted dialogue and Hindi dandas (`।`, `॥`).

### 5. TXT Hindi Headings & Single-Chapter Detection (`modules/textvoice/epub.py`) — **MEDIUM**
- TXT splitter previously only matched English `Chapter \d+` and required at least 2 matches.
- **Fixed**: Added support for Hindi headings (`अध्याय 1`, `अध्याय १`, `भाग`, `खंड`, `प्रस्तावना`, `उपसंहार`), Roman numerals (`Chapter IV`), word numbers (`Chapter One`), `Part`/`Book`/`Prologue`/`Epilogue`, and single-chapter files.

### 6. Race Conditions, Memory Footprint & Telegram API Resilience (`handler.py`, `bot.py`, `config.py`) — **MEDIUM**
- Guarded `reading` stage against concurrent uploads (`_is_busy` now checks both `reading` and `generating`).
- Freed `state["chapters"]` and `job["text"]` from RAM once chunking/synthesis completes (crucial for 1 GB Oracle Cloud Always-Free VMs) and deleted raw `.epub`/`.txt` right after parsing.
- Added automatic `RetryAfter` (Telegram FloodWait), `TimedOut`, and `NetworkError` retry logic on multi-part audio uploads, plus explicit `filename=` so Telegram displays clean `.mp3` names.
- Added `/cancel`, `/status`, `/help`, **📋 View Chapter List**, Back button from Language -> Range, rotating logs (`logs/bot.log`), global `error_handler`, and relative `.env` loading via `Path(__file__).resolve().parent / ".env"`.

### 7. Oracle Cloud Deployment Tooling
- Added `setup_oracle.sh` (automated installer + `systemd` service generator for Ubuntu/Debian/Oracle Linux x86_64 & ARM64).
- Added `modubot-tts.service` template, `Dockerfile`, and `docker-compose.yml`.
