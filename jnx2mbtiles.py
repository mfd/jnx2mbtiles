#!/usr/bin/env python3
"""
jnx2mbtiles.py - конвертирует Garmin .jnx в .mbtiles (SQLite), автоматически
определяя проекцию исходных тайлов и выбирая нужный алгоритм:

  - сферический Web Mercator / EPSG:3857 (Google, OSM, Bing, ...)
      -> быстрый путь: тайлы копируются как есть, без пересчёта пикселей
  - эллипсоидальный Mercator WGS84 (характерно для Яндекс.Карт)
      -> тайлы физически перепроецируются в EPSG:3857 (NumPy + Pillow,
         без GDAL - т.к. между этими проекциями отличается только широта,
         долгота считается одинаково)

Определение идёт по содержимому файла (по факту, какая формула даёт целые
номера тайлов), а не по имени файла - это надёжнее.

Использование:
    python3 jnx2mbtiles.py map.jnx output.mbtiles [--name "My Map"]
    python3 jnx2mbtiles.py map.jnx output.mbtiles --projection ellipsoidal  # принудительно
"""

import argparse
import io
import math
import os
import sqlite3
import struct
import sys

import numpy as np
from PIL import Image

SEMICIRCLE = 0x7FFFFFFF
TILE_SIZE = 256

VERBOSE = False  # управляется флагом --verbose


def progress(current, total, prefix=''):
    """Однострочный прогресс-бар с обновлением через \\r (без доп. зависимостей)."""
    width = 30
    frac = current / total if total else 1.0
    filled = int(width * frac)
    bar = '#' * filled + '-' * (width - filled)
    sys.stdout.write(f'\r{prefix} [{bar}] {current}/{total} ({frac * 100:5.1f}%)')
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write('\n')
        sys.stdout.flush()


def log(msg):
    """Обычное сообщение - печатается всегда (не привязано к прогресс-бару)."""
    print(msg, flush=True)


def log_verbose(msg):
    """Подробное сообщение - только если включён --verbose."""
    if VERBOSE:
        print(msg, flush=True)


# WGS84 ellipsoid (используется, например, Яндекс.Картами - в отличие от
# сферического Web Mercator/EPSG:3857 у Google/OSM/Bing)
_WGS84_F = 1 / 298.257223563
_WGS84_E2 = 2 * _WGS84_F - _WGS84_F * _WGS84_F
_WGS84_E = math.sqrt(_WGS84_E2)


# ---------------------------------------------------------------------------
# Парсинг JNX (реконструировано по проекту jnxutil, apr2504/jnxutil, GPLv3)
# ---------------------------------------------------------------------------

def semicircle_to_deg(v: int) -> float:
    return v * 180.0 / SEMICIRCLE


def read_u32(f):
    return struct.unpack('<I', f.read(4))[0]


def read_i32(f):
    return struct.unpack('<i', f.read(4))[0]


def read_u16(f):
    return struct.unpack('<H', f.read(2))[0]


def read_cstr(f):
    buf = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b'\x00':
            break
        buf += c
    return buf.decode('latin-1', errors='replace')


def parse_jnx(path):
    with open(path, 'rb') as f:
        f.seek(-8, os.SEEK_END)
        if f.read(8) != b'BirdsEye':
            raise ValueError("Не похоже на JNX-файл (нет сигнатуры 'BirdsEye')")
        f.seek(0)

        hdr = {
            'version': read_u32(f), 'devid': read_u32(f),
            'lat1': read_i32(f), 'lon1': read_i32(f),
            'lat2': read_i32(f), 'lon2': read_i32(f),
            'details': read_u32(f), 'expire': read_u32(f),
            'productId': read_i32(f), 'crc': read_u32(f),
            'signature': read_u32(f), 'signature_offset': read_u32(f),
        }
        if hdr['version'] == 4:
            hdr['zorder'] = read_i32(f)

        levels = []
        for _ in range(hdr['details']):
            lvl = {'num_tiles': read_u32(f), 'offset': read_u32(f), 'scale': read_u32(f)}
            if hdr['version'] == 4:
                lvl['unknown'] = read_u32(f)
                lvl['copyright'] = read_cstr(f)
            levels.append(lvl)

        if read_u32(f) != 9:
            raise ValueError("Неожиданный block_version - файл повреждён или неизвестный вариант формата")

        meta = {
            'group_id': read_cstr(f), 'group_name': read_cstr(f),
            'unknown_str': read_cstr(f), 'product_id': read_u16(f),
            'map_name': read_cstr(f),
        }
        read_u32(f)  # details2

        for lvl in levels:
            lvl['level_name'] = read_cstr(f)
            lvl['level_desc'] = read_cstr(f)
            lvl['level_copyright'] = read_cstr(f)
            lvl['level_zoom'] = read_u32(f)

        for lvl in levels:
            f.seek(lvl['offset'])
            tiles = []
            for _ in range(lvl['num_tiles']):
                top, right, bottom, left = struct.unpack('<4i', f.read(16))
                width, height = struct.unpack('<2h', f.read(4))
                size, tile_offset = struct.unpack('<2I', f.read(8))
                tiles.append({
                    'top': semicircle_to_deg(top), 'right': semicircle_to_deg(right),
                    'bottom': semicircle_to_deg(bottom), 'left': semicircle_to_deg(left),
                    'width': width, 'height': height,
                    'size': size, 'offset': tile_offset,
                })
            lvl['tiles'] = tiles

        return hdr, meta, levels


# ---------------------------------------------------------------------------
# Прямые/обратные формулы проекций (в долях тайла, 0..2^z)
# ---------------------------------------------------------------------------

def _tile_x(lon, zoom):
    return (lon + 180.0) / 360.0 * (2.0 ** zoom)


def sph_lat_to_y(lat_deg, zoom):
    lat_rad = math.radians(lat_deg)
    return (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (2.0 ** zoom)


def sph_y_to_lat(y_frac, zoom):
    n = 2.0 ** zoom
    psi = math.pi * (1.0 - 2.0 * y_frac / n)
    return math.degrees(2.0 * math.atan(math.exp(psi)) - math.pi / 2.0)


def ellip_lat_to_y(lat_deg, zoom):
    lat = math.radians(lat_deg)
    s = math.sin(lat)
    es = _WGS84_E * s
    y = 0.5 - (1 / (4 * math.pi)) * math.log((1 + s) / (1 - s) * ((1 - es) / (1 + es)) ** _WGS84_E)
    return y * (2.0 ** zoom)


def ellip_y_to_lat(y_frac, zoom, iterations=8):
    n = 2.0 ** zoom
    psi = math.pi * (1.0 - 2.0 * y_frac / n)
    lat = 2.0 * math.atan(math.exp(psi)) - math.pi / 2.0  # сферическое приближение как старт
    e = _WGS84_E
    for _ in range(iterations):
        lat = 2.0 * math.atan(math.exp(psi + e * math.atanh(e * math.sin(lat)))) - math.pi / 2.0
    return math.degrees(lat)


PROJECTIONS = {
    'spherical': sph_lat_to_y,
    'ellipsoidal': ellip_lat_to_y,
}


def latlon_to_tile_xy(lat, lon, zoom, projection):
    return _tile_x(lon, zoom), PROJECTIONS[projection](lat, zoom)


def detect_projection(levels, tolerance=0.01):
    """Определяет, какая проекция даёт целые номера тайлов для данного файла."""
    best_proj, best_err = None, None
    for proj_name in PROJECTIONS:
        max_err, count = 0.0, 0
        for lvl in levels:
            zoom = lvl['level_zoom']
            for t in lvl['tiles']:
                if t['size'] == 0 or t['width'] != TILE_SIZE or t['height'] != TILE_SIZE:
                    continue
                x_f, y_f = latlon_to_tile_xy(t['top'], t['left'], zoom, proj_name)
                err = max(abs(x_f - round(x_f)), abs(y_f - round(y_f)))
                max_err = max(max_err, err)
                count += 1
        if count and (best_err is None or max_err < best_err):
            best_proj, best_err = proj_name, max_err
    if best_err is not None and best_err <= tolerance:
        return best_proj, best_err
    return None, best_err


# ---------------------------------------------------------------------------
# Сборка тайлов уровня в mbtiles: быстрый путь (spherical) или перепроекция
# ---------------------------------------------------------------------------

def build_level(conn, jnx_path, level, projection, progress_every=200):
    zoom = level['level_zoom']
    tiles = [t for t in level['tiles'] if t['size'] and t['width'] == TILE_SIZE and t['height'] == TILE_SIZE]
    if not tiles:
        return 0
    total_in = len(tiles)
    log(f"Уровень z={zoom}: тайлов на входе {total_in}")

    placed = []
    with open(jnx_path, 'rb') as f:
        for idx, t in enumerate(tiles, 1):
            x_f, y_f = latlon_to_tile_xy(t['top'], t['left'], zoom, projection)
            x, y = round(x_f), round(y_f)
            f.seek(t['offset'])
            jpeg = b'\xff\xd8' + f.read(t['size'])
            placed.append((x, y, jpeg))
            progress(idx, total_in, prefix='  чтение     ')

    if projection == 'spherical':
        # быстрый путь - без ресемплинга, тайлы уже на нужной сетке
        total = 0
        for idx, (x, y, jpeg) in enumerate(placed, 1):
            tms_row = (2 ** zoom - 1) - y
            conn.execute(
                "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                (zoom, x, tms_row, sqlite3.Binary(jpeg))
            )
            total += 1
            progress(idx, len(placed), prefix='  запись     ')
        return total

    # --- нестандартная проекция: собираем мозаику ИСХОДНИКА (uint8, один раз) ---
    xs, ys = [p[0] for p in placed], [p[1] for p in placed]
    min_x, max_x = min(xs), max(xs)
    min_y_src, max_y_src = min(ys), max(ys)

    w_tiles = max_x - min_x + 1
    h_tiles_src = max_y_src - min_y_src + 1
    mosaic = np.zeros((h_tiles_src * TILE_SIZE, w_tiles * TILE_SIZE, 3), dtype=np.uint8)
    mask = np.zeros((h_tiles_src * TILE_SIZE, w_tiles * TILE_SIZE), dtype=bool)

    for idx, (x, y, jpeg) in enumerate(placed, 1):
        img = np.array(Image.open(io.BytesIO(jpeg)).convert('RGB'))
        row0, col0 = (y - min_y_src) * TILE_SIZE, (x - min_x) * TILE_SIZE
        mosaic[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE] = img
        mask[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE] = True
        progress(idx, len(placed), prefix='  сборка     ')
    log_verbose(f"  мозаика {w_tiles}x{h_tiles_src} тайлов, {mosaic.nbytes / 1e6:.0f} МБ")

    lat_north = ellip_y_to_lat(min_y_src, zoom)
    lat_south = ellip_y_to_lat(max_y_src + 1, zoom)
    y_sph_north = sph_lat_to_y(lat_north, zoom)
    y_sph_south = sph_lat_to_y(lat_south, zoom)
    min_y_tgt = math.floor(y_sph_north)
    max_y_tgt = math.ceil(y_sph_south) - 1
    h_tiles_tgt = max_y_tgt - min_y_tgt + 1

    # --- перепроекция ПОЛОСАМИ по 256 строк (=1 ряд выходных тайлов), а не всей
    # мозаикой сразу - иначе временные float32-массивы на больших уровнях (z16+)
    # съедают несколько гигабайт памяти и всё подвисает ---
    total = 0
    for j in range(h_tiles_tgt):
        row_start = j * TILE_SIZE
        out_rows = np.arange(row_start, row_start + TILE_SIZE)
        y_sph_frac = min_y_tgt + out_rows / TILE_SIZE
        lats = [sph_y_to_lat(v, zoom) for v in y_sph_frac]
        y_src_frac = np.array([ellip_lat_to_y(lat, zoom) for lat in lats])
        src_row_f = np.clip((y_src_frac - min_y_src) * TILE_SIZE, 0, mosaic.shape[0] - 1.001)
        row_lo = np.floor(src_row_f).astype(int)
        row_hi = np.clip(row_lo + 1, 0, mosaic.shape[0] - 1)
        frac = (src_row_f - row_lo)[:, None, None]

        band = (mosaic[row_lo].astype(np.float32) * (1 - frac) +
                mosaic[row_hi].astype(np.float32) * frac).astype(np.uint8)
        band_mask = mask[row_lo] | mask[row_hi]

        for i in range(w_tiles):
            block = band[:, i * TILE_SIZE:(i + 1) * TILE_SIZE]
            block_mask = band_mask[:, i * TILE_SIZE:(i + 1) * TILE_SIZE]
            if not block_mask.any():
                continue
            x_abs, y_abs = min_x + i, min_y_tgt + j
            buf = io.BytesIO()
            Image.fromarray(block).save(buf, format='JPEG', quality=88)
            tms_row = (2 ** zoom - 1) - y_abs
            conn.execute(
                "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                (zoom, x_abs, tms_row, sqlite3.Binary(buf.getvalue()))
            )
            total += 1
        progress(j + 1, h_tiles_tgt, prefix='  перепроекция')

    log(f"Уровень z={zoom}: перепроецировано {w_tiles}x{h_tiles_src} -> {w_tiles}x{h_tiles_tgt} тайлов ({total} с данными)")
    return total


# ---------------------------------------------------------------------------
# mbtiles schema + основной конвейер
# ---------------------------------------------------------------------------

def create_schema(conn):
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("""CREATE TABLE tiles (
        zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB
    )""")
    conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")


def convert(jnx_path, out_path, name=None, projection=None):
    hdr, meta, levels = parse_jnx(jnx_path)
    name = name or meta.get('map_name') or os.path.splitext(os.path.basename(jnx_path))[0]
    log(f"Карта: {meta.get('map_name')!r}, версия JNX {hdr['version']}, уровней: {len(levels)}")

    proj_ru = {'spherical': 'сферический Web Mercator (Google/OSM/Bing) - без перепроекции',
               'ellipsoidal': 'эллипсоидальный Mercator WGS84 (напр. Яндекс.Карты) - будет перепроецирован'}

    if projection is None:
        projection, err = detect_projection(levels, tolerance=0.03)
        if projection is None:
            print(f"Не удалось определить проекцию тайлов (невязка {err}).\n"
                  f"Похоже, это JNX с произвольной нарезкой - для него нужен полноценный GDAL-пайплайн.")
            sys.exit(1)
        log(f"Определена проекция тайлов: {proj_ru.get(projection, projection)} (невязка {err:.5f})")
    else:
        log(f"Проекция задана явно: {proj_ru.get(projection, projection)}")

    if os.path.exists(out_path):
        print(f"Файл {out_path} уже существует - удалите его или укажите другое имя.")
        sys.exit(1)

    conn = sqlite3.connect(out_path)
    create_schema(conn)

    total = 0
    minzoom, maxzoom = None, None
    for lvl in levels:
        n = build_level(conn, jnx_path, lvl, projection)
        total += n
        if n:
            z = lvl['level_zoom']
            minzoom = z if minzoom is None else min(minzoom, z)
            maxzoom = z if maxzoom is None else max(maxzoom, z)

    if total == 0:
        conn.close()
        os.remove(out_path)
        print("Не удалось получить ни одного тайла.")
        sys.exit(1)

    meta_kv = {
        'name': name, 'type': 'baselayer', 'version': '1.1',
        'description': name, 'format': 'jpg',
        'minzoom': str(minzoom), 'maxzoom': str(maxzoom),
    }
    for k, v in meta_kv.items():
        conn.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    log(f"Готово: {total} тайлов записано в {out_path} (zoom {minzoom}-{maxzoom}), сетка EPSG:3857")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('jnx_path')
    ap.add_argument('output_mbtiles')
    ap.add_argument('--name', default=None)
    ap.add_argument('--projection', choices=['spherical', 'ellipsoidal'], default=None,
                     help='Задать проекцию явно вместо автоопределения по содержимому файла')
    ap.add_argument('--verbose', '-v', action='store_true',
                     help='Подробный вывод (размер мозаики и т.п.) в дополнение к прогресс-барам')
    args = ap.parse_args()
    VERBOSE = args.verbose
    convert(args.jnx_path, args.output_mbtiles, args.name, args.projection)
