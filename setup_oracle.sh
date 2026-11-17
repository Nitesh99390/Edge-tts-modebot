#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ModuBot TTS Novel — Oracle Cloud Server Installer & Service Setup
# Supports: Ubuntu 20.04 / 22.04 / 24.04, Debian 11/12, Oracle Linux 8/9 (x86_64 & ARM64)
# ==============================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="modubot-tts"
CURRENT_USER="$(id -un)"

echo "============================================================"
echo " 🎙️ ModuBot TTS Novel — Oracle Cloud Server Setup"
echo " 📁 Project Directory: ${APP_DIR}"
echo " 👤 Running User:      ${CURRENT_USER}"
echo "============================================================"

# 1. Install system dependencies (Python3, venv, pip, ffmpeg)
echo ""
echo "[1/4] Checking & installing system packages (Python 3 + FFmpeg)..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg
elif command -v dnf >/dev/null 2>&1; then
    # Oracle Linux / AlmaLinux / Rocky Linux 8 & 9
    sudo dnf install -y python3 python3-pip
    if ! command -v ffmpeg >/dev/null 2>&1; then
        echo "Installing static FFmpeg binary for Oracle Linux..."
        ARCH="$(uname -m)"
        TMP_FF="$(mktemp -d)"
        if [ "${ARCH}" = "aarch64" ]; then
            FF_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
        else
            FF_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        fi
        curl -fsSL "${FF_URL}" -o "${TMP_FF}/ffmpeg.tar.xz"
        tar -xf "${TMP_FF}/ffmpeg.tar.xz" -C "${TMP_FF}" --strip-components=1
        sudo install -m 0755 "${TMP_FF}/ffmpeg" /usr/local/bin/ffmpeg
        sudo install -m 0755 "${TMP_FF}/ffprobe" /usr/local/bin/ffprobe
        rm -rf "${TMP_FF}"
    fi
else
    echo "⚠️ Package manager not recognized. Please ensure python3 and ffmpeg are installed."
fi

# 2. Create Python virtual environment and install dependencies
echo ""
echo "[2/4] Setting up Python virtual environment (.venv)..."
cd "${APP_DIR}"
python3 -m venv .venv
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
"${APP_DIR}/.venv/bin/pip" install -r requirements.txt

# 3. Configure .env if missing or still has placeholders
echo ""
echo "[3/4] Checking .env configuration..."
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
fi

if grep -q "PASTE_YOUR_OWN_TELEGRAM_BOT_TOKEN_HERE\|YOUR_NUMERIC_TELEGRAM_USER_ID" "${APP_DIR}/.env"; then
    if [ -t 0 ]; then
        echo ""
        echo "🔑 Let's configure your Telegram Bot credentials:"
        read -rp "👉 Enter your Telegram BOT_TOKEN (from @BotFather): " INPUT_TOKEN
        read -rp "👉 Enter your numeric OWNER_ID (from @userinfobot): " INPUT_OWNER
        if [ -n "${INPUT_TOKEN}" ] && [ -n "${INPUT_OWNER}" ]; then
            cat > "${APP_DIR}/.env" <<EOF
BOT_TOKEN=${INPUT_TOKEN}
OWNER_ID=${INPUT_OWNER}
TTS_CONCURRENCY=3
LOG_LEVEL=INFO
EOF
            chmod 600 "${APP_DIR}/.env"
            echo "✅ Saved credentials to ${APP_DIR}/.env"
        else
            echo "⚠️ Credentials left empty. Edit ${APP_DIR}/.env before starting the service:"
            echo "   nano ${APP_DIR}/.env"
        fi
    else
        echo "⚠️ Non-interactive shell detected. Please edit ${APP_DIR}/.env manually:"
        echo "   nano ${APP_DIR}/.env"
    fi
else
    chmod 600 "${APP_DIR}/.env"
    echo "✅ Valid .env configuration found."
fi

# 4. Install & enable systemd service for 24/7 background operation
echo ""
echo "[4/4] Installing systemd service (${SERVICE_NAME}.service)..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "${SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=ModuBot Telegram TTS Novel Bot (Oracle Cloud Edition)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=8
TimeoutStopSec=30
KillSignal=SIGINT

# Security & resource hygiene
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

if ! grep -q "PASTE_YOUR_OWN_TELEGRAM_BOT_TOKEN_HERE\|YOUR_NUMERIC_TELEGRAM_USER_ID" "${APP_DIR}/.env"; then
    sudo systemctl restart "${SERVICE_NAME}"
    echo ""
    echo "✅ Bot service started! Checking status:"
    sudo systemctl --no-pager --full status "${SERVICE_NAME}" || true
else
    echo ""
    echo "ℹ️ Once you update ${APP_DIR}/.env, start the bot with:"
    echo "   sudo systemctl restart ${SERVICE_NAME}"
fi

echo ""
echo "============================================================"
echo " 🎉 Setup Complete! Useful Oracle Server Commands:"
echo "   • Check status:  sudo systemctl status ${SERVICE_NAME}"
echo "   • Live logs:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "   • Restart bot:   sudo systemctl restart ${SERVICE_NAME}"
echo "   • Stop bot:      sudo systemctl stop ${SERVICE_NAME}"
echo "   • Update EdgeTTS: ${APP_DIR}/.venv/bin/pip install -U edge-tts && sudo systemctl restart ${SERVICE_NAME}"
echo "============================================================"
