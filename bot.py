#!/usr/bin/env python3
"""
Telegram bot + built-in HTTP server for large file transfer.

Flow:
  1. User sends /convert
  2. Bot replies with a curl upload URL (unique token)
  3. User uploads .jnx directly to VPS via curl/wget — no Telegram size limits
  4. Server converts, bot sends download link

Environment variables:
  BOT_TOKEN      (required) Telegram bot token from @BotFather
  VPS_URL        (required) Public base URL, e.g. http://1.2.3.4:8080
  HTTP_PORT      (optional) Port for the built-in HTTP server, default 8080
  EXPIRE_HOURS   (optional) How long download links stay alive, default 2
"""

import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import jnx2mbtiles

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ['BOT_TOKEN']
VPS_URL = os.environ['VPS_URL'].rstrip('/')      # e.g. http://1.2.3.4:8080
HTTP_PORT = int(os.environ.get('HTTP_PORT', 8080))
EXPIRE_SECONDS = int(os.environ.get('EXPIRE_HOURS', 2)) * 3600
UPLOAD_TIMEOUT = 15 * 60  # 15 min to start the upload

# token -> {'chat_id', 'created', 'jnx_path', 'out_path', 'status'}
_sessions: dict[str, dict] = {}
_tmpdir = tempfile.mkdtemp(prefix='jnx2mb_')
_bot_app: Application | None = None


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def http_upload(request: web.Request) -> web.Response:
    token = request.match_info['token']
    filename = request.match_info.get('filename', f'{token[:8]}.jnx')

    session = _sessions.get(token)
    if not session:
        return web.Response(status=404, text='Unknown or expired token\n')
    if session['status'] != 'waiting':
        return web.Response(status=409, text='Already uploaded\n')

    jnx_path: Path = session['jnx_path']
    jnx_path = jnx_path.parent / filename  # use real filename if provided in URL
    session['jnx_path'] = jnx_path
    session['out_path'] = jnx_path.parent / (jnx_path.stem + '.mbtiles')

    logger.info('Receiving upload for token %s (%s)', token, filename)
    with open(jnx_path, 'wb') as fh:
        while chunk := await request.content.read(1024 * 1024):
            fh.write(chunk)

    size_mb = jnx_path.stat().st_size / 1024 / 1024
    logger.info('Upload complete: %.1f MB', size_mb)

    session['status'] = 'converting'
    asyncio.get_event_loop().create_task(_convert_and_notify(token))

    return web.Response(text=f'Received {size_mb:.1f} MB. Conversion started — check Telegram.\n')


async def http_download(request: web.Request) -> web.Response:
    token = request.match_info['token']
    session = _sessions.get(token)

    if not session:
        return web.Response(status=404, text='Unknown or expired token\n')
    if session['status'] != 'done':
        return web.Response(status=425, text=f"Not ready yet (status: {session['status']})\n")

    out_path: Path = session['out_path']
    if not out_path.exists():
        return web.Response(status=410, text='File already deleted\n')

    return web.FileResponse(
        out_path,
        headers={'Content-Disposition': f'attachment; filename="{out_path.name}"'},
    )


# ---------------------------------------------------------------------------
# Conversion + Telegram notification
# ---------------------------------------------------------------------------

async def _convert_and_notify(token: str) -> None:
    session = _sessions[token]
    chat_id = session['chat_id']
    jnx_path: Path = session['jnx_path']
    out_path: Path = session['out_path']

    bot = _bot_app.bot
    msg = await bot.send_message(chat_id, f'Converting {jnx_path.name}...')

    try:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _run_convert, str(jnx_path), str(out_path))
        except SystemExit as exc:
            raise RuntimeError(f'jnx2mbtiles exited with code {exc.code}') from exc

        session['status'] = 'done'
        out_mb = out_path.stat().st_size / 1024 / 1024
        dl_url = f'{VPS_URL}/download/{token}/{out_path.name}'
        expire_h = EXPIRE_SECONDS // 3600

        await msg.edit_text(
            f'Done\\! {jnx_path.name} → {out_path.name} \\({out_mb:.1f} MB\\)\n\n'
            f'Download \\(link valid {expire_h}h\\):\n{dl_url}\n\n'
            f'via curl:\n`curl -OJ {dl_url}`',
            parse_mode='MarkdownV2',
        )
        logger.info('Done: %s → %.1f MB', out_path.name, out_mb)
        loop.call_later(EXPIRE_SECONDS, _cleanup, token)

    except Exception as exc:
        session['status'] = 'error'
        logger.exception('Conversion failed for token %s', token)
        await msg.edit_text(f'Error: {exc}')
        _cleanup(token)


def _run_convert(jnx_path: str, out_path: str) -> None:
    jnx2mbtiles.VERBOSE = False
    jnx2mbtiles.convert(jnx_path, out_path, name=None, projection=None)


def _cleanup(token: str) -> None:
    session = _sessions.pop(token, None)
    if not session:
        return
    for key in ('jnx_path', 'out_path'):
        p: Path = session.get(key)
        if p and p.exists():
            p.unlink(missing_ok=True)
    logger.info('Cleaned up token %s', token)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        'Send /convert to get an upload link for your .jnx file.\n\n'
        'Files go directly to the server — no Telegram size limits.'
    )


async def cmd_convert(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    token = uuid.uuid4().hex
    stem = token[:8]

    _sessions[token] = {
        'chat_id': update.effective_chat.id,
        'created': time.time(),
        'jnx_path': Path(_tmpdir) / f'{stem}.jnx',
        'out_path': Path(_tmpdir) / f'{stem}.mbtiles',
        'status': 'waiting',
    }

    asyncio.get_event_loop().call_later(UPLOAD_TIMEOUT, _expire_if_waiting, token)

    upload_url = f'{VPS_URL}/upload/{token}'
    await update.message.reply_text(
        f'Upload your .jnx file (link valid 15 min):\n\n'
        f'curl:\n`curl -T yourfile.jnx {upload_url}/yourfile.jnx`\n\n'
        f'wget:\n`wget --method=PUT --body-file=yourfile.jnx {upload_url}/yourfile.jnx`\n\n'
        f"You'll get a download link here when it's done.",
        parse_mode='Markdown',
    )


def _expire_if_waiting(token: str) -> None:
    if _sessions.get(token, {}).get('status') == 'waiting':
        logger.info('Upload slot expired: %s', token)
        _cleanup(token)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_all() -> None:
    global _bot_app

    http_app = web.Application(client_max_size=4 * 1024 ** 3)
    http_app.router.add_put('/upload/{token}/{filename}', http_upload)
    http_app.router.add_post('/upload/{token}/{filename}', http_upload)
    http_app.router.add_put('/upload/{token}', http_upload)
    http_app.router.add_post('/upload/{token}', http_upload)
    http_app.router.add_get('/download/{token}/{filename}', http_download)

    runner = web.AppRunner(http_app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', HTTP_PORT).start()
    logger.info('HTTP server on port %d', HTTP_PORT)

    _bot_app = Application.builder().token(BOT_TOKEN).build()
    _bot_app.add_handler(CommandHandler('start', cmd_start))
    _bot_app.add_handler(CommandHandler('convert', cmd_convert))

    await _bot_app.initialize()
    await _bot_app.start()
    await _bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info('Telegram bot polling started')

    try:
        await asyncio.Event().wait()
    finally:
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(run_all())
