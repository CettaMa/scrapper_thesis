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

RTX 3060 Laptop can use `hevc_nvenc` and `h264_nvenc`, but not AV1 NVENC encode. Keep:

```env
ARCHIVE_VIDEO_ENCODER=hevc_nvenc
```

## Data And Config

The image does not bake `.env` or `dataset` into the container.

Runtime mounts:

```text
./dataset -> /app/dataset
./cctv_points.csv -> /app/cctv_points.csv
./secrets -> /app/secrets
```

Runtime env:

```text
.env -> container environment
TZ=Asia/Jakarta -> container timezone
```

## Google Drive Upload

1. Create a Google Cloud service account.
2. Download its JSON key.
3. Put the key at:

```text
secrets/google-service-account.json
```

4. Share the target Google Drive folder with the service account email.
5. Put the target folder ID into `.env`:

```env
GOOGLE_DRIVE_UPLOAD_ENABLED=true
GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE=/app/secrets/google-service-account.json
GOOGLE_DRIVE_FOLDER_ID=your_drive_folder_id
GOOGLE_DRIVE_DELETE_LOCAL_AFTER_UPLOAD=false
```

Uploaded raw `.ts` files are placed in Drive as:

```text
<root folder>/<date>/<camera>/videos/<file>.ts
```

If `GOOGLE_DRIVE_DELETE_LOCAL_AFTER_UPLOAD=false`, local `.ts` files stay on disk and a `.uploaded` marker prevents duplicate uploads.
