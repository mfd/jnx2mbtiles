# Telegram Bot — Deployment on Oracle Cloud Free Tier

`bot.py` receives a `.jnx` file, converts it to `.mbtiles`, and sends it back.  
Large files (100–200+ MB) are supported via a local Telegram Bot API server.

## How it works

1. User sends a `.jnx` file to the bot
2. Bot downloads it through the local API server (no size limit)
3. `jnx2mbtiles.py` converts the file
4. Bot sends back the `.mbtiles` file
5. Temp files are deleted automatically

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `LOCAL_API_URL` | recommended | `http://localhost:8081/bot` — local API server for files > 20 MB |
| `MAX_INPUT_MB` | no | Hard limit on incoming file size (default: `2000`) |

---

## Deploy on Oracle Cloud Free Tier

### 1. Create an instance

Oracle Cloud → Compute → Instances → Create

- Shape: **Ampere A1** (ARM, up to 4 OCPU + 24 GB RAM — always free)
- OS: Ubuntu 22.04

### 2. Install dependencies

```bash
sudo apt update && sudo apt install -y python3-pip docker.io git
sudo usermod -aG docker $USER && newgrp docker
```

### 3. Clone the repository

```bash
git clone https://github.com/mfd/jnx2mbtiles.git
cd jnx2mbtiles
pip3 install -r requirements.txt
```

### 4. Start the local Bot API server

This removes Telegram's 20 MB download / 50 MB upload limits.

Get `api_id` and `api_hash` at [my.telegram.org](https://my.telegram.org) → API development tools.

```bash
docker run -d --name tgbotapi --restart=always \
  -e TELEGRAM_API_ID=<api_id> \
  -e TELEGRAM_API_HASH=<api_hash> \
  -p 8081:8081 \
  -v tgbotapi_data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest
```

Check that it started:

```bash
docker logs tgbotapi
```

### 5. Create a systemd service

```bash
sudo nano /etc/systemd/system/jnx2mb-bot.service
```

```ini
[Unit]
Description=jnx2mbtiles Telegram Bot
After=network.target docker.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/jnx2mbtiles
Environment="BOT_TOKEN=<token_from_botfather>"
Environment="LOCAL_API_URL=http://localhost:8081/bot"
ExecStart=python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jnx2mb-bot
```

### 6. Check logs

```bash
sudo journalctl -fu jnx2mb-bot
```

---

## Without a local API server (small files only)

If your files are under 20 MB, you can skip the Docker step and run the bot directly:

```bash
BOT_TOKEN=<token> python3 bot.py
```

The bot will print a warning that `LOCAL_API_URL` is not set and will use the standard Bot API with its size limits.
