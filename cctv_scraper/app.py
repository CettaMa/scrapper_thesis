import logging
import signal
import subprocess
import threading
import time
from dataclasses import replace

# Ensure DoH patch is loaded
import cctv_scraper.doh  # noqa: F401
from cctv_scraper.archive import ArchiveEncoder, DailyArchiver
from cctv_scraper.config import RuntimeConfig, load_cctv_points
from cctv_scraper.disk import DiskMonitor
from cctv_scraper.logging_setup import setup_logging
from cctv_scraper.metadata import MetadataCollector
from cctv_scraper.recorder import CCTVRecorder

HARDWARE_ENCODERS = {"h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi"}


def _archive_with_encoder(config: RuntimeConfig, encoder: str) -> RuntimeConfig:
    return replace(config, archive=replace(config.archive, video_encoder=encoder))


def _hardware_probe_command(config: RuntimeConfig) -> list[str]:
    encoder = config.archive.video_encoder
    device = config.archive.vaapi_device
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
        # Gen9 VA-API rejects anything under 32x32; keep the probe comfortably above
        # every encoder's minimum so a valid device is never mistaken for a broken one.
        "testsrc=size=320x240:rate=1",
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
    if config.recorder.ffmpeg_transport_mode in {"smooth", "transcode"}:
        required_encoders.add(config.recorder.video_encoder)
        recorder_encoders.add(config.recorder.video_encoder)
    archive_hardware = config.archive.video_encoder in HARDWARE_ENCODERS
    if config.archive.enabled:
        required_encoders.add(config.archive.video_encoder)
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
        config.archive.enabled
        and archive_hardware
        and config.archive.video_encoder not in recorder_encoders
        and fallback not in missing
    )
    unrecoverable = [
        encoder
        for encoder in missing
        if not (archive_can_fallback and encoder == config.archive.video_encoder)
    ]
    if unrecoverable:
        raise RuntimeError(
            f"Encoder FFmpeg tidak tersedia: {', '.join(unrecoverable)}. "
            "Periksa encoder hardware, atau ubah encoder ke h264_vaapi, hevc_vaapi, libx265, atau libx264."
        )

    if not archive_can_fallback:
        return config

    if config.archive.video_encoder in missing:
        logging.getLogger("app").warning(
            "Archive encoder %s is not compiled into FFmpeg; using fallback %s for this process.",
            config.archive.video_encoder,
            fallback,
        )
        return _archive_with_encoder(config, fallback)

    try:
        probe = _probe_archive_hardware(config)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        probe_error = str(exc)
        logging.getLogger("app").warning(
            "Archive hardware encoder %s could not use device %s; using fallback %s: %s",
            config.archive.video_encoder,
            config.archive.vaapi_device,
            fallback,
            probe_error,
        )
        return _archive_with_encoder(config, fallback)

    if probe.returncode != 0:
        stderr = (probe.stderr or "").strip().splitlines()
        detail = " | ".join(stderr[-3:])
        logging.getLogger("app").warning(
            "Archive hardware encoder %s could not use device %s; using fallback %s%s",
            config.archive.video_encoder,
            config.archive.vaapi_device,
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
            self.config.storage.output_root,
            self.config.storage.log_max_bytes,
            self.config.storage.log_backup_count,
        )
        self.config = validate_video_encoder(self.config)

        logging.info("Loading CCTV points from: %s", self.config.config_file)
        points = load_cctv_points(self.config)

        logging.info("CCTV 24x7 Scraper starting.")
        logging.info("Total CCTV points: %s", len(points))
        logging.info("Output root: %s", self.config.storage.output_root.resolve())
        logging.info("Segment duration: %s seconds", self.config.recorder.segment_seconds)
        logging.info("Video container: %s", self.config.recorder.video_container)
        logging.info("FFmpeg transport mode: %s", self.config.recorder.ffmpeg_transport_mode)
        logging.info("FFmpeg referer: %s", self.config.recorder.ffmpeg_referer or "-")
        logging.info("FFmpeg origin: %s", self.config.recorder.ffmpeg_origin or "-")
        logging.info("HLS reconnect at EOF: %s", self.config.recorder.hls_reconnect_at_eof)
        logging.info("Segment at clock time: %s", self.config.recorder.segment_atclocktime)
        logging.info("HLS live start index: %s", self.config.recorder.hls_live_start_index)
        logging.info(
            "FFmpeg reconnect on HTTP error: %s",
            self.config.recorder.ffmpeg_reconnect_on_http_error,
        )
        logging.info("Output FPS: %s", self.config.recorder.output_fps)
        logging.info("Transcode preset: %s", self.config.recorder.transcode_preset)
        logging.info("Video encoder: %s", self.config.recorder.video_encoder)
        logging.info("Target bitrate: %s", self.config.recorder.target_bitrate)
        logging.info("Maximum bitrate: %s", self.config.recorder.max_bitrate)
        logging.info("Buffer size: %s", self.config.recorder.buffer_size)
        logging.info("Output height: %s", self.config.recorder.output_height or "source")
        logging.info("Archive encoder enabled: %s", self.config.archive.enabled)
        logging.info("Archive interval: %s seconds", self.config.archive.interval_seconds)
        logging.info("Archive scan interval: %s seconds", self.config.archive.scan_seconds)
        logging.info("Archive safe age: %s seconds", self.config.archive.safe_age_seconds)
        logging.info(
            "Archive delete raw after success: %s",
            self.config.archive.delete_raw_after_success,
        )
        logging.info("Archive video encoder: %s", self.config.archive.video_encoder)
        logging.info("Archive VA-API/QSV device: %s", self.config.archive.vaapi_device)
        logging.info("Archive retry max attempts: %s", self.config.archive.max_attempts)
        logging.info("Daily 7-Zip archive enabled: %s", self.config.archive.daily_archive_enabled)
        logging.info("Daily 7-Zip archive scan interval: %s seconds", self.config.archive.daily_archive_scan_seconds)
        logging.info("Daily 7-Zip archiver binary: %s", self.config.archive.archiver_binary)
        logging.info("Archive target bitrate: %s", self.config.archive.target_bitrate)
        logging.info("Archive maximum bitrate: %s", self.config.archive.max_bitrate)
        logging.info("Preflight check: %s", self.config.network.preflight_check)
        logging.info("Offline retry seconds: %s", self.config.network.offline_retry_seconds)
        logging.info("Network retry seconds: %s", self.config.network.network_retry_seconds)
        logging.info(
            "Metadata CSV write interval: %s seconds",
            self.config.metadata.metadata_interval_seconds,
        )
        logging.info(
            "TomTom API interval: %s seconds", self.config.metadata.tomtom_interval_seconds
        )
        logging.info(
            "Open-Meteo API interval: %s seconds",
            self.config.metadata.openmeteo_interval_seconds,
        )
        logging.info(
            "Metadata minimum pass interval: %s seconds",
            self.config.metadata.min_pass_interval_seconds,
        )
        logging.info(
            "Expired URL escalation threshold: %s failures",
            self.config.network.expired_url_escalation_threshold,
        )
        logging.info("Retention days: %s", self.config.storage.retention_days)
        logging.info("Minimum free disk: %.2f GB", self.config.storage.min_free_space_gb)
        logging.info("Log max bytes: %s", self.config.storage.log_max_bytes)
        logging.info("Log backup count: %s", self.config.storage.log_backup_count)

        if not self.config.metadata.tomtom_api_key:
            logging.warning("TOMTOM_API not found. TomTom metadata will be empty.")

        fallback_points = [
            p.name
            for p in points
            if p.lat == self.config.metadata.default_lat
            and p.lon == self.config.metadata.default_lon
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

        daily_archiver = DailyArchiver(self.config, self.stop_event)
        self.threads.append(daily_archiver)
        daily_archiver.start()

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
