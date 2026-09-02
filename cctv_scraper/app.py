import logging
import signal
import subprocess
import threading
import time
from dataclasses import replace

# Ensure DoH patch is loaded
import cctv_scraper.doh  # noqa: F401
from cctv_scraper.archive import ArchiveEncoder
from cctv_scraper.config import RuntimeConfig, load_cctv_points
from cctv_scraper.disk import DiskMonitor
from cctv_scraper.logging_setup import setup_logging
from cctv_scraper.metadata import MetadataCollector
from cctv_scraper.recorder import CCTVRecorder

HARDWARE_ENCODERS = {"h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi"}


def _archive_with_encoder(config: RuntimeConfig, encoder: str) -> RuntimeConfig:
    return replace(config, archive=replace(config.archive, video_encoder=encoder))


def _hardware_probe_command(config: RuntimeConfig) -> list[str]:
    encoder = config.archive_video_encoder
    device = config.archive_vaapi_device
    device_option = "-vaapi_device" if encoder.endswith("vaapi") else "-qsv_device"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        device_option,
        device,
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=16x16:rate=1",
        "-t",
        "1",
        "-vf",
        "format=nv12" if encoder.endswith("qsv") else "format=nv12,hwupload",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]


def _probe_archive_hardware(config: RuntimeConfig) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _hardware_probe_command(config),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
    )


def validate_video_encoder(config: RuntimeConfig) -> RuntimeConfig:
    """Validate FFmpeg encoders and fall back if archive hardware is unusable.

    Listing an encoder only proves that FFmpeg was compiled with it.  The short
    probe below also checks that the configured device can actually encode.
    The returned config is the process-lifetime config used by ``CCTVApp``.
    """
    required_encoders: set[str] = set()
    recorder_encoders: set[str] = set()
    if config.ffmpeg_transport_mode in {"smooth", "transcode"}:
        required_encoders.add(config.video_encoder)
        recorder_encoders.add(config.video_encoder)
    archive_hardware = config.archive_video_encoder in HARDWARE_ENCODERS
    if config.archive_encoder_enabled:
        required_encoders.add(config.archive_video_encoder)
        if archive_hardware:
            required_encoders.add(config.archive.fallback_video_encoder)

    if not required_encoders:
        return config

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
    fallback = config.archive.fallback_video_encoder
    archive_can_fallback = (
        config.archive_encoder_enabled
        and archive_hardware
        and config.archive_video_encoder not in recorder_encoders
        and fallback not in missing
    )
    unrecoverable = [
        encoder
        for encoder in missing
        if not (archive_can_fallback and encoder == config.archive_video_encoder)
    ]
    if unrecoverable:
        raise RuntimeError(
            f"Encoder FFmpeg tidak tersedia: {', '.join(unrecoverable)}. "
            "Periksa encoder hardware, atau ubah encoder ke h264_vaapi, hevc_vaapi, libx265, atau libx264."
        )

    if not archive_can_fallback:
        return config

    if config.archive_video_encoder in missing:
        logging.getLogger("app").warning(
            "Archive encoder %s is not compiled into FFmpeg; using fallback %s for this process.",
            config.archive_video_encoder,
            fallback,
        )
        return _archive_with_encoder(config, fallback)

    try:
        probe = _probe_archive_hardware(config)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        probe_error = str(exc)
        logging.getLogger("app").warning(
            "Archive hardware encoder %s could not use device %s; using fallback %s: %s",
            config.archive_video_encoder,
            config.archive_vaapi_device,
            fallback,
            probe_error,
        )
        return _archive_with_encoder(config, fallback)

    if probe.returncode != 0:
        stderr = (probe.stderr or "").strip().splitlines()
        detail = " | ".join(stderr[-3:])
        logging.getLogger("app").warning(
            "Archive hardware encoder %s could not use device %s; using fallback %s%s",
            config.archive_video_encoder,
            config.archive_vaapi_device,
            fallback,
            f": {detail}" if detail else ".",
        )
        return _archive_with_encoder(config, fallback)

    return config


class CCTVApp:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        setup_logging(
            self.config.output_root,
            self.config.log_max_bytes,
            self.config.log_backup_count,
        )
        self.config = validate_video_encoder(self.config)

        logging.info("Loading CCTV points from: %s", self.config.config_file)
        points = load_cctv_points(self.config)

        logging.info("CCTV 24x7 Scraper starting.")
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
        logging.info(
            "FFmpeg reconnect on HTTP error: %s",
            self.config.ffmpeg_reconnect_on_http_error,
        )
        logging.info("Output FPS: %s", self.config.output_fps)
        logging.info("Transcode preset: %s", self.config.transcode_preset)
        logging.info("Video encoder: %s", self.config.video_encoder)
        logging.info("Target bitrate: %s", self.config.target_bitrate)
        logging.info("Maximum bitrate: %s", self.config.max_bitrate)
        logging.info("Buffer size: %s", self.config.buffer_size)
        logging.info("Output height: %s", self.config.output_height or "source")
        logging.info("Archive encoder enabled: %s", self.config.archive_encoder_enabled)
        logging.info("Archive interval: %s seconds", self.config.archive_interval_seconds)
        logging.info("Archive scan interval: %s seconds", self.config.archive_scan_seconds)
        logging.info("Archive safe age: %s seconds", self.config.archive_safe_age_seconds)
        logging.info(
            "Archive delete raw after success: %s",
            self.config.archive_delete_raw_after_success,
        )
        logging.info("Archive video encoder: %s", self.config.archive_video_encoder)
        logging.info("Archive VA-API/QSV device: %s", self.config.archive_vaapi_device)
        logging.info("Archive retry max attempts: %s", self.config.archive_max_attempts)
        logging.info("Archive target bitrate: %s", self.config.archive_target_bitrate)
        logging.info("Archive maximum bitrate: %s", self.config.archive_max_bitrate)
        logging.info("Preflight check: %s", self.config.preflight_check)
        logging.info("Offline retry seconds: %s", self.config.offline_retry_seconds)
        logging.info("Network retry seconds: %s", self.config.network_retry_seconds)
        logging.info(
            "Metadata CSV write interval: %s seconds",
            self.config.metadata_interval_seconds,
        )
        logging.info("TomTom API interval: %s seconds", self.config.tomtom_interval_seconds)
        logging.info(
            "Open-Meteo API interval: %s seconds",
            self.config.openmeteo_interval_seconds,
        )
        logging.info(
            "Metadata minimum pass interval: %s seconds",
            self.config.metadata_min_pass_interval_seconds,
        )
        logging.info(
            "Expired URL escalation threshold: %s failures",
            self.config.expired_url_escalation_threshold,
        )
        logging.info("Retention days: %s", self.config.retention_days)
        logging.info("Minimum free disk: %.2f GB", self.config.min_free_space_gb)
        logging.info("Log max bytes: %s", self.config.log_max_bytes)
        logging.info("Log backup count: %s", self.config.log_backup_count)

        if not self.config.tomtom_api_key:
            logging.warning("TOMTOM_API not found. TomTom metadata will be empty.")

        fallback_points = [
            p.name
            for p in points
            if p.lat == self.config.default_lat and p.lon == self.config.default_lon
        ]
        if fallback_points:
            logging.warning(
                "Some CCTV points use fallback coordinates. Update cctv_points.csv for accuracy: %s",
                ", ".join(fallback_points),
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
