# CCTV 24/7 Scraper

This project records public HLS CCTV streams continuously with FFmpeg, collects TomTom traffic
and Open-Meteo weather metadata, and converts completed raw transport-stream segments into local five-minute MP4 archives.
Completed dataset days are also packaged into verified, uncompressed 7-Zip Store archives for
transfer to a personal computer. The dataset is kept on local disk for thesis analysis; there is
no Google Drive upload component.

## Requirements

- Python 3.10 or newer
- FFmpeg on `PATH` for local runs
- An Intel VA-API/QuickSync device at `/dev/dri/renderD128` for hardware encoding (optional;
  archive encoding falls back to `libx264` when the device probe fails)
- Docker and Docker Compose for the containerized setup

Install the Python package and development tools locally with:

```bash
python -m pip install -e ".[dev]"
```

## Configure camera points

Create `cctv_points.csv` in the project directory. A header is recommended:

```csv
name,url,lat,lon
Camera One,https://example.invalid/live.m3u8,-6.850,107.500
Camera Two,https://example.invalid/other.m3u8,-6.851,107.501
```

`name` becomes the sanitized dataset directory name. `url` must be an HTTP or HTTPS URL. If
`lat` or `lon` is omitted, the configured default coordinates are used. Camera URLs are treated
as credentials by the operator: rotating or expired `memfs` URLs must be replaced in this file.

## Configure `.env`

Copy `.env.example` to `.env` and set the stream, archive, storage, and metadata options you need.
The most important settings are:

```env
CCTV_CONFIG_FILE=cctv_points.csv
CCTV_OUTPUT_ROOT=dataset
FFMPEG_TRANSPORT_MODE=copy
ARCHIVE_ENCODER_ENABLED=true
ARCHIVE_VIDEO_ENCODER=h264_vaapi
ARCHIVE_FALLBACK_ENCODER=libx264
ARCHIVE_VAAPI_DEVICE=/dev/dri/renderD128
ARCHIVE_DELETE_RAW_AFTER_SUCCESS=true
DAILY_ARCHIVE_ENABLED=true
DAILY_ARCHIVE_SCAN_SECONDS=300
DAILY_ARCHIVE_DELETE_SOURCE=false
ARCHIVER_BINARY=7z
TOMTOM_API=your_tomtom_key
```

The Intel-only archive path is probed at startup. If VA-API/QuickSync is unavailable, the
archive worker uses the configured fallback encoder. `ARCHIVE_DELETE_RAW_AFTER_SUCCESS=false`
retains both raw segments and MP4 archives, which is useful while validating a deployment.

`ARCHIVE_RETRY_*` and `ARCHIVE_MAX_ATTEMPTS` control persistent retry markers for failed archive
windows. `RETENTION_DAYS`, `MIN_FREE_SPACE_GB`, `LOG_MAX_BYTES`, and `LOG_BACKUP_COUNT` control
storage protection. See `.env.example` for all supported variables.

## Run locally

With `.env` and `cctv_points.csv` present:

```bash
python main.py
```

The CLI flags remain available and override their corresponding environment values:

```bash
python main.py --config cctv_points.csv --output dataset \
  --segment-seconds 60 --retention-days 7 --min-free-space-gb 20 --video-container ts
```

Stop with `Ctrl-C`. FFmpeg recording, metadata collection, archive encoding, disk monitoring,
and status reporting run as separate workers.

## Run with Docker

The Compose service mounts the local dataset and camera configuration into the container:

```bash
# Set the host device group IDs in the environment used by Compose.
export RENDER_GID=$(getent group render | cut -d: -f3)
export VIDEO_GID=$(getent group video | cut -d: -f3)

docker compose build
docker compose up -d
docker compose logs -f
```

On a host without an Intel `/dev/dri` device, the recorder can still start in copy mode and the
archive worker uses `libx264`. On an Intel host, Compose passes `/dev/dri` through and supplies the
host-specific render/video group IDs. Do not hardcode those IDs in the image.

Stop the service with:

```bash
docker compose down
```

## Dataset layout

```text
dataset/
├── YYYY-MM-DD/
│   └── <camera-name>/
│       ├── videos/
│       │   └── <camera>_YYYYMMDD_HHMMSS.ts
│       ├── videos_encoded/
│       │   ├── <camera>_YYYYMMDD_HHMMSS_HHMMSS.mp4
│       │   └── .<archive>.mp4.manifest.json
│       └── metadata/
│           └── <camera>_YYYY-MM-DD_metadata.csv
├── archives/
│   └── YYYY-MM-DD.7z
├── logs/
│   ├── scraper.log
│   ├── <camera>.log
│   └── ffmpeg/<camera>_YYYY-MM-DD.ffmpeg.log
├── status/
│   └── <camera>_status.csv
└── ...
```

Archive manifests record the source window, segment count, covered duration, and encoder actually
used. Failed archive windows use hidden `.failure.json` markers beside the intended archive and
retain their raw `.ts` files. After a date changes, the daily archiver packages the complete
previous date as `archives/YYYY-MM-DD.7z` using 7-Zip Store (`-mx=0`), verifies it with `7z t`,
and retries it on the next scan if needed. Source directories are retained by default; set
`DAILY_ARCHIVE_DELETE_SOURCE=true` only if the verified archive should replace the source.
Logs and status files are retained according to `RETENTION_DAYS`;
application and per-camera logs also rotate by size.

Metadata CSVs retain the traffic and weather cache-status columns (`fresh`, `cached`, and
`fresh_fallback`) used by downstream analysis. The recorder status CSV records online/offline
transitions and emits `EXPIRED_URL_ESCALATION` when an expired URL remains unavailable for the
configured number of consecutive preflight checks.
