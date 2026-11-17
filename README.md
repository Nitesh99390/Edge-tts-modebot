# ModuBot TTS Novel — Oracle Cloud Server Edition

A production-ready standalone Telegram bot for converting **EPUB** and **TXT** novels into MP3 audiobooks using **Microsoft Edge Neural TTS** + **FFmpeg**, optimized for **24/7 operation on Oracle Cloud Free Tier** (Ubuntu / Debian / Oracle Linux — both AMD `x86_64` and Ampere `ARM64`).

---

## ✨ Key Features & Oracle Cloud Optimizations

- **EPUB & TXT Novel Support** — Automatically parses EPUB2/EPUB3 spine & TOC titles, plus TXT chapter headings in English (`Chapter 1`, `Prologue`, Roman numerals) and Hindi (`अध्याय 1`, `भाग 1`, `प्रस्तावना`).
- **100% Verified Edge TTS Voices** — Includes verified English (US, India, UK, Australia), Native Hindi (`Swara`, `Madhur`), and Multilingual Neural voices that seamlessly read mixed **Hindi + English (Hinglish)** novels.
- **3× Faster Async Pipelined Conversion** — Runs Edge TTS natively inside `asyncio` with configurable concurrency (`TTS_CONCURRENCY=3`) and zero nested event-loop thread overhead.
- **Automatic <45 MB Part Splitting** — Long novels (2–50+ hours) are losslessly concatenated via FFmpeg (`-c:a copy`) and automatically split into parts under Telegram's 50 MB bot upload limit.
- **Low RAM & Disk Footprint** — Ideal for 1 GB RAM Oracle Always-Free VMs: deletes downloaded source files once parsed, frees chapter memory during synthesis, and cleans temp files on completion, cancellation, startup, and shutdown.
- **Instant Cancellation & Server Commands** — Use `/cancel` or the inline **🛑 Cancel** button to abort in-flight TTS requests immediately; check VM disk & job health anytime with `/status`.
- **24/7 `systemd` Service & Docker Support** — Auto-starts on server reboot and auto-restarts on network hiccups, with rotating log files (`logs/bot.log`).

---

## 🚀 Quick Setup on Oracle Cloud Server (One Command)

SSH into your Oracle Cloud VM (`ssh ubuntu@YOUR_ORACLE_IP` or `ssh opc@YOUR_ORACLE_IP`) and run:

```bash
git clone https://github.com/Nitesh99390/Edge-tts-modebot.git
cd Edge-tts-modebot
chmod +x setup_oracle.sh
./setup_oracle.sh
```

`setup_oracle.sh` automatically:
1. Installs **Python 3**, `venv`, `pip`, and **FFmpeg** (`apt` on Ubuntu/Debian, `dnf` + static FFmpeg on Oracle Linux).
2. Creates `.venv` and installs all Python packages (`python-telegram-bot`, `edge-tts`, `beautifulsoup4`, `lxml`, `python-dotenv`).
3. Prompts you for your `BOT_TOKEN` and `OWNER_ID` and writes a secure `.env` (`chmod 600`).
4. Creates, enables, and starts the **`modubot-tts` systemd background service** so the bot keeps running 24/7 even after you close SSH or reboot the Oracle VM!

---

## 🛠️ Useful Oracle Server Commands (Hinglish / Linux Cheat Sheet)

| Action | Command |
| :--- | :--- |
| **Check Bot Status** | `sudo systemctl status modubot-tts` |
| **View Live Logs** | `sudo journalctl -u modubot-tts -f` |
| **Restart Bot** | `sudo systemctl restart modubot-tts` |
| **Stop Bot** | `sudo systemctl stop modubot-tts` |
| **Edit `.env` Token/ID** | `nano .env && sudo systemctl restart modubot-tts` |
| **Update `edge-tts`** | `./.venv/bin/pip install -U edge-tts && sudo systemctl restart modubot-tts` |

*(Note: Oracle Cloud par outbound traffic by default open hota hai, aur Telegram polling mode use ho raha hai isliye koi inbound port/firewall open karne ki zaroorat nahi hai!)*

---

## 🐳 Alternative: Run with Docker Compose

If you prefer Docker on your Oracle Server:

```bash
cp .env.example .env
nano .env   # Fill BOT_TOKEN and OWNER_ID
docker compose up -d --build
docker compose logs -f
```

---

## 🤖 Telegram Bot Commands

- `/start` — Welcome message & quick overview
- `/textvoice` — Start a new EPUB / TXT audiobook session
- `/status` — Check Oracle server disk free space, FFmpeg status, and active conversion progress
- `/cancel` — Immediately stop a running conversion and clean temporary files
- `/help` — Step-by-step usage guide

---

## ⚙️ Environment Variables (`.env`)

```env
BOT_TOKEN=123456789:ABCDEF...
OWNER_ID=987654321
# Optional: comma-separated extra allowed user IDs
# ALLOWED_USERS=111222333,444555666
TTS_CONCURRENCY=3
LOG_LEVEL=INFO
```

---

## 🔒 Privacy & Security

- Only Telegram user IDs listed in `OWNER_ID` (and optional `ALLOWED_USERS`) can interact with the bot.
- `.env` is excluded via `.gitignore` and locked to `chmod 600` by `setup_oracle.sh`.
