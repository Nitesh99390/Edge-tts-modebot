# Changes in this fixed version

## Bugs fixed

### Critical
- **Speed choice was ignored.** The speed labels are strings such as "0.85×", so
  the value read for the rate was always 0. Speeds now add their offset to the
  style rate (slow + 0.85× = -30%).
- **The bot froze during conversion.** The generation ran inside the button
  handler, and python-telegram-bot processes updates one at a time by default.
  Nothing else (not even /textvoice or Cancel) was answered until the whole book
  finished. Conversion now runs as a background task.

### High
- **Long books failed at upload.** Telegram bots can upload at most 50 MB, which
  is about 2.3 hours of audio at this bitrate. Long books are now split into
  parts (`<name>_part01.mp3`, ...), each under 45 MB, and sent one after another.
- **One network hiccup killed the whole job.** Every TTS request now retries up
  to 3 times with a short back-off.
- **Words were split across lines in EPUBs.** Inline tags such as `<em>`,
  `<span>` and `<a>` put a newline inside sentences. Text is now joined correctly,
  and paragraphs, table cells and list items stay separate.
- **Chapter headings were read twice** (EPUB chapters begin with their heading,
  and the heading was also added as a title). Each heading is now spoken once.
- **Hindi text was cut in the wrong places.** The Devanagari danda (।) was not
  recognised as a sentence end, so long Hindi paragraphs were cut mid-word.

### Medium
- **Text before "Chapter 1" in TXT files was lost** (prologue, foreword). It is
  now kept as an "Introduction" section.
- **TXT files saved as UTF-16 or Windows-1252 came out as garbage.** Encoding is
  now detected.
- **EPUB chapters with percent-encoded file names were silently skipped** (for
  example `caf%C3%A9.xhtml`). Hrefs are now decoded, and letter case is ignored
  when matching.
- **Temporary files piled up.** A new book, /textvoice, or a failed job left the
  previous book (and any partial audio) on disk. The old folder is now removed,
  failed jobs are cleaned up, and leftovers are removed at start-up.
- **Double taps are now guarded.** Conversions run in the background, so a second
  tap while one is running would otherwise start another job in the same folder.
  Other buttons and new uploads are refused with a notice while a conversion runs.
- **Cancel was not possible** during generation. A Cancel button is now shown on
  the progress message.

### Low
- The chapter-range screen had no buttons, so the user could get stuck. Back and
  Cancel buttons are now shown there.
- The "unknown option" branch answered the callback query twice, which Telegram rejects.
- Speed buttons used list positions as callback ids. They now use the speed value.
- The uploaded file name was used directly in a path. It is now sanitised.
- Files over 20 MB (the Telegram download limit) are refused with a clear message.
- A missing FFmpeg gave a cryptic error. The message now says how to install it.
- Progress and status messages show the speed label and voice name, not raw numbers.
- The Telegram audio title keeps the book's real name (spaces, not underscores).
- The "Energetic" style fallback in the handler moved into the style config.

## Behaviour changes
- Each part of a long book is sent with a caption such as "Part 2 of 5".
- Progress is shown on the status message roughly every 20 seconds.
- Only one conversion runs at a time. Send /textvoice or another book after it
  finishes, or press Cancel.
- Temporary files are stored in `temp/textvoice` inside the project folder.

## Files changed
- `modules/textvoice/audio.py`: rewritten (splitting, parts, cancellation, cleanup).
- `modules/textvoice/handler.py`: rewritten (speed fix, background job, cancel, cleanup, flows).
- `modules/textvoice/tts.py`: retries, energetic style moved here.
- `modules/textvoice/epub.py`: text joining, href decoding, TXT decoding and preamble.
- `bot.py`: start-up cleanup call.

## Not changed / to check on your side
- The Hindi voice IDs (Aarav, Ananya, ...) could not be checked offline. On your
  phone, run `edge-tts --list-voices | grep hi-IN` and remove any name that does not appear.
- Edge TTS is an unofficial endpoint. If it starts failing, run `pip install -U edge-tts`.
- TXT chapter detection only recognises English "Chapter N" headings. Hindi
  "अध्याय" headings are not detected yet.
