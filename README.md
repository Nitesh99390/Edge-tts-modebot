# ModuBot TTS Novel — Turbo Oracle Cloud Edition

A compact, fast, and crash-proof Telegram bot that converts **EPUB** and **TXT** novels into MP3 audiobooks using **Microsoft Edge Neural TTS** + **FFmpeg**, optimized for **24/7 operation on Oracle Cloud** (Ubuntu / Debian / Oracle Linux — `x86_64` & `ARM64`).

---

## ⚡ Turbo Performance & Stability

- **~3 Minutes for a 4-Hour Audiobook** — Continuous sliding-window async worker pool (`TTS_CONCURRENCY=8`) keeps 8 Edge TTS WebSocket streams running simultaneously (~90–115× real-time audio speed, well within safe limits).
- **Streaming Ordered FFmpeg Merger** — Completed chunks are merged on-the-fly (`-c:a copy`, zero re-encoding) into `<45 MB` Telegram parts and immediately deleted from disk so RAM & storage stay minimal even on 1 GB Oracle Always-Free VMs.
- **Error & Rate-Limit Proof** — Atomic `.part` writes, 5 automatic retries with exponential backoff + random jitter, per-chunk timeouts, Telegram `RetryAfter` flood-wait handling, and SSML control-char sanitization.
- **100% Verified Voices** — All English, Native Hindi (`Swara`, `Madhur`), and Multilingual Neural voices (seamless Hindi + English / Hinglish) verified live against Microsoft Edge TTS.
- **Smart EPUB3 & Hindi/English TXT Parser** — Preserves `<header><h1>` EPUB3 titles, reads TOC titles from `nav.xhtml` / `toc.ncx` without speaking the TOC itself, and detects Hindi (`अध्याय`, `भाग`, `खंड`, `प्रस्तावना`) & English chapters.

---

## 🚀 One-Command Setup on Oracle Cloud Server

SSH into your Oracle VM (`ssh ubuntu@YOUR_ORACLE_IP` or `ssh opc@YOUR_ORACLE_IP`) and run:

```bash
git clone https://github.com/Nitesh99390/Edge-tts-modebot.git
cd Edge-tts-modebot
chmod +x setup_oracle.sh
./setup_oracle.sh
```

`setup_oracle.sh` installs Python 3 + FFmpeg, sets up `.venv`, configures `.env`, and enables the **`modubot-tts` `systemd` service** so the bot runs 24/7 automatically (even after SSH disconnect or VM reboot).

---

## 🛠️ Oracle Server Commands

| Action | Command |
| :--- | :--- |
| **Status** | `sudo systemctl status modubot-tts` |
| **Live Logs** | `sudo journalctl -u modubot-tts -f` |
| **Restart** | `sudo systemctl restart modubot-tts` |
| **Stop** | `sudo systemctl stop modubot-tts` |
| **Edit `.env`** | `nano .env && sudo systemctl restart modubot-tts` |
| **Update `edge-tts`** | `./.venv/bin/pip install -U edge-tts && sudo systemctl restart modubot-tts` |

---

## 🤖 Telegram Commands

- `/start` — Overview
- `/textvoice` — Start a new audiobook session
- `/status` — Check server disk, FFmpeg, worker count & active job progress
- `/cancel` — Immediately stop active conversion & clean temporary files
- `/help` — Quick usage guide
