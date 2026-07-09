# jnx2mbtiles

Converts Garmin BirdsEye raster maps (`.jnx`) to [MBTiles](https://github.com/mapbox/mbtiles-spec) (`.mbtiles` / SQLite), ready to use in any MBTiles-compatible viewer (Gurumaps, AlpineQuest, OsmAnd, MAPS.ME, MapTiler, QGIS, etc.).

## Features

- **Auto-detects tile projection** from file contents — no guessing by filename
- **Two projection paths:**
  - **Spherical Web Mercator (EPSG:3857)** — Google, OSM, Bing tiles → fast copy, no pixel resampling
  - **Ellipsoidal Mercator (WGS-84)** — Yandex Maps and similar → tiles are reprojected to EPSG:3857 on the fly using NumPy + Pillow (no GDAL required)
- Memory-efficient: reprojection is done in strips (one tile row at a time), so large maps at zoom 16+ don't exhaust RAM
- Progress bar for every processing stage
- No external C libraries — pure Python + NumPy + Pillow

## Requirements

- Python 3.9+
- `numpy >= 1.24`
- `Pillow >= 9.0`

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 jnx2mbtiles.py <input.jnx> [output.mbtiles] [--name "Map Name"] [--projection spherical|ellipsoidal] [--verbose]
```

### Arguments

| Argument | Description |
|---|---|
| `input.jnx` | Source Garmin JNX file |
| `output.mbtiles` | *(optional)* Destination MBTiles file (must not exist). If omitted, defaults to `input.mbtiles` — same name as the input file with the extension replaced |
| `--name "..."` | Override the map name stored in metadata (defaults to the name embedded in the JNX) |
| `--projection` | Force a specific projection instead of auto-detecting: `spherical` or `ellipsoidal` |
| `--verbose`, `-v` | Print extra detail (mosaic size, memory usage, etc.) |

### Examples

```bash
# Auto-detect projection, output defaults to map.mbtiles
python3 jnx2mbtiles.py map.jnx

# Explicit output filename
python3 jnx2mbtiles.py map.jnx map.mbtiles

# Force ellipsoidal (Yandex Maps) projection
python3 jnx2mbtiles.py yandex_sat.jnx yandex_sat.mbtiles --projection ellipsoidal --name "Yandex Satellite"

# Verbose output
python3 jnx2mbtiles.py map.jnx -v
```

## How it works

### JNX format

JNX is a proprietary Garmin raster tile format used by BirdsEye satellite imagery. Each file contains multiple zoom levels; each level stores a list of tiles with geographic bounding boxes (in semicircles) and raw JPEG data.

### Projection detection

The script checks both projections by computing the expected tile grid coordinates for every tile's corner coordinates and measuring how far off they are from integer values. The projection that produces the smallest residual wins. A tolerance of 0.03 tiles is used to accept a match.

If neither projection fits (e.g. tiles cut on an irregular grid), the script exits with an error and suggests using a full GDAL pipeline.

### Reprojection (ellipsoidal → EPSG:3857)

The ellipsoidal (WGS-84) Mercator formula differs from spherical Mercator only in the latitude axis. The conversion iteratively solves the inverse ellipsoidal Mercator formula (8 Newton iterations) to map output pixel rows back to geographic latitudes, then re-samples the source mosaic with bilinear interpolation row-by-row.

Output tiles are JPEG at quality 88. The output grid is always EPSG:3857 (TMS scheme), compatible with all standard tile consumers.

## Output format

The resulting `.mbtiles` file follows the [MBTiles 1.1 spec](https://github.com/mapbox/mbtiles-spec/blob/master/1.1/spec.md):

- Tile rows use TMS (Y=0 at south)
- Tile format: `jpg`
- Metadata: `name`, `type`, `version`, `description`, `format`, `minzoom`, `maxzoom`

## License

Reconstructed JNX parsing based on [jnxutil](https://github.com/apr2504/jnxutil) (GPLv3).
