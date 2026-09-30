# Burmese Local Voice Clone Telegram Bot

Telegram bot for Burmese text-to-speech using the **self-hosted F5-Myanmar-TTS** model. No ElevenLabs account or paid voice API key is required.

## Features

- Burmese voice cloning entirely on the VPS
- User access only after redeeming a one-time code
- Admin creates codes by days or months
- Admin can list code statistics and revoke unused codes
- Per-user reference WAV and exact Burmese transcript stored locally
- Daily generation and text-length limits
- Serialized CPU inference queue so a VPS does not run several model jobs at once
- Runs continuously with systemd and long polling
- Temporary Telegram uploads and generated files are deleted after processing

## Model and license

This version uses `f5-myanmar-tts` v2. The model package downloads roughly 643 MB of weights on first startup and supports Burmese zero-shot voice cloning from a short reference clip plus its transcript.

The model is released under **CC BY-NC 4.0**. It is suitable for personal, research, education, and non-commercial use. If Redeem Codes will be sold or the bot will be used commercially, review the model license and obtain permission or switch to a commercially licensed model such as VoxCPM2.

## Important security step

The Telegram bot token pasted into chat must be treated as compromised. Before running the bot:

1. Open `@BotFather` in Telegram.
2. Revoke/regenerate the token for this bot.
3. Put the new token only in `/opt/burmese-voice-bot/.env`.
4. Never commit `.env` or paste the new token into chat.

## Recommended VPS

Hostinger KVM 8 is the better of the two CPU-only options for the model: Hostinger's current official VPS parameter page lists 8 vCPU, 32 GB RAM, and 400 GB disk for KVM 8. A smaller 6-core VPS may run the bot but can be slower or run short of RAM depending on its actual memory allocation.

Keep only one bot process active at a time. Running the same Telegram bot on both VPSs in polling mode will conflict.

## Local voice sample workflow

F5 voice cloning needs both:

- a clear Burmese voice sample, ideally 20–120 seconds;
- the exact Burmese transcript of the words spoken in that sample.

The user sends the audio/voice message with the transcript in the Telegram caption. The bot converts it to 24 kHz mono WAV and stores it in `voice_samples/`. It does not upload the sample to a third-party voice API.

## VPS installation

Run as root or a sudo-capable user on the Hostinger KVM 8 VPS:

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg unzip
sudo useradd --system --home /opt/burmese-voice-bot --shell /usr/sbin/nologin voicebot || true
sudo mkdir -p /opt/burmese-voice-bot
sudo cp -a . /opt/burmese-voice-bot/
sudo chown -R voicebot:voicebot /opt/burmese-voice-bot
cd /opt/burmese-voice-bot
sudo -u voicebot python3 -m venv .venv
sudo -u voicebot .venv/bin/pip install --upgrade pip
# Install CPU-only PyTorch first; this VPS has no NVIDIA GPU.
sudo -u voicebot .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
sudo -u voicebot .venv/bin/pip install -r requirements.txt
sudo -u voicebot cp .env.example .env
sudo nano .env
```

Fill in only:

```dotenv
TELEGRAM_BOT_TOKEN=NEW_TOKEN_FROM_BOTFATHER
ADMIN_ID=841100316
F5_DEVICE=cpu
MAX_TEXT_CHARS=1000
MAX_DAILY_GENERATIONS=20
MAX_SAMPLE_SECONDS=120
```

Protect the file:

```bash
sudo chown voicebot:voicebot /opt/burmese-voice-bot/.env
sudo chmod 600 /opt/burmese-voice-bot/.env
```

The first startup downloads and loads the F5 model. This may take a while and uses several GB of RAM during model initialization.

Install and start the service:

```bash
sudo cp burmese-voice-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now burmese-voice-bot
sudo systemctl status burmese-voice-bot
sudo journalctl -u burmese-voice-bot -f
```

## User flow

1. User sends `/start`.
2. User sends `/redeem CODE`.
3. User sends `/consent`.
4. User sends a clear Burmese voice/audio sample with the exact transcript in the caption.
5. User sends `/say မင်္ဂလာပါ` or any ordinary text.
6. The bot generates speech locally and returns an MP3.

## Admin commands

Admin ID is configured as `841100316`:

```text
/gen_code 30d 5    # generate five 30-day codes
/gen_code 1m 10    # generate ten 30-day codes (1 month = 30 days)
/codes             # stats + latest unused codes
/revoke CODE       # revoke an unused code
/users             # registered-user count
```

## Operational notes

- This version uses polling, so one bot process must run at a time. No public domain or webhook is required.
- CPU-only generation is intentionally serialized. Expect slower responses than a GPU server; benchmark on the actual VPS before opening it to many users.
- The first request is not allowed to trigger model loading because the service loads the model at startup. If the model cannot fit in RAM, the service will fail and the VPS must be upgraded or a smaller/quantized model used.
- SQLite is suitable for a small/medium bot. Move to PostgreSQL if usage grows substantially.
- Voice samples belong only to the users who submit them; keep the consent confirmation and do not use samples without permission.
