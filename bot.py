#!/usr/bin/env python3
"""
Telegram bot for jnx → mbtiles conversion.
Send a .jnx file — get a .mbtiles file back.

Requires a local Telegram Bot API server (LOCAL_API_URL) to lift the 50 MB limit.

Environment variables:
  BOT_TOKEN      (required) Telegram bot token from @BotFather
  LOCAL_API_URL  (optional) Local Bot API server, e.g. http://localhost:8081
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import jnx2mbtiles

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ['BOT_TOKEN']
LOCAL_API_URL = os.environ.get('LOCAL_API_URL', '').rstrip('/')

_tmpdir = tempfile.mkdtemp(prefix='jnx2mb_')


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Пришлите .jnx файл — получите .mbtiles.')


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith('.jnx'):
        await update.message.reply_text('Нужен файл с расширением .jnx')
        return

    msg = await update.message.reply_text(f'Получаю {doc.file_name}…')
    jnx_path = Path(_tmpdir) / doc.file_name
    out_path = jnx_path.with_suffix('.mbtiles')

    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(jnx_path)

        size_mb = jnx_path.stat().st_size / 1024 / 1024
        await msg.edit_text(f'Конвертирую {doc.file_name} ({size_mb:.1f} МБ)…')

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _run_convert, str(jnx_path), str(out_path))
        except SystemExit as exc:
            raise RuntimeError(f'jnx2mbtiles завершился с кодом {exc.code}') from exc

        out_mb = out_path.stat().st_size / 1024 / 1024
        await msg.edit_text(f'Отправляю {out_path.name} ({out_mb:.1f} МБ)…')

        with open(out_path, 'rb') as fh:
            await ctx.bot.send_document(
                chat_id=update.effective_chat.id,
                document=fh,
                filename=out_path.name,
            )
        await msg.delete()
        logger.info('Done: %s → %s (%.1f MB)', doc.file_name, out_path.name, out_mb)

    except Exception as exc:
        logger.exception('Failed for %s', doc.file_name)
        await msg.edit_text(f'Ошибка: {exc}')
    finally:
        jnx_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def _run_convert(jnx_path: str, out_path: str) -> None:
    jnx2mbtiles.VERBOSE = False
    jnx2mbtiles.convert(jnx_path, out_path, name=None, projection=None)


async def run_all() -> None:
    builder = Application.builder().token(BOT_TOKEN)
    if LOCAL_API_URL:
        builder = builder.base_url(f'{LOCAL_API_URL}/bot').local_mode(True)
        logger.info('Using local Bot API server: %s', LOCAL_API_URL)

    builder = builder.read_timeout(120).write_timeout(120).connect_timeout(30).pool_timeout(120)

    app = builder.build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info('Bot polling started')

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == '__main__':
    asyncio.run(run_all())
