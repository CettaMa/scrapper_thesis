import argparse
import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# =========================================================
# DEFAULT CONFIG
# =========================================================
DEFAULT_CONFIG_FILE = "cctv_points.csv"
DEFAULT_OUTPUT_ROOT = "dataset"

DEFAULT_SEGMENT_SECONDS = 60
DEFAULT_RESTART_DELAY_SECONDS = 5
DEFAULT_HEALTH_CHECK_SECONDS = 30
DEFAULT_STALE_FILE_SECONDS = 240
DEFAULT_METADATA_INTERVAL_SECONDS = 60
DEFAULT_TOMTOM_INTERVAL_SECONDS = 300
DEFAULT_OPENMETEO_INTERVAL_SECONDS = 60
DEFAULT_DISK_CHECK_SECONDS = 300
DEFAULT_HLS_RECONNECT_AT_EOF = False

DEFAULT_RETENTION_DAYS = 7
DEFAULT_MIN_FREE_SPACE_GB = 20

DEFAULT_LAT = "-6.851117"
DEFAULT_LON = "107.496586"

API_TIMEOUT_SECONDS = 10


# =========================================================
# DATA MODEL
# =========================================================
@dataclass(frozen=True)
class CCTVPoint:
    name: str
    url: str
    lat: float
    lon: float


@dataclass
class RuntimeConfig:
    config_file: Path
    output_root: Path
    segment_seconds: int
    restart_delay_seconds: int
    health_check_seconds: int
    stale_file_seconds: int
    metadata_interval_seconds: int
    tomtom_interval_seconds: int
    openmeteo_interval_seconds: int
    disk_check_seconds: int
    retention_days: int
    min_free_space_gb: float
    tomtom_api_key: str | None
    default_lat: float
    default_lon: float

    # New in v2
    video_container: str
    ffmpeg_loglevel: str
    ffmpeg_transport_mode: str
    ffmpeg_user_agent: str
    ffmpeg_referer: str
    ffmpeg_origin: str
    ffmpeg_analyzeduration: str
    ffmpeg_probesize: str
    hls_reconnect_at_eof: bool
    segment_atclocktime: bool
    hls_live_start_index: str
    ffmpeg_rw_timeout: str
    ffmpeg_reconnect_delay_max: str
    ffmpeg_reconnect_on_http_error: str
    output_fps: int
    transcode_preset: str
    transcode_crf: str
    segment_keyframe_seconds: int
    video_encoder: str
    target_bitrate: str
    max_bitrate: str
    buffer_size: str
    output_height: int
    archive_encoder_enabled: bool
    archive_interval_seconds: int
    archive_scan_seconds: int
    archive_safe_age_seconds: int
    archive_delete_raw_after_success: bool
    archive_video_encoder: str
    archive_preset: str
    archive_target_bitrate: str
    archive_max_bitrate: str
    archive_buffer_size: str
    archive_output_height: int
    drive_upload_enabled: bool
    drive_auth_file: Path
    drive_folder_id: str
    drive_scan_seconds: int
    drive_safe_age_seconds: int
    drive_delete_local_after_upload: bool

    # Link health handling
    preflight_check: bool
    offline_retry_seconds: int
    network_retry_seconds: int


# =========================================================
# UTILS
# =========================================================
def sanitize_filename(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-zA-Z0-9_\-]", "", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def now_local() -> datetime:
    return datetime.now()


def safe_float(value: str, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return fallback


def parse_coordinate(value: object, fallback: float) -> float:
    """
    Ambil angka koordinat pertama dari nilai CSV.

    Beberapa baris lama memakai format seperti "107.4967733,237"; bagian
    setelah koma tampaknya bukan bagian longitude, jadi jangan langsung
    jatuh ke fallback default.
    """
    text = str(value).strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if match:
            return safe_float(match.group(0), fallback)
        return fallback


def is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} harus berupa integer.") from exc


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} harus berupa angka.") from exc


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return is_truthy(value)


def validate_positive_number(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} harus lebih besar dari 0.")


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# =========================================================
# CONFIG LOADER
# =========================================================
def load_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    # Load .env for both secrets and runtime options. CLI arguments still take priority.
    load_dotenv()

    config_file = Path(args.config or env_str("CCTV_CONFIG_FILE", DEFAULT_CONFIG_FILE))
    output_root = Path(args.output or env_str("CCTV_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT))

    segment_seconds = args.segment_seconds or env_int("SEGMENT_SECONDS", DEFAULT_SEGMENT_SECONDS)
    retention_days = args.retention_days or env_int("RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    min_free_space_gb = (
        args.min_free_space_gb
        if args.min_free_space_gb is not None
        else env_float("MIN_FREE_SPACE_GB", DEFAULT_MIN_FREE_SPACE_GB)
    )

    video_container = (args.video_container or env_str("VIDEO_CONTAINER", "ts")).lower().strip()
    if video_container not in {"ts", "mp4"}:
        raise ValueError("video_container harus 'ts' atau 'mp4'.")

    # Recommended default for unstable public HLS CCTV:
    # copy = no real-time decode/transcode, lower CPU, and more tolerant for raw recording.
    ffmpeg_transport_mode = env_str("FFMPEG_TRANSPORT_MODE", "copy").lower()
    if ffmpeg_transport_mode not in {"copy", "smooth", "transcode"}:
        raise ValueError("FFMPEG_TRANSPORT_MODE harus 'copy', 'smooth', atau 'transcode'.")

    validate_positive_number("segment_seconds", segment_seconds)
    validate_positive_number("retention_days", retention_days)
    validate_positive_number("min_free_space_gb", min_free_space_gb)

    archive_interval_seconds = env_int("ARCHIVE_INTERVAL_SECONDS", 300)
    archive_scan_seconds = env_int("ARCHIVE_SCAN_SECONDS", 60)
    archive_safe_age_seconds = env_int("ARCHIVE_SAFE_AGE_SECONDS", 90)
    validate_positive_number("ARCHIVE_INTERVAL_SECONDS", archive_interval_seconds)
    validate_positive_number("ARCHIVE_SCAN_SECONDS", archive_scan_seconds)
    validate_positive_number("ARCHIVE_SAFE_AGE_SECONDS", archive_safe_age_seconds)

    drive_scan_seconds = env_int("GOOGLE_DRIVE_SCAN_SECONDS", 60)
    drive_safe_age_seconds = env_int("GOOGLE_DRIVE_SAFE_AGE_SECONDS", 90)
    validate_positive_number("GOOGLE_DRIVE_SCAN_SECONDS", drive_scan_seconds)
    validate_positive_number("GOOGLE_DRIVE_SAFE_AGE_SECONDS", drive_safe_age_seconds)

    return RuntimeConfig(
        config_file=config_file,
        output_root=output_root,
        segment_seconds=segment_seconds,
        restart_delay_seconds=env_int("RESTART_DELAY_SECONDS", DEFAULT_RESTART_DELAY_SECONDS),
        health_check_seconds=env_int("HEALTH_CHECK_SECONDS", DEFAULT_HEALTH_CHECK_SECONDS),
        stale_file_seconds=env_int("STALE_FILE_SECONDS", DEFAULT_STALE_FILE_SECONDS),
        metadata_interval_seconds=env_int(
            "METADATA_INTERVAL_SECONDS", DEFAULT_METADATA_INTERVAL_SECONDS
        ),
        tomtom_interval_seconds=env_int("TOMTOM_INTERVAL_SECONDS", DEFAULT_TOMTOM_INTERVAL_SECONDS),
        openmeteo_interval_seconds=env_int(
            "OPENMETEO_INTERVAL_SECONDS", DEFAULT_OPENMETEO_INTERVAL_SECONDS
        ),
        disk_check_seconds=env_int("DISK_CHECK_SECONDS", DEFAULT_DISK_CHECK_SECONDS),
        retention_days=retention_days,
        min_free_space_gb=min_free_space_gb,
        tomtom_api_key=os.getenv("TOMTOM_API"),
        default_lat=env_float("DEFAULT_LAT", float(DEFAULT_LAT)),
        default_lon=env_float("DEFAULT_LON", float(DEFAULT_LON)),
        video_container=video_container,
        ffmpeg_loglevel=env_str("FFMPEG_LOGLEVEL", "warning"),
        ffmpeg_transport_mode=ffmpeg_transport_mode,
        ffmpeg_user_agent=env_str("FFMPEG_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
        ffmpeg_referer=env_str("FFMPEG_REFERER", ""),
        ffmpeg_origin=env_str("FFMPEG_ORIGIN", ""),
        ffmpeg_analyzeduration=env_str("FFMPEG_ANALYZEDURATION", "10000000"),
        ffmpeg_probesize=env_str("FFMPEG_PROBESIZE", "10000000"),
        hls_reconnect_at_eof=env_bool("HLS_RECONNECT_AT_EOF", DEFAULT_HLS_RECONNECT_AT_EOF),
        # Keep false by default. If true, the first segment after every restart can be short
        # because FFmpeg cuts at the nearest wall-clock boundary.
        segment_atclocktime=env_bool("SEGMENT_ATCLOCKTIME", False),
        # -1 means start from the newest HLS segment to avoid expired playlist entries.
        hls_live_start_index=env_str("HLS_LIVE_START_INDEX", "-1"),
        ffmpeg_rw_timeout=env_str("FFMPEG_RW_TIMEOUT", "10000000"),
        ffmpeg_reconnect_delay_max=env_str("FFMPEG_RECONNECT_DELAY_MAX", "10"),
        # Avoid reconnecting expired live HLS media segments. 4xx segment URLs
        # usually stay expired, so retrying them can prevent FFmpeg from moving on.
        ffmpeg_reconnect_on_http_error=env_str("FFMPEG_RECONNECT_ON_HTTP_ERROR", "5xx"),
        output_fps=env_int("OUTPUT_FPS", 10),
        transcode_preset=env_str("TRANSCODE_PRESET", "p4"),
        transcode_crf=env_str("TRANSCODE_CRF", "25"),
        segment_keyframe_seconds=env_int("SEGMENT_KEYFRAME_SECONDS", 2),
        video_encoder=env_str("VIDEO_ENCODER", "hevc_nvenc"),
        target_bitrate=env_str("TARGET_BITRATE", "650k"),
        max_bitrate=env_str("MAX_BITRATE", "900k"),
        buffer_size=env_str("BUFFER_SIZE", "1300k"),
        output_height=env_int("OUTPUT_HEIGHT", 0),
        archive_encoder_enabled=env_bool("ARCHIVE_ENCODER_ENABLED", True),
        archive_interval_seconds=archive_interval_seconds,
        archive_scan_seconds=archive_scan_seconds,
        archive_safe_age_seconds=archive_safe_age_seconds,
        archive_delete_raw_after_success=env_bool("ARCHIVE_DELETE_RAW_AFTER_SUCCESS", True),
        archive_video_encoder=env_str("ARCHIVE_VIDEO_ENCODER", "hevc_nvenc"),
        archive_preset=env_str("ARCHIVE_PRESET", "p4"),
        archive_target_bitrate=env_str("ARCHIVE_TARGET_BITRATE", "650k"),
        archive_max_bitrate=env_str("ARCHIVE_MAX_BITRATE", "900k"),
        archive_buffer_size=env_str("ARCHIVE_BUFFER_SIZE", "1300k"),
        archive_output_height=env_int("ARCHIVE_OUTPUT_HEIGHT", 0),
        drive_upload_enabled=env_bool("GOOGLE_DRIVE_UPLOAD_ENABLED", False),
        drive_auth_file=Path(env_str("GOOGLE_DRIVE_AUTH_FILE", "secrets/token.json")),
        drive_folder_id=env_str("GOOGLE_DRIVE_FOLDER_ID", ""),
        drive_scan_seconds=drive_scan_seconds,
        drive_safe_age_seconds=drive_safe_age_seconds,
        drive_delete_local_after_upload=env_bool("GOOGLE_DRIVE_DELETE_LOCAL_AFTER_UPLOAD", False),
        preflight_check=env_bool("PREFLIGHT_CHECK", True),
        offline_retry_seconds=env_int("OFFLINE_RETRY_SECONDS", 300),
        network_retry_seconds=env_int("NETWORK_RETRY_SECONDS", 60),
    )


def load_cctv_points(config: RuntimeConfig) -> list[CCTVPoint]:
    path = config.config_file

    if not path.exists():
        raise FileNotFoundError(f"File konfigurasi CCTV tidak ditemukan: {path}")

    points: list[CCTVPoint] = []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)

        has_header = "name" in sample.lower() and (
            "url" in sample.lower() or "link" in sample.lower()
        )

        if has_header:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("name") or row.get("nama") or "").strip()
                url = (row.get("url") or row.get("link") or "").strip()

                if not name or not url:
                    continue

                point_name = sanitize_filename(name)
                if not point_name:
                    continue
                if not is_http_url(url):
                    raise ValueError(f"URL CCTV tidak valid untuk '{name}': {url}")

                lat = row.get("lat") or row.get("latitude") or config.default_lat
                lon = row.get("lon") or row.get("longitude") or config.default_lon

                points.append(
                    CCTVPoint(
                        name=point_name,
                        url=url,
                        lat=parse_coordinate(lat, config.default_lat),
                        lon=parse_coordinate(lon, config.default_lon),
                    )
                )
        else:
            csv_reader = csv.reader(f)
            for raw_row in csv_reader:
                if len(raw_row) < 2:
                    continue

                name = raw_row[0].strip()
                url = raw_row[1].strip()

                if not name or not url:
                    continue

                point_name = sanitize_filename(name)
                if not point_name:
                    continue
                if not is_http_url(url):
                    raise ValueError(f"URL CCTV tidak valid untuk '{name}': {url}")

                points.append(
                    CCTVPoint(
                        name=point_name,
                        url=url,
                        lat=config.default_lat,
                        lon=config.default_lon,
                    )
                )

    if not points:
        raise ValueError("Tidak ada titik CCTV valid di file konfigurasi.")

    seen = set()
    duplicates = []
    for p in points:
        if p.name in seen:
            duplicates.append(p.name)
        seen.add(p.name)

    if duplicates:
        raise ValueError(f"Nama titik CCTV duplikat ditemukan: {sorted(set(duplicates))}")

    return points
