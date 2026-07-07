#!/usr/bin/env python3
"""
Telegram bot: receive .jnx → convert → return .mbtiles

Environment variables:
  BOT_TOKEN        (required) Telegram bot token from @BotFather
  LOCAL_API_URL    (optional) http://localhost:8081/bot  — local Bot API server for files > 20 MB
  MAX_INPUT_MB     (optional) hard limit on incoming file size, default 2000

Large file support requires a local Telegram Bot API server:
  docker run -d --name tgbotapi --restart=always \
    -e TELEGRAM_API_ID=<api_id> \
    -e TELEGRAM_API_HASH=<api_hash> \
    -p 8081:8081 \
    -v tgbotapi_data:/var/lib/telegram-bot-api \
    aiogram/telegram-bot-api:latest
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import jnx2mbtiles

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ['BOT_TOKEN']
LOCAL_API_URL = os.environ.get('LOCAL_API_URL')          # None → standard Bot API (20 MB limit)
MAX_INPUT_MB = int(os.environ.get('MAX_INPUT_MB', 2000))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a .jnx file — I'll convert it to .mbtiles (EPSG:3857).\n\n"
        "Supports both spherical (Google/OSM/Bing) and ellipsoidal (Yandex) tile grids.\n"
        "Large files (100-200+ MB) are supported when the bot runs with a local API server."
    )


async def handle_document(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    fname = doc.file_name or ''

    if not fname.lower().endswith('.jnx'):
        await update.message.reply_text("Please send a file with the .jnx extension.")
        return

    size_mb = (doc.file_size or 0) / 1024 / 1024
    if size_mb > MAX_INPUT_MB:
        await update.message.reply_text(
            f"File is too large ({size_mb:.0f} MB). Limit is {MAX_INPUT_MB} MB."
        )
        return

    msg = await update.message.reply_text(
        f"Downloading {fname} ({size_mb:.1f} MB)..."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        jnx_path = Path(tmpdir) / fname
        out_name = Path(fname).stem + '.mbtiles'
        out_path = Path(tmpdir) / out_name

        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(jnx_path)

            await msg.edit_text(f"Converting {fname}...")

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, _run_convert, str(jnx_path), str(out_path))
            except SystemExit as exc:
                raise RuntimeError(f"Conversion failed (jnx2mbtiles exited with code {exc.code})") from exc

            out_mb = out_path.stat().st_size / 1024 / 1024
            await msg.edit_text(f"Uploading {out_name} ({out_mb:.1f} MB)...")

            with out_path.open('rb') as fh:
                await update.message.reply_document(document=fh, filename=out_name)

            await msg.delete()
            logger.info("Converted %s (%.1f MB) -> %s (%.1f MB)", fname, size_mb, out_name, out_mb)

        except Exception as exc:
            logger.exception("Failed processing %s", fname)
            await msg.edit_text(f"Error: {exc}")


def _run_convert(jnx_path: str, out_path: str) -> None:
    """Runs in a thread pool so the asyncio event loop stays responsive."""
    jnx2mbtiles.VERBOSE = False
    jnx2mbtiles.convert(jnx_path, out_path, name=None, projection=None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    builder = Application.builder().token(BOT_TOKEN)
    if LOCAL_API_URL:
        builder = builder.base_url(LOCAL_API_URL)
        logger.info("Using local Bot API server: %s", LOCAL_API_URL)
    else:
        logger.warning(
            "LOCAL_API_URL not set — standard Bot API is used (download limit 20 MB, upload limit 50 MB)"
        )

    app = builder.build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot polling started")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
