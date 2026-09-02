#!/usr/bin/env bash
set -Eeuo pipefail

DEVICE="${VAAPI_DEVICE:-/dev/dri/renderD128}"
OUTPUT="${VAAPI_TEST_OUTPUT:-/tmp/test_vaapi.mp4}"

if [[ ! -e "$DEVICE" ]]; then
  echo "VA-API device not found: $DEVICE" >&2
  echo "Mount /dev/dri and verify the host Intel GPU driver is loaded." >&2
  exit 2
fi

echo "VA-API device: $DEVICE"
echo "LIBVA_DRIVER_NAME=${LIBVA_DRIVER_NAME:-}"
echo "LIBVA_DRIVERS_PATH=${LIBVA_DRIVERS_PATH:-}"

vainfo --display drm --device "$DEVICE"

ffmpeg -hide_banner -loglevel warning -y \
  -vaapi_device "$DEVICE" \
  -f lavfi -i "testsrc=size=1920x1080:rate=30" \
  -vf "format=nv12,hwupload" \
  -c:v h264_vaapi \
  -t 5 \
  "$OUTPUT"

[[ -s "$OUTPUT" ]]
echo "VA-API encode succeeded: $OUTPUT ($(stat -c '%s' "$OUTPUT") bytes)"
