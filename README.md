# ModuBot TTS Novel — Standalone Edition

A standalone Telegram bot for converting EPUB and TXT books into
audio using the included Text-to-Voice module.

## Features

- EPUB and TXT input
- Chapter selection and other options supported by the included module
- Available voices, languages, styles and speeds supported by the module
- Audio generation and delivery through Telegram

## Requirements

- Python 3.10 or newer
- FFmpeg installed and available in PATH
- A Telegram bot token
- Your numeric Telegram user ID
- Internet access for Telegram and Edge TTS

## Setup in Termux

Install system packages:

    pkg update
    pkg install python ffmpeg

Extract the ZIP and enter the extracted directory:

    cd ModuBot_TTS_Novel_Only

Create a virtual environment:

    python -m venv .venv
    source .venv/bin/activate

Install Python dependencies:

    python -m pip install --upgrade pip
    pip install -r requirements.txt

Create your private configuration:

    cp .env.example .env
    nano .env

Replace the placeholders:

    BOT_TOKEN=YOUR_OWN_BOT_TOKEN
    OWNER_ID=YOUR_NUMERIC_TELEGRAM_USER_ID

Save the file. In nano, press Ctrl+O, Enter, then Ctrl+X.

Start the bot:

    python bot.py

## Other operating systems

Install Python 3.10+, FFmpeg, and the dependencies in requirements.txt.
Make sure the ffmpeg executable is available in PATH.

Copy .env.example to .env and enter your own bot token and Telegram
user ID before starting the bot with:

    python bot.py

## Privacy and security

- The ZIP does not include the creator's .env file.
- Never share your private .env file or bot token.
- The bot entry point restricts commands, messages, documents and
  callbacks to the OWNER_ID configured in .env.
- Keep .env private and do not upload it to public repositories.
- This package uses Edge TTS and requires internet connectivity.

The included module is packaged separately from the original ModuBot.
