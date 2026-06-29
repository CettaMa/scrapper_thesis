import argparse
import csv
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
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
    tomtom_api_key: Optional[str]
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
# LOGGING
# =========================================================
def setup_logging(output_root: Path) -> None:
    ensure_dir(output_root)
    ensure_dir(output_root / "logs")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(
        output_root / "logs" / "scraper.log",
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def point_logger(output_root: Path, point_name: str) -> logging.Logger:
    logger = logging.getLogger(f"cctv.{point_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = True

    marker = f"{point_name}.log"
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename.endswith(marker):
            return logger

    ensure_dir(output_root / "logs")
    handler = logging.FileHandler(output_root / "logs" / marker, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger


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
        metadata_interval_seconds=env_int("METADATA_INTERVAL_SECONDS", DEFAULT_METADATA_INTERVAL_SECONDS),
        tomtom_interval_seconds=env_int("TOMTOM_INTERVAL_SECONDS", DEFAULT_TOMTOM_INTERVAL_SECONDS),
        openmeteo_interval_seconds=env_int("OPENMETEO_INTERVAL_SECONDS", DEFAULT_OPENMETEO_INTERVAL_SECONDS),
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

        has_header = "name" in sample.lower() and ("url" in sample.lower() or "link" in sample.lower())

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
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue

                name = row[0].strip()
                url = row[1].strip()

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


# =========================================================
# VIDEO RECORDER
# =========================================================
class CCTVRecorder(threading.Thread):
    def __init__(self, point: CCTVPoint, config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name=f"recorder-{point.name}", daemon=True)
        self.point = point
        self.config = config
        self.stop_event = stop_event
        self.process: Optional[subprocess.Popen] = None
        self.logger = point_logger(config.output_root, point.name)
        self.last_restart_at: Optional[datetime] = None
        self.process_start_date: Optional[datetime.date] = None
        self.ffmpeg_stderr_file = None
        self._ffmpeg_self_exited = False

    def run(self) -> None:
        self.logger.info("Recorder watchdog started.")

        while not self.stop_event.is_set():
            ok, reason, http_status = self.preflight_stream()
            if not ok:
                self.write_status("offline", reason=reason, http_status=http_status)
                self.logger.warning(
                    "Preflight failed for %s | reason=%s | http_status=%s",
                    self.point.name,
                    reason,
                    http_status or "-"
                )
                self.sleep_after_preflight_failure(reason)
                continue

            self.write_status("online_preflight_ok", reason=reason, http_status=http_status)
            self._ffmpeg_self_exited = False
            self.start_ffmpeg()
            self.monitor_ffmpeg()

            if not self.stop_event.is_set():
                self.write_status("recorder_restart", reason="ffmpeg_stopped_or_unhealthy")
                if self._ffmpeg_self_exited:
                    # FFmpeg drop sendiri (biasanya TLS/network). Jeda singkat lalu preflight ulang.
                    self.logger.warning(
                        "FFmpeg dropped on its own. Short delay then re-preflight before restart."
                    )
                    self.stop_event.wait(self.config.restart_delay_seconds)
                    # Loop kembali ke preflight — tidak langsung spawn FFmpeg.
                else:
                    self.logger.warning(
                        "FFmpeg stopped or unhealthy. Restarting in %s seconds.",
                        self.config.restart_delay_seconds
                    )
                    self.stop_event.wait(self.config.restart_delay_seconds)

        self.stop_ffmpeg()
        self.logger.info("Recorder watchdog stopped.")

    def current_date_folder(self) -> str:
        return now_local().strftime("%Y-%m-%d")

    def current_video_dir(self) -> Path:
        video_dir = self.config.output_root / self.current_date_folder() / self.point.name / "videos"
        ensure_dir(video_dir)
        return video_dir

    def stderr_path(self) -> Path:
        ensure_dir(self.config.output_root / "logs" / "ffmpeg")
        date = now_local().strftime("%Y-%m-%d")
        return self.config.output_root / "logs" / "ffmpeg" / f"{self.point.name}_{date}.ffmpeg.log"

    def output_extension(self) -> str:
        return "ts" if self.config.video_container == "ts" else "mp4"

    def build_output_pattern(self) -> str:
        video_dir = self.current_video_dir()
        ext = self.output_extension()
        return str(video_dir / f"{self.point.name}_%Y%m%d_%H%M%S.{ext}")

    def input_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.ffmpeg_user_agent,
        }
        if self.config.ffmpeg_referer:
            headers["Referer"] = self.config.ffmpeg_referer
        if self.config.ffmpeg_origin:
            headers["Origin"] = self.config.ffmpeg_origin
        return headers

    def ffmpeg_headers_arg(self) -> str:
        return "".join(
            f"{key}: {value}\r\n"
            for key, value in self.input_headers().items()
        )

    def build_ffmpeg_command(self) -> list[str]:
        output_pattern = self.build_output_pattern()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", self.config.ffmpeg_loglevel,

            # Input stability for unstable HLS CCTV streams.
            "-fflags", "+genpts+discardcorrupt+nobuffer",
            "-err_detect", "ignore_err",
            "-rw_timeout", self.config.ffmpeg_rw_timeout,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_on_http_error", self.config.ffmpeg_reconnect_on_http_error,
            "-reconnect_delay_max", self.config.ffmpeg_reconnect_delay_max,
            "-http_persistent", "0",
            "-multiple_requests", "0",
            "-user_agent", self.config.ffmpeg_user_agent,
            "-headers", self.ffmpeg_headers_arg(),
            "-analyzeduration", self.config.ffmpeg_analyzeduration,
            "-probesize", self.config.ffmpeg_probesize,
        ]

        if self.config.hls_reconnect_at_eof:
            cmd += ["-reconnect_at_eof", "1"]

        # Start from newest live HLS segment. This reduces "expired from playlists"
        # and short/failed recordings caused by trying to fetch old .ts fragments.
        if self.point.url.lower().split("?")[0].endswith(".m3u8"):
            cmd += ["-live_start_index", self.config.hls_live_start_index]

        cmd += ["-i", self.point.url]

        if self.config.ffmpeg_transport_mode in {"transcode", "smooth"}:
            # CFR mengatasi timestamp stream sumber yang tidak stabil.
            # Bitrate-limited encoding menjaga kebutuhan storage dapat diprediksi.
            fps = max(1, int(self.config.output_fps))
            gop = max(1, fps * max(1, int(self.config.segment_keyframe_seconds)))

            filters = [f"fps={fps}"]
            if self.config.output_height > 0:
                filters.append(f"scale=-2:{self.config.output_height}")
            filters.append("format=yuv420p")

            encoder = self.config.video_encoder
            cmd += [
                "-map", "0:v:0",
                "-an",
                "-vf", ",".join(filters),
                "-fps_mode", "cfr",
                "-c:v", encoder,
            ]

            if encoder in {"h264_nvenc", "hevc_nvenc"}:
                cmd += [
                    "-preset", self.config.transcode_preset,
                    "-rc:v", "vbr",
                    "-b:v", self.config.target_bitrate,
                    "-maxrate:v", self.config.max_bitrate,
                    "-bufsize:v", self.config.buffer_size,
                ]
            elif encoder in {"libx264", "libx265"}:
                cpu_preset = self.config.transcode_preset
                if cpu_preset.startswith("p") and cpu_preset[1:].isdigit():
                    cpu_preset = "veryfast"
                cmd += [
                    "-preset", cpu_preset,
                    "-b:v", self.config.target_bitrate,
                    "-maxrate:v", self.config.max_bitrate,
                    "-bufsize:v", self.config.buffer_size,
                ]
            else:
                raise ValueError(
                    f"VIDEO_ENCODER tidak didukung: {encoder}. "
                    "Gunakan hevc_nvenc, h264_nvenc, libx265, atau libx264."
                )

            cmd += [
                "-g", str(gop),
                "-keyint_min", str(gop),
                "-sc_threshold", "0",
            ]
        else:
            # Recommended raw recording mode: no decode/transcode.
            # This is lighter and usually more stable for 24-hour CCTV capture.
            cmd += ["-map", "0:v:0", "-an", "-c:v", "copy"]

        cmd += ["-max_muxing_queue_size", "1024"]

        cmd += [
            "-f", "segment",
            "-segment_time", str(self.config.segment_seconds),
            "-reset_timestamps", "1",
            "-strftime", "1",
        ]

        # Disabled by default. When enabled, FFmpeg cuts on wall-clock boundaries,
        # so the first segment after a restart can naturally be only 1-59 seconds.
        if self.config.segment_atclocktime:
            cmd += ["-segment_atclocktime", "1"]

        if self.config.video_container == "mp4":
            cmd += [
                "-segment_format", "mp4",
                "-movflags", "+faststart",
            ]
        else:
            cmd += ["-segment_format", "mpegts"]

        cmd += [output_pattern]
        return cmd

    def status_dir(self) -> Path:
        path = self.config.output_root / "status"
        ensure_dir(path)
        return path

    def status_file(self) -> Path:
        return self.status_dir() / f"{self.point.name}_status.csv"

    def write_status(self, status: str, reason: str = "", http_status: str = "") -> None:
        path = self.status_file()
        file_exists = path.exists()
        row = {
            "timestamp": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "cctv_name": self.point.name,
            "status": status,
            "reason": reason,
            "http_status": http_status,
            "url": self.point.url,
        }

        try:
            with path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as exc:
            self.logger.warning("Failed to write status CSV: %s", exc)

    def preflight_stream(self) -> tuple[bool, str, str]:
        """
        Mengecek apakah URL HLS dapat dibuka sebelum menjalankan FFmpeg.
        Tujuannya bukan menggantikan FFmpeg, tetapi mencegah restart loop agresif
        saat URL jelas 404/403 atau DNS sedang bermasalah.
        """
        if not self.config.preflight_check:
            return True, "preflight_disabled", ""

        headers = {
            **self.input_headers(),
            "Accept": "*/*",
        }

        try:
            with requests.Session() as session:
                with session.get(
                    self.point.url,
                    headers=headers,
                    timeout=10,
                    allow_redirects=True,
                    stream=True,
                ) as response:
                    http_status = str(response.status_code)

                    # 2xx berarti playlist dapat dibuka.
                    if 200 <= response.status_code < 300:
                        return True, "ok", http_status

                    # 404/410 biasanya link memfs sudah expired/hilang.
                    if response.status_code in {404, 410}:
                        return False, "not_found_or_expired_url", http_status

                    # 401/403 biasanya perlu header/token/link baru.
                    if response.status_code in {401, 403}:
                        return False, "forbidden_or_unauthorized", http_status

                    # 5xx kemungkinan server sedang bermasalah.
                    if 500 <= response.status_code < 600:
                        return False, "server_error", http_status

                    return False, f"http_{response.status_code}", http_status

        except requests.exceptions.ConnectionError as exc:
            return False, f"connection_error: {exc}", ""

        except requests.exceptions.Timeout:
            return False, "timeout", ""

        except Exception as exc:
            return False, f"preflight_error: {exc}", ""

    def sleep_after_preflight_failure(self, reason: str) -> None:
        if "not_found" in reason or "forbidden" in reason or "unauthorized" in reason:
            delay = self.config.offline_retry_seconds
            self.logger.warning(
                "Stream URL looks offline/expired: %s. Retrying in %s seconds.",
                reason,
                delay
            )
        else:
            delay = self.config.network_retry_seconds
            self.logger.warning(
                "Stream network/server issue: %s. Retrying in %s seconds.",
                reason,
                delay
            )

        self.stop_event.wait(delay)

    def start_ffmpeg(self) -> None:
        command = self.build_ffmpeg_command()
        self.last_restart_at = now_local()
        self.process_start_date = now_local().date()

        stderr_path = self.stderr_path()
        self.ffmpeg_stderr_file = open(stderr_path, "a", encoding="utf-8", buffering=1)

        self.ffmpeg_stderr_file.write("\n" + "=" * 100 + "\n")
        self.ffmpeg_stderr_file.write(f"START {now_local().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.ffmpeg_stderr_file.write("COMMAND:\n")
        self.ffmpeg_stderr_file.write(" ".join(command) + "\n")
        self.ffmpeg_stderr_file.write("=" * 100 + "\n")

        self.logger.info("Starting FFmpeg. stderr log: %s", stderr_path)
        self.logger.info("Output container: %s", self.config.video_container)
        self.logger.info("Transport mode: %s", self.config.ffmpeg_transport_mode)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=self.ffmpeg_stderr_file,
            creationflags=creationflags
        )

    def monitor_ffmpeg(self) -> None:
        while not self.stop_event.is_set():
            if self.process is None:
                return

            return_code = self.process.poll()
            if return_code is not None:
                self.logger.warning("FFmpeg exited unexpectedly with code: %s", return_code)
                self.log_recent_ffmpeg_stderr()
                self.close_ffmpeg_stderr()
                # Tandai bahwa FFmpeg keluar sendiri (bukan di-stop oleh kita).
                self._ffmpeg_self_exited = True
                return

            if self.process_start_date and now_local().date() != self.process_start_date:
                self.logger.info("Date changed. Restarting FFmpeg to switch output folder.")
                self.stop_ffmpeg()
                return

            if self.is_video_stale():
                self.logger.warning(
                    "No recent valid video file detected in the last %s seconds.",
                    self.config.stale_file_seconds
                )
                self.log_recent_ffmpeg_stderr()
                self.stop_ffmpeg()
                return

            self.stop_event.wait(self.config.health_check_seconds)

    def latest_video_file(self) -> Optional[Path]:
        video_root = self.current_video_dir()

        if not video_root.exists():
            return None

        ext = self.output_extension()
        candidates = list(video_root.glob(f"{self.point.name}_*.{ext}"))

        # Jangan mengevaluasi segment lama dari sesi recorder sebelumnya.
        if self.last_restart_at is not None:
            start_ts = self.last_restart_at.timestamp() - 5
            candidates = [
                p for p in candidates
                if p.exists() and p.stat().st_mtime >= start_ts
            ]

        if not candidates:
            return None

        return max(candidates, key=lambda p: p.stat().st_mtime)

    def is_video_stale(self) -> bool:
        latest = self.latest_video_file()

        if latest is None:
            if self.last_restart_at is None:
                return False
            age_since_restart = (now_local() - self.last_restart_at).total_seconds()
            return age_since_restart > self.config.stale_file_seconds

        stat = latest.stat()
        age = time.time() - stat.st_mtime

        # Jangan menilai file yang sedang aktif ditulis.
        if age > self.config.stale_file_seconds and stat.st_size < 100 * 1024:
            self.logger.warning("Latest file is too small: %s | %s bytes", latest, stat.st_size)
            return True

        return age > self.config.stale_file_seconds

    def log_recent_ffmpeg_stderr(self, lines: int = 30) -> None:
        path = self.stderr_path()
        if not path.exists():
            return

        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            recent = content[-lines:]
            if recent:
                self.logger.warning("Recent FFmpeg stderr:")
                for line in recent:
                    self.logger.warning("FFmpeg | %s", line)
        except Exception as exc:
            self.logger.warning("Cannot read FFmpeg stderr log: %s", exc)

    def close_ffmpeg_stderr(self) -> None:
        if self.ffmpeg_stderr_file:
            try:
                self.ffmpeg_stderr_file.flush()
                self.ffmpeg_stderr_file.close()
            except Exception:
                pass
            self.ffmpeg_stderr_file = None

    def stop_ffmpeg(self) -> None:
        if self.process is None:
            self.close_ffmpeg_stderr()
            return

        if self.process.poll() is None:
            self.logger.info("Stopping FFmpeg.")
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.logger.warning("FFmpeg did not terminate. Killing process.")
                self.process.kill()
            except Exception as exc:
                self.logger.warning("Failed to stop FFmpeg cleanly: %s", exc)

        self.process = None
        self.close_ffmpeg_stderr()


# =========================================================
# METADATA COLLECTOR
# =========================================================
class MetadataCollector(threading.Thread):
    """
    Mengambil metadata eksternal per titik.

    v4:
    - TomTom dipanggil default setiap 300 detik / 5 menit.
    - Open-Meteo dipanggil default setiap 60 detik.
    - CSV metadata tetap ditulis setiap METADATA_INTERVAL_SECONDS.
    - Jika belum waktunya call ulang, nilai terakhir dipakai kembali dan diberi status cached.
    """

    def __init__(self, points: list[CCTVPoint], config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="metadata-collector", daemon=True)
        self.points = points
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("metadata")

        self.tomtom_cache: dict[str, dict] = {}
        self.tomtom_last_fetch: dict[str, float] = {}

        self.openmeteo_cache: dict[str, dict] = {}
        self.openmeteo_last_fetch: dict[str, float] = {}

    def run(self) -> None:
        self.logger.info("Metadata collector started.")
        self.logger.info("Metadata CSV write interval: %s seconds", self.config.metadata_interval_seconds)
        self.logger.info("TomTom API interval: %s seconds", self.config.tomtom_interval_seconds)
        self.logger.info("Open-Meteo API interval: %s seconds", self.config.openmeteo_interval_seconds)

        while not self.stop_event.is_set():
            start = time.time()
            timestamp = now_local()

            for point in self.points:
                try:
                    metadata = self.collect_point_metadata(point, timestamp)
                    self.write_metadata(point, metadata)
                except Exception as exc:
                    self.logger.exception("Metadata failed for %s: %s", point.name, exc)

            elapsed = time.time() - start
            sleep_time = max(0, self.config.metadata_interval_seconds - elapsed)
            self.stop_event.wait(sleep_time)

        self.logger.info("Metadata collector stopped.")

    def should_fetch(self, cache_key: str, last_fetch: dict[str, float], interval_seconds: int) -> bool:
        if cache_key not in last_fetch:
            return True

        return (time.time() - last_fetch[cache_key]) >= interval_seconds

    def collect_point_metadata(self, point: CCTVPoint, timestamp: datetime) -> dict:
        with requests.Session() as session:
            tomtom = self.get_tomtom_cached(session, point)
            weather = self.get_openmeteo_cached(session, point)

        row = {
            "cctv_name": point.name,
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "date": timestamp.strftime("%Y-%m-%d"),
            "time": timestamp.strftime("%H:%M:%S"),
            "datestamp": timestamp.strftime("%Y%m%d"),
            "timestampsafe": timestamp.strftime("%H%M%S"),
            "latitude": point.lat,
            "longitude": point.lon,
            "stream_url": point.url,
        }
        row.update(tomtom)
        row.update(weather)
        return row

    def get_tomtom_cached(self, session: requests.Session, point: CCTVPoint) -> dict:
        cache_key = point.name

        if self.should_fetch(cache_key, self.tomtom_last_fetch, self.config.tomtom_interval_seconds):
            data = self.get_tomtom(session, point)
            data["traffic_cache_status"] = "fresh"
            data["traffic_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")

            self.tomtom_cache[cache_key] = data
            self.tomtom_last_fetch[cache_key] = time.time()
            return data

        cached = self.tomtom_cache.get(cache_key)
        if cached:
            data = dict(cached)
            data["traffic_cache_status"] = "cached"
            return data

        # Safety fallback. Normally unreachable because first call should fetch.
        data = self.get_tomtom(session, point)
        data["traffic_cache_status"] = "fresh_fallback"
        data["traffic_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        self.tomtom_cache[cache_key] = data
        self.tomtom_last_fetch[cache_key] = time.time()
        return data

    def get_openmeteo_cached(self, session: requests.Session, point: CCTVPoint) -> dict:
        cache_key = point.name

        if self.should_fetch(cache_key, self.openmeteo_last_fetch, self.config.openmeteo_interval_seconds):
            data = self.get_openmeteo(session, point)
            data["weather_cache_status"] = "fresh"
            data["weather_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")

            self.openmeteo_cache[cache_key] = data
            self.openmeteo_last_fetch[cache_key] = time.time()
            return data

        cached = self.openmeteo_cache.get(cache_key)
        if cached:
            data = dict(cached)
            data["weather_cache_status"] = "cached"
            return data

        data = self.get_openmeteo(session, point)
        data["weather_cache_status"] = "fresh_fallback"
        data["weather_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        self.openmeteo_cache[cache_key] = data
        self.openmeteo_last_fetch[cache_key] = time.time()
        return data

    def get_tomtom(self, session: requests.Session, point: CCTVPoint) -> dict:
        default = {
            "traffic_speed": None,
            "traffic_freeflow": None,
            "traffic_confidence": None,
            "traffic_road_closure": None,
            "traffic_source": "tomtom",
            "traffic_status": "missing_api_key" if not self.config.tomtom_api_key else "error",
        }

        if not self.config.tomtom_api_key:
            return default

        url = (
            "https://api.tomtom.com/traffic/services/4/flowSegmentData/"
            f"absolute/10/json?key={self.config.tomtom_api_key}&point={point.lat},{point.lon}"
        )

        try:
            response = session.get(url, timeout=API_TIMEOUT_SECONDS)
            response.raise_for_status()
            flow = response.json().get("flowSegmentData", {})

            return {
                "traffic_speed": flow.get("currentSpeed"),
                "traffic_freeflow": flow.get("freeFlowSpeed"),
                "traffic_confidence": flow.get("confidence"),
                "traffic_road_closure": flow.get("roadClosure"),
                "traffic_source": "tomtom",
                "traffic_status": "ok",
            }

        except Exception as exc:
            self.logger.warning("TomTom failed for %s: %s", point.name, exc)
            return default

    def get_openmeteo(self, session: requests.Session, point: CCTVPoint) -> dict:
        default = {
            "weather_temp": None,
            "weather_humidity": None,
            "weather_rain": None,
            "weather_wind_speed": None,
            "weather_source": "open-meteo",
            "weather_status": "error",
        }

        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={point.lat}&longitude={point.lon}"
            "&current=temperature_2m,relative_humidity_2m,rain,wind_speed_10m"
            "&timezone=Asia%2FJakarta"
        )

        try:
            response = session.get(url, timeout=API_TIMEOUT_SECONDS)
            response.raise_for_status()
            current = response.json().get("current", {})

            return {
                "weather_temp": current.get("temperature_2m"),
                "weather_humidity": current.get("relative_humidity_2m"),
                "weather_rain": current.get("rain"),
                "weather_wind_speed": current.get("wind_speed_10m"),
                "weather_source": "open-meteo",
                "weather_status": "ok",
            }

        except Exception as exc:
            self.logger.warning("Open-Meteo failed for %s: %s", point.name, exc)
            return default

    def write_metadata(self, point: CCTVPoint, row: dict) -> None:
        date_folder = row["date"]
        metadata_dir = self.config.output_root / date_folder / point.name / "metadata"
        ensure_dir(metadata_dir)

        csv_path = metadata_dir / f"{point.name}_{date_folder}_metadata.csv"
        file_exists = csv_path.exists()

        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

        self.logger.info(
            "Metadata saved: %s | TomTom=%s | OpenMeteo=%s",
            csv_path,
            row.get("traffic_cache_status"),
            row.get("weather_cache_status")
        )


# =========================================================
# DISK MONITOR
# =========================================================
class DiskMonitor(threading.Thread):
    def __init__(self, config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="disk-monitor", daemon=True)
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("disk")

    def run(self) -> None:
        self.logger.info("Disk monitor started.")

        while not self.stop_event.is_set():
            try:
                self.check_disk()
                self.cleanup_old_folders()
            except Exception as exc:
                self.logger.exception("Disk monitor error: %s", exc)

            self.stop_event.wait(self.config.disk_check_seconds)

        self.logger.info("Disk monitor stopped.")

    def check_disk(self) -> None:
        ensure_dir(self.config.output_root)
        usage = shutil.disk_usage(self.config.output_root)
        free_gb = usage.free / (1024 ** 3)

        if free_gb < self.config.min_free_space_gb:
            self.logger.warning(
                "Low disk space: %.2f GB free. Minimum configured: %.2f GB.",
                free_gb,
                self.config.min_free_space_gb
            )
        else:
            self.logger.info("Disk free space: %.2f GB.", free_gb)

    def cleanup_old_folders(self) -> None:
        cutoff_date = now_local().date() - timedelta(days=self.config.retention_days)

        if not self.config.output_root.exists():
            return

        for child in self.config.output_root.iterdir():
            if not child.is_dir():
                continue

            if child.name == "logs":
                continue

            try:
                folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue

            if folder_date < cutoff_date:
                self.logger.warning("Deleting old footage folder: %s", child)
                shutil.rmtree(child, ignore_errors=True)


# =========================================================
# ARCHIVE ENCODER
# =========================================================
class ArchiveEncoder(threading.Thread):
    def __init__(self, points: list[CCTVPoint], config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="archive-encoder", daemon=True)
        self.points = points
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("archive")

    def run(self) -> None:
        if not self.config.archive_encoder_enabled:
            self.logger.info("Archive encoder disabled.")
            return

        self.logger.info("Archive encoder started.")

        while not self.stop_event.is_set():
            try:
                self.encode_ready_batches()
            except Exception as exc:
                self.logger.exception("Archive encoder error: %s", exc)

            self.stop_event.wait(self.config.archive_scan_seconds)

        self.logger.info("Archive encoder stopped.")

    def encode_ready_batches(self) -> None:
        for point in self.points:
            for date_dir in self.date_dirs_for_point(point):
                if self.stop_event.is_set():
                    return
                self.encode_point_date(point, date_dir)

    def date_dirs_for_point(self, point: CCTVPoint) -> list[Path]:
        if not self.config.output_root.exists():
            return []

        dirs: list[Path] = []
        for child in self.config.output_root.iterdir():
            if not child.is_dir():
                continue
            try:
                datetime.strptime(child.name, "%Y-%m-%d")
            except ValueError:
                continue

            point_dir = child / point.name
            if (point_dir / "videos").exists():
                dirs.append(child)

        return sorted(dirs)

    def encode_point_date(self, point: CCTVPoint, date_dir: Path) -> None:
        raw_dir = date_dir / point.name / "videos"
        encoded_dir = date_dir / point.name / "videos_encoded"
        ensure_dir(encoded_dir)

        files = self.ready_raw_files(raw_dir)
        if not files:
            return

        grouped: dict[int, list[Path]] = {}
        for path in files:
            window_start = int(path.stat().st_mtime) // self.config.archive_interval_seconds
            window_start *= self.config.archive_interval_seconds
            window_end = window_start + self.config.archive_interval_seconds
            if time.time() < window_end + self.config.archive_safe_age_seconds:
                continue
            grouped.setdefault(window_start, []).append(path)

        for window_start, batch in sorted(grouped.items()):
            if self.stop_event.is_set():
                return
            self.encode_batch(point, encoded_dir, window_start, sorted(batch))

    def ready_raw_files(self, raw_dir: Path) -> list[Path]:
        if not raw_dir.exists():
            return []

        cutoff = time.time() - self.config.archive_safe_age_seconds
        files: list[Path] = []
        for path in raw_dir.glob("*.ts"):
            try:
                if path.stat().st_size <= 0:
                    continue
                if path.stat().st_mtime > cutoff:
                    continue
                files.append(path)
            except FileNotFoundError:
                continue
        return files

    def encode_batch(self, point: CCTVPoint, encoded_dir: Path, window_start: int, files: list[Path]) -> None:
        if not files:
            return

        start_dt = datetime.fromtimestamp(window_start)
        end_dt = datetime.fromtimestamp(window_start + self.config.archive_interval_seconds)
        output = encoded_dir / (
            f"{point.name}_{start_dt.strftime('%Y%m%d_%H%M%S')}_"
            f"{end_dt.strftime('%H%M%S')}.mp4"
        )

        if output.exists() and output.stat().st_size > 0:
            return

        list_path = encoded_dir / f".{output.stem}.concat.txt"
        tmp_output = encoded_dir / f".{output.name}.tmp.mp4"

        try:
            self.write_concat_file(list_path, files)
            command = self.build_encode_command(list_path, tmp_output)

            self.logger.info(
                "Encoding %s raw segments for %s -> %s",
                len(files),
                point.name,
                output,
            )

            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            if result.returncode != 0:
                self.logger.warning(
                    "Archive encode failed for %s | code=%s | stderr=%s",
                    output,
                    result.returncode,
                    self.trim_stderr(result.stderr),
                )
                return

            if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
                self.logger.warning("Archive encode produced empty output: %s", tmp_output)
                return

            tmp_output.replace(output)
            self.logger.info("Archive saved: %s | bytes=%s", output, output.stat().st_size)

            if self.config.archive_delete_raw_after_success and not self.config.drive_upload_enabled:
                self.delete_raw_files(files)
            elif self.config.archive_delete_raw_after_success and self.config.drive_upload_enabled:
                self.logger.info("Raw segments kept for Google Drive uploader: %s files", len(files))

        finally:
            try:
                list_path.unlink(missing_ok=True)
            except Exception:
                pass
            if tmp_output.exists():
                try:
                    tmp_output.unlink()
                except Exception:
                    pass

    def write_concat_file(self, list_path: Path, files: list[Path]) -> None:
        with list_path.open("w", encoding="utf-8", newline="\n") as f:
            for path in files:
                escaped = path.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

    def build_encode_command(self, list_path: Path, output: Path) -> list[str]:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", self.config.ffmpeg_loglevel,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-an",
        ]

        filters = []
        if self.config.archive_output_height > 0:
            filters.append(f"scale=-2:{self.config.archive_output_height}")
        if filters:
            cmd += ["-vf", ",".join(filters)]

        encoder = self.config.archive_video_encoder
        cmd += ["-c:v", encoder]

        if encoder in {"hevc_nvenc", "h264_nvenc"}:
            cmd += [
                "-preset", self.config.archive_preset,
                "-rc:v", "vbr",
                "-b:v", self.config.archive_target_bitrate,
                "-maxrate:v", self.config.archive_max_bitrate,
                "-bufsize:v", self.config.archive_buffer_size,
            ]
        elif encoder in {"libx265", "libx264"}:
            preset = self.config.archive_preset
            if preset.startswith("p") and preset[1:].isdigit():
                preset = "medium"
            cmd += [
                "-preset", preset,
                "-b:v", self.config.archive_target_bitrate,
                "-maxrate:v", self.config.archive_max_bitrate,
                "-bufsize:v", self.config.archive_buffer_size,
            ]
        else:
            raise ValueError(
                f"ARCHIVE_VIDEO_ENCODER tidak didukung: {encoder}. "
                "Gunakan hevc_nvenc, h264_nvenc, libx265, atau libx264."
            )

        cmd += ["-movflags", "+faststart", str(output)]
        return cmd

    def delete_raw_files(self, files: list[Path]) -> None:
        deleted = 0
        for path in files:
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                self.logger.warning("Failed deleting raw segment %s: %s", path, exc)

        self.logger.info("Deleted %s raw segments after archive encode.", deleted)

    def trim_stderr(self, stderr: str, max_lines: int = 12) -> str:
        lines = (stderr or "").splitlines()
        return " | ".join(lines[-max_lines:])


# =========================================================
# GOOGLE DRIVE UPLOADER
# =========================================================
class GoogleDriveUploader(threading.Thread):
    DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
    MIME_TYPES = {
        ".ts": "video/mp2t",
        ".mp4": "video/mp4",
        ".csv": "text/csv"
    }

    def __init__(self, points: list[CCTVPoint], config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="google-drive-uploader", daemon=True)
        self.points = points
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("gdrive")
        self.service = None
        self.folder_cache: dict[tuple[str, str], str] = {}

    def run(self) -> None:
        if not self.config.drive_upload_enabled:
            self.logger.info("Google Drive uploader disabled.")
            return

        if not self.config.drive_folder_id:
            self.logger.error("GOOGLE_DRIVE_FOLDER_ID kosong. Google Drive uploader disabled.")
            return

        if not self.config.drive_auth_file.exists():
            self.logger.error(
                "Google Drive Auth file tidak ditemukan: %s. Google Drive uploader disabled.",
                self.config.drive_auth_file,
            )
            return

        try:
            self.service = self.build_service()
        except Exception as exc:
            self.logger.exception("Failed initializing Google Drive service: %s", exc)
            return

        self.logger.info("Google Drive uploader started.")

        while not self.stop_event.is_set():
            try:
                self.upload_ready_files()
            except Exception as exc:
                self.logger.exception("Google Drive uploader error: %s", exc)

            self.stop_event.wait(self.config.drive_scan_seconds)

        self.logger.info("Google Drive uploader stopped.")

    def build_service(self):
        from googleapiclient.discovery import build
        import json

        scopes = ["https://www.googleapis.com/auth/drive.file"]
        
        with open(self.config.drive_auth_file, "r") as f:
            auth_data = json.load(f)
            
        if "type" in auth_data and auth_data["type"] == "service_account":
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                self.config.drive_auth_file,
                scopes=scopes,
            )
        else:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            
            credentials = Credentials.from_authorized_user_file(self.config.drive_auth_file, scopes)
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                with open(self.config.drive_auth_file, "w") as f:
                    f.write(credentials.to_json())

        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def upload_ready_files(self) -> None:
        for point in self.points:
            for date_dir in self.date_dirs_for_point(point):
                if self.stop_event.is_set():
                    return
                self.upload_point_date(point, date_dir)

    def date_dirs_for_point(self, point: CCTVPoint) -> list[Path]:
        if not self.config.output_root.exists():
            return []

        dirs: list[Path] = []
        for child in self.config.output_root.iterdir():
            if not child.is_dir():
                continue
            try:
                datetime.strptime(child.name, "%Y-%m-%d")
            except ValueError:
                continue

            if (child / point.name / "videos").exists():
                dirs.append(child)

        return sorted(dirs)

    def upload_point_date(self, point: CCTVPoint, date_dir: Path) -> None:
        # Prioritize uploading encoded mp4s if they exist, otherwise raw videos
        encoded_dir = date_dir / point.name / "videos_encoded"
        raw_dir = date_dir / point.name / "videos"
        metadata_dir = date_dir / point.name / "metadata"
        
        video_files = self.ready_files(encoded_dir, ["*.ts", "*.mp4"])
        if not video_files:
            video_files = self.ready_files(raw_dir, ["*.ts", "*.mp4"])
            
        csv_files = self.ready_files(metadata_dir, ["*.csv"])
            
        if not video_files and not csv_files:
            return

        date_folder_id = self.ensure_drive_folder(self.config.drive_folder_id, date_dir.name)
        camera_folder_id = self.ensure_drive_folder(date_folder_id, point.name)

        # Upload Videos
        if video_files:
            videos_folder_id = self.ensure_drive_folder(camera_folder_id, "videos")
            for path in video_files:
                if self.stop_event.is_set():
                    return
                self.upload_file(path, videos_folder_id)

        # Upload Metadata
        if csv_files:
            metadata_folder_id = self.ensure_drive_folder(camera_folder_id, "metadata")
            for path in csv_files:
                if self.stop_event.is_set():
                    return
                self.upload_file(path, metadata_folder_id)

    def ready_files(self, target_dir: Path, extensions: list[str]) -> list[Path]:
        if not target_dir.exists():
            return []

        files: list[Path] = []
        for ext in extensions:
            if ext == "*.csv" or ext == ".csv":
                cutoff = time.time() - 86400  # 24 hours safe age for CSV
            else:
                cutoff = time.time() - self.config.drive_safe_age_seconds
                
            for path in sorted(target_dir.glob(ext)):
                marker = self.upload_marker(path)
                try:
                    if marker.exists():
                        continue
                    if path.stat().st_size <= 0:
                        continue
                    if path.stat().st_mtime > cutoff:
                        continue
                    files.append(path)
                except FileNotFoundError:
                    continue
        return sorted(files)

    def upload_file(self, path: Path, parent_id: str) -> None:
        from googleapiclient.http import MediaFileUpload

        file_metadata = {
            "name": path.name,
            "parents": [parent_id],
        }
        mime_type = self.MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)

        self.logger.info("Uploading TS to Google Drive: %s", path)
        created = (
            self.service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id,name,size",
                supportsAllDrives=True,
            )
            .execute()
        )

        self.logger.info(
            "Google Drive upload complete: %s | drive_id=%s | size=%s",
            path,
            created.get("id"),
            created.get("size"),
        )

        if self.config.drive_delete_local_after_upload:
            path.unlink(missing_ok=True)
            self.logger.info("Deleted local TS after Google Drive upload: %s", path)
        else:
            self.upload_marker(path).write_text(
                f"{now_local().strftime('%Y-%m-%d %H:%M:%S')},{created.get('id','')}\n",
                encoding="utf-8",
            )

    def ensure_drive_folder(self, parent_id: str, name: str) -> str:
        cache_key = (parent_id, name)
        if cache_key in self.folder_cache:
            return self.folder_cache[cache_key]

        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and "
            f"name = '{escaped_name}' and "
            f"mimeType = '{self.DRIVE_FOLDER_MIME}' and "
            "trashed = false"
        )
        response = (
            self.service.files()
            .list(
                q=query,
                fields="files(id,name)",
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            metadata = {
                "name": name,
                "mimeType": self.DRIVE_FOLDER_MIME,
                "parents": [parent_id],
            }
            created = (
                self.service.files()
                .create(
                    body=metadata,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            folder_id = created["id"]
            self.logger.info("Created Google Drive folder: %s", name)

        self.folder_cache[cache_key] = folder_id
        return folder_id

    def upload_marker(self, path: Path) -> Path:
        return path.with_name(f"{path.name}.uploaded")


def validate_video_encoder(config: RuntimeConfig) -> None:
    """Pastikan encoder FFmpeg tersedia sebelum recorder dijalankan."""
    required_encoders = set()
    if config.ffmpeg_transport_mode in {"smooth", "transcode"}:
        required_encoders.add(config.video_encoder)
    if config.archive_encoder_enabled:
        required_encoders.add(config.archive_video_encoder)

    if not required_encoders:
        return

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg tidak ditemukan pada PATH.") from exc

    missing = sorted(encoder for encoder in required_encoders if encoder not in result.stdout)
    if missing:
        raise RuntimeError(
            f"Encoder FFmpeg tidak tersedia: {', '.join(missing)}. "
            "Periksa NVIDIA driver/NVENC, atau ubah encoder ke libx265/libx264."
        )


# =========================================================
# APP
# =========================================================
class CCTVApp:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        setup_logging(self.config.output_root)
        validate_video_encoder(self.config)

        logging.info("Loading CCTV points from: %s", self.config.config_file)
        points = load_cctv_points(self.config)

        logging.info("CCTV 24x7 Scraper v7 Storage Optimized starting.")
        logging.info("Total CCTV points: %s", len(points))
        logging.info("Output root: %s", self.config.output_root.resolve())
        logging.info("Segment duration: %s seconds", self.config.segment_seconds)
        logging.info("Video container: %s", self.config.video_container)
        logging.info("FFmpeg transport mode: %s", self.config.ffmpeg_transport_mode)
        logging.info("FFmpeg referer: %s", self.config.ffmpeg_referer or "-")
        logging.info("FFmpeg origin: %s", self.config.ffmpeg_origin or "-")
        logging.info("HLS reconnect at EOF: %s", self.config.hls_reconnect_at_eof)
        logging.info("Segment at clock time: %s", self.config.segment_atclocktime)
        logging.info("HLS live start index: %s", self.config.hls_live_start_index)
        logging.info("FFmpeg reconnect on HTTP error: %s", self.config.ffmpeg_reconnect_on_http_error)
        logging.info("Output FPS: %s", self.config.output_fps)
        logging.info("Transcode preset: %s", self.config.transcode_preset)
        logging.info("Transcode CRF/CQ compatibility value: %s", self.config.transcode_crf)
        logging.info("Video encoder: %s", self.config.video_encoder)
        logging.info("Target bitrate: %s", self.config.target_bitrate)
        logging.info("Maximum bitrate: %s", self.config.max_bitrate)
        logging.info("Buffer size: %s", self.config.buffer_size)
        logging.info("Output height: %s", self.config.output_height or "source")
        logging.info("Archive encoder enabled: %s", self.config.archive_encoder_enabled)
        logging.info("Archive interval: %s seconds", self.config.archive_interval_seconds)
        logging.info("Archive scan interval: %s seconds", self.config.archive_scan_seconds)
        logging.info("Archive safe age: %s seconds", self.config.archive_safe_age_seconds)
        logging.info("Archive delete raw after success: %s", self.config.archive_delete_raw_after_success)
        logging.info("Archive video encoder: %s", self.config.archive_video_encoder)
        logging.info("Archive target bitrate: %s", self.config.archive_target_bitrate)
        logging.info("Archive maximum bitrate: %s", self.config.archive_max_bitrate)
        logging.info("Google Drive upload enabled: %s", self.config.drive_upload_enabled)
        logging.info("Google Drive folder ID configured: %s", bool(self.config.drive_folder_id))
        logging.info("Google Drive scan interval: %s seconds", self.config.drive_scan_seconds)
        logging.info("Google Drive safe age: %s seconds", self.config.drive_safe_age_seconds)
        logging.info("Google Drive delete local after upload: %s", self.config.drive_delete_local_after_upload)
        logging.info("Preflight check: %s", self.config.preflight_check)
        logging.info("Offline retry seconds: %s", self.config.offline_retry_seconds)
        logging.info("Network retry seconds: %s", self.config.network_retry_seconds)
        logging.info("Metadata CSV write interval: %s seconds", self.config.metadata_interval_seconds)
        logging.info("TomTom API interval: %s seconds", self.config.tomtom_interval_seconds)
        logging.info("Open-Meteo API interval: %s seconds", self.config.openmeteo_interval_seconds)
        logging.info("Retention days: %s", self.config.retention_days)
        logging.info("Minimum free disk: %.2f GB", self.config.min_free_space_gb)

        if not self.config.tomtom_api_key:
            logging.warning("TOMTOM_API not found. TomTom metadata will be empty.")

        fallback_points = [
            p.name for p in points
            if p.lat == self.config.default_lat and p.lon == self.config.default_lon
        ]
        if fallback_points:
            logging.warning(
                "Some CCTV points use fallback coordinates. Update cctv_points.csv for accuracy: %s",
                ", ".join(fallback_points)
            )

        for point in points:
            recorder = CCTVRecorder(point, self.config, self.stop_event)
            self.threads.append(recorder)
            recorder.start()

        metadata = MetadataCollector(points, self.config, self.stop_event)
        self.threads.append(metadata)
        metadata.start()

        archive = ArchiveEncoder(points, self.config, self.stop_event)
        self.threads.append(archive)
        archive.start()

        drive = GoogleDriveUploader(points, self.config, self.stop_event)
        self.threads.append(drive)
        drive.start()

        disk = DiskMonitor(self.config, self.stop_event)
        self.threads.append(disk)
        disk.start()

        self.wait_forever()

    def wait_forever(self) -> None:
        def request_stop(signum=None, frame=None):
            logging.info("Stop signal received.")
            self.stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

        while not self.stop_event.is_set():
            time.sleep(1)

        logging.info("Stopping all workers.")
        for thread in self.threads:
            thread.join(timeout=30)

        logging.info("CCTV 24x7 Scraper stopped.")


# =========================================================
# CLI
# =========================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CCTV 24x7 scraper with FFmpeg segment mode.")
    parser.add_argument("--config", default=None, help="Path to cctv_points.csv")
    parser.add_argument("--output", default=None, help="Output root directory")
    parser.add_argument(
        "--segment-seconds",
        type=int,
        default=None,
        help="Durasi tiap segment video. Jika kosong, memakai SEGMENT_SECONDS dari .env atau default 60.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Jumlah hari penyimpanan sebelum folder lama dihapus.",
    )
    parser.add_argument(
        "--min-free-space-gb",
        type=float,
        default=None,
        help="Batas minimum sisa storage dalam GB.",
    )
    parser.add_argument(
        "--video-container",
        choices=["ts", "mp4"],
        default=None,
        help="Use 'ts' for robust HLS capture or 'mp4' for direct MP4 segments.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_runtime_config(args)
    app = CCTVApp(config)
    app.start()


if __name__ == "__main__":
    main()
