import logging
import signal
import subprocess
import threading
import time

# Ensure DoH patch is loaded
import cctv_scraper.doh  # noqa: F401
from cctv_scraper.archive import ArchiveEncoder
from cctv_scraper.config import RuntimeConfig, load_cctv_points
from cctv_scraper.disk import DiskMonitor
from cctv_scraper.drive import GoogleDriveUploader
from cctv_scraper.logging_setup import setup_logging
from cctv_scraper.metadata import MetadataCollector
from cctv_scraper.recorder import CCTVRecorder


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
        logging.info("Archive target bitrate: %s", self.config.archive_target_bitrate)
        logging.info("Archive maximum bitrate: %s", self.config.archive_max_bitrate)
        logging.info("Google Drive upload enabled: %s", self.config.drive_upload_enabled)
        logging.info("Google Drive folder ID configured: %s", bool(self.config.drive_folder_id))
        logging.info("Google Drive scan interval: %s seconds", self.config.drive_scan_seconds)
        logging.info("Google Drive safe age: %s seconds", self.config.drive_safe_age_seconds)
        logging.info(
            "Google Drive delete local after upload: %s",
            self.config.drive_delete_local_after_upload,
        )
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
        logging.info("Retention days: %s", self.config.retention_days)
        logging.info("Minimum free disk: %.2f GB", self.config.min_free_space_gb)

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
