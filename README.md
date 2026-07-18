# jnx2mbtiles

Converts Garmin BirdsEye raster maps (`.jnx`) to [MBTiles](https://github.com/mapbox/mbtiles-spec) (`.mbtiles` / SQLite), ready to use in any MBTiles-compatible viewer (Gurumaps, AlpineQuest, OsmAnd, MAPS.ME, MapTiler, QGIS, etc.).

Includes a **Telegram bot** that accepts a `.jnx` file directly in chat and replies with the converted `.mbtiles` — no desktop needed.

## Features

- **Auto-detects tile projection** from file contents — no guessing by filename
- **Two projection paths:**
  - **Spherical Web Mercator (EPSG:3857)** — Google, OSM, Bing tiles → fast copy, no pixel resampling
  - **Ellipsoidal Mercator (WGS-84)** — Yandex Maps and similar → tiles are reprojected to EPSG:3857 on the fly using NumPy + Pillow (no GDAL required)
- **Low memory footprint** — ellipsoidal reprojection uses a sliding tile cache (1–2 source tile rows at a time), peak RAM ~30–40 MB regardless of zoom level. Tested on z17 with 4500 tiles on a 512 MB VPS
- **Telegram bot** — send `.jnx`, receive `.mbtiles`. Live progress bar with elapsed time and RAM/swap usage. Optional HTTP download link for large files
- No external C libraries — pure Python + NumPy + Pillow

## Requirements

- Python 3.9+
- `numpy >= 1.24`
- `Pillow >= 9.0`

```bash
pip install -r requirements.txt
```

## CLI usage

```bash
python3 jnx2mbtiles.py <input.jnx> [output.mbtiles] [--name "Map Name"] [--projection spherical|ellipsoidal] [--verbose]
```

### Arguments

| Argument | Description |
|---|---|
| `input.jnx` | Source Garmin JNX file |
| `output.mbtiles` | *(optional)* Destination MBTiles file (must not exist). Defaults to `input.mbtiles` |
| `--name "..."` | Override the map name stored in metadata |
| `--projection` | Force a projection instead of auto-detecting: `spherical` or `ellipsoidal` |
| `--verbose`, `-v` | Print extra detail (mosaic dimensions, etc.) |

### Examples

```bash
# Auto-detect projection
python3 jnx2mbtiles.py map.jnx

# Explicit output filename
python3 jnx2mbtiles.py map.jnx map.mbtiles

# Force ellipsoidal (Yandex Maps)
python3 jnx2mbtiles.py yandex_sat.jnx yandex_sat.mbtiles --projection ellipsoidal --name "Yandex Satellite"

# Verbose output
python3 jnx2mbtiles.py map.jnx -v
```

## Telegram bot

The bot runs on a VPS and handles the full pipeline from your phone:

```
Send .jnx  →  bot downloads  →  converts  →  sends .mbtiles back
                                              + HTTP download link (optional)
```

During conversion the bot updates the message in real time:

```
Конвертирую yandex_z17.jnx (172.0 МБ)…
перепроекция [████████████░░░░░░░░] 60%
⏱ 01:23  RAM 187 МБ  swap 0 МБ
```

Files larger than 50 MB require a local [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server (lifts the limit to 2 GB). See **[BOT.md](BOT.md)** for a full step-by-step VPS deployment guide.

### Bot environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Token from [@BotFather](https://t.me/BotFather) |
| `LOCAL_API_URL` | no | Local Bot API server, e.g. `http://localhost:8081`. Without it the file size limit is 50 MB |
| `VPS_URL` | no | Public base URL, e.g. `http://1.2.3.4:8080`. When set, bot also sends an HTTP download link |
| `HTTP_PORT` | no | Port for the download server (default: `8080`) |
| `EXPIRE_HOURS` | no | How long the download link stays alive (default: `2`) |

## How it works

### JNX format

JNX is a proprietary Garmin raster tile format used by BirdsEye satellite imagery. Each file contains multiple zoom levels; each level stores a list of tiles with geographic bounding boxes (in semicircles) and raw JPEG data.

### Projection detection

The script checks both projections by computing the expected tile grid coordinates for every tile's corner and measuring how far off they are from integer values. The projection with the smallest residual wins. A tolerance of 0.03 tiles is used to accept a match.

If neither projection fits (e.g. tiles cut on an irregular grid), the script exits with an error and suggests using a full GDAL pipeline.

### Reprojection (ellipsoidal → EPSG:3857)

The ellipsoidal (WGS-84) Mercator formula differs from spherical Mercator only in the latitude axis. The conversion iteratively solves the inverse ellipsoidal Mercator formula (8 Newton iterations) to map output pixel rows back to geographic latitudes, then re-samples with bilinear interpolation.

Memory is kept flat by using a sliding tile cache: for each output strip (one row of 256-px output tiles), only the 1–2 source tile rows that overlap it are decoded and held in RAM. Processed rows are evicted before loading the next ones. Peak allocation is proportional to map width, not total tile count.

Output tiles are JPEG at quality 88. The output grid is always EPSG:3857 (TMS scheme).

## Output format

The resulting `.mbtiles` file follows the [MBTiles 1.1 spec](https://github.com/mapbox/mbtiles-spec/blob/master/1.1/spec.md):

- Tile rows use TMS (Y=0 at south)
- Tile format: `jpg`
- Metadata: `name`, `type`, `version`, `description`, `format`, `minzoom`, `maxzoom`

## License

Reconstructed JNX parsing based on [jnxutil](https://github.com/apr2504/jnxutil) (GPLv3).
