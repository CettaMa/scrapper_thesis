# ---------------------------------------------------------------------------
# Stage 1: Build dependencies inside a virtual environment
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Minimal runtime image with FFmpeg and optional Intel QuickSync support
# ---------------------------------------------------------------------------
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Jakarta \
    LIBVA_DRIVER_NAME=iHD \
    LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        intel-media-va-driver \
        libva-drm2 \
        libva2 \
        vainfo \
        python3 \
        tzdata \
        tini \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

COPY main.py ./
COPY cctv_scraper/ ./cctv_scraper/
COPY scripts/test_vaapi.sh /usr/local/bin/test-vaapi
COPY cctv_points.csv ./

RUN chmod +x /usr/local/bin/test-vaapi \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/dataset \
    && chown -R appuser:appuser /app

USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "main.py"]
