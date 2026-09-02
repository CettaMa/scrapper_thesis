# Docker Usage

## Build

```powershell
docker compose build
```

## Run

```powershell
docker compose up -d
```

After changing Dockerfile settings such as timezone, rebuild first:

```powershell
docker compose build --no-cache
docker compose up -d
```

Logs:

```powershell
docker compose logs -f
```

Stop:

```powershell
docker compose down
```

## NVIDIA GPU / NVENC Check

Install Docker Desktop plus NVIDIA Container Toolkit support first. Then verify FFmpeg sees NVENC inside the image:

```powershell
docker compose run --rm cctv-scraper ffmpeg -hide_banner -encoders
```

Look for:

```text
h264_nvenc
hevc_nvenc
```

Intel QuickSync can use `h264_qsv` or `hevc_qsv` when FFmpeg and the host expose the Intel media device. Use:

```env
ARCHIVE_VIDEO_ENCODER=h264_qsv
ARCHIVE_PRESET=veryfast
```

On Linux Docker hosts, the compose file exposes `/dev/dri`; install a matching Intel media driver on the host and ensure the container user can access the device. On Windows, validate `h264_qsv` with the native FFmpeg installation or Docker Desktop GPU support before deployment. If QuickSync initialization fails, the archive worker retries with `ARCHIVE_FALLBACK_ENCODER` (default `libx264`) instead of losing the window.

## Data And Config

The image does not bake `.env` or `dataset` into the container.

Runtime mounts:

```text
./dataset -> /app/dataset
./cctv_points.csv -> /app/cctv_points.csv
```

Runtime env:

```text
.env -> container environment (local recording configuration only)
TZ=Asia/Jakarta -> container timezone
```

## Local Video Storage

Google Drive uploading has been removed. All recordings remain in the local dataset directory:

```text
dataset/<date>/<camera>/videos/*.ts
dataset/<date>/<camera>/videos_encoded/*.mp4
dataset/<date>/<camera>/metadata/*.csv
```

The Docker compose configuration mounts `./dataset` to `/app/dataset`, so files remain available on the host. Set `ARCHIVE_DELETE_RAW_AFTER_SUCCESS=true` to remove raw `.ts` segments after successful local MP4 archiving, or set it to `false` to retain both formats.
