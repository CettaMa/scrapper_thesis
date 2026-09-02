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

## Intel QuickSync / FFmpeg Check

The image does not contain NVIDIA CUDA or require NVIDIA Container Toolkit. It is based on standard Ubuntu and includes FFmpeg plus Intel media runtime libraries.

Check the available encoders:

```powershell
docker compose run --rm cctv-scraper ffmpeg -hide_banner -encoders
```

The archive configuration uses:

```env
ARCHIVE_VIDEO_ENCODER=h264_vaapi
ARCHIVE_FALLBACK_ENCODER=libx264
ARCHIVE_PRESET=veryfast
```

VA-API requires an Intel GPU/media device. On Linux hosts with Intel graphics, add this device mapping to the service if `/dev/dri` exists:

```yaml
devices:
  - /dev/dri:/dev/dri
```

If VA-API is unavailable, the archive worker automatically retries the affected window with `libx264` instead of losing the recording. A VPS without an Intel media device will therefore use the CPU fallback.

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
