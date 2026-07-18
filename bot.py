#!/usr/bin/env python3
"""
Telegram bot for jnx → mbtiles conversion.
Send a .jnx file — get a .mbtiles file back via Telegram + optional HTTP download link.

Requires a local Telegram Bot API server (LOCAL_API_URL) to lift the 50 MB limit.

Environment variables:
  BOT_TOKEN      (required) Telegram bot token from @BotFather
  LOCAL_API_URL  (optional) Local Bot API server, e.g. http://localhost:8081
  VPS_URL        (optional) Public base URL for download links, e.g. http://1.2.3.4:8080
  HTTP_PORT      (optional) Port for the download HTTP server, default 8080
  EXPIRE_HOURS   (optional) How long download links stay alive, default 2
"""

import asyncio
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from aiohttp import web
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
VPS_URL = os.environ.get('VPS_URL', '').rstrip('/')
HTTP_PORT = int(os.environ.get('HTTP_PORT', 8080))
EXPIRE_SECONDS = int(os.environ.get('EXPIRE_HOURS', 2)) * 3600
EXPIRE_HOURS = EXPIRE_SECONDS // 3600

_tmpdir = tempfile.mkdtemp(prefix='jnx2mb_')

# token -> {'out_path': Path, 'expires': float}
_downloads: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# HTTP download server
# ---------------------------------------------------------------------------

async def http_download(request: web.Request) -> web.Response:
    token = request.match_info['token']
    entry = _downloads.get(token)
    if not entry:
        return web.Response(status=404, text='Not found or expired\n')
    if time.time() > entry['expires']:
        _downloads.pop(token, None)
        return web.Response(status=410, text='Link expired\n')
    out_path: Path = entry['out_path']
    if not out_path.exists():
        return web.Response(status=410, text='File already deleted\n')
    return web.FileResponse(
        out_path,
        headers={'Content-Disposition': f'attachment; filename="{out_path.name}"'},
    )


def _schedule_download_cleanup(token: str) -> None:
    entry = _downloads.pop(token, None)
    if entry:
        entry['out_path'].unlink(missing_ok=True)
        logger.info('Cleaned up download token %s', token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc_mem():
    """Возвращает (rss_мб, swap_мб) текущего процесса. Читает /proc/self/status (Linux)."""
    rss = swap = 0.0
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) / 1024
                elif line.startswith('VmSwap:'):
                    swap = int(line.split()[1]) / 1024
    except Exception:
        pass
    return rss, swap


async def _safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

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
        started = time.time()
        _last = [0.0]
        _lock = threading.Lock()
        _level: dict = {}  # zoom, idx, total, projection — заполняется level_hook

        _PROJ_RU = {
            'spherical':    'сферическая (Google/OSM)',
            'ellipsoidal':  'эллипсоидальная (Яндекс)',
        }

        def _on_level(zoom, level_idx, total_levels, projection):
            _level['zoom'] = zoom
            _level['idx'] = level_idx + 1
            _level['total'] = total_levels
            _level['proj'] = _PROJ_RU.get(projection, projection)

        def _on_progress(current, total, prefix=''):
            now = time.time()
            with _lock:
                if now - _last[0] < 3.0:
                    return
                _last[0] = now
            pct = current / total if total else 1.0
            filled = max(1 if pct > 0 else 0, int(20 * pct))
            bar = '█' * filled + '░' * (20 - filled)
            elapsed = int(now - started)
            rss, swap = _proc_mem()
            proj = _level.get('proj', '…')
            zoom = _level.get('zoom', '?')
            lvl_str = f"{_level['idx']}/{_level['total']}" if _level else '…'
            text = (
                f'Конвертирую {doc.file_name} ({size_mb:.1f} МБ)\n'
                f'Проекция: {proj}\n'
                f'z={zoom} [{lvl_str}]  {prefix.strip()} [{bar}] {pct * 100:.0f}%\n'
                f'⏱ {elapsed // 60:02d}:{elapsed % 60:02d}  '
                f'RAM {rss:.0f} МБ  swap {swap:.0f} МБ'
            )
            asyncio.run_coroutine_threadsafe(_safe_edit(msg, text), loop)

        jnx2mbtiles.level_hook = _on_level
        jnx2mbtiles.progress_hook = _on_progress
        try:
            await loop.run_in_executor(None, _run_convert, str(jnx_path), str(out_path))
        except SystemExit as exc:
            raise RuntimeError(f'jnx2mbtiles завершился с кодом {exc.code}') from exc
        finally:
            jnx2mbtiles.level_hook = None
            jnx2mbtiles.progress_hook = None

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

        if VPS_URL:
            token = uuid.uuid4().hex
            _downloads[token] = {'out_path': out_path, 'expires': time.time() + EXPIRE_SECONDS}
            dl_url = f'{VPS_URL}/download/{token}/{out_path.name}'
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f'Ссылка для скачивания (действует {EXPIRE_HOURS}ч):\n`{dl_url}`',
                parse_mode='Markdown',
            )
            loop.call_later(EXPIRE_SECONDS, _schedule_download_cleanup, token)
        else:
            out_path.unlink(missing_ok=True)

    except Exception as exc:
        logger.exception('Failed for %s', doc.file_name)
        await msg.edit_text(f'Ошибка: {exc}')
        out_path.unlink(missing_ok=True)
    finally:
        jnx_path.unlink(missing_ok=True)


def _run_convert(jnx_path: str, out_path: str) -> None:
    jnx2mbtiles.VERBOSE = False
    jnx2mbtiles.convert(jnx_path, out_path, name=None, projection=None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_all() -> None:
    if VPS_URL:
        http_app = web.Application()
        http_app.router.add_get('/download/{token}/{filename}', http_download)
        runner = web.AppRunner(http_app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', HTTP_PORT).start()
        logger.info('HTTP download server on port %d', HTTP_PORT)

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
        if VPS_URL:
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(run_all())
