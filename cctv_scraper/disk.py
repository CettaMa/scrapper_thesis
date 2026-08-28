import logging
import shutil
import threading
from datetime import datetime, timedelta

from cctv_scraper.config import RuntimeConfig, ensure_dir, now_local


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
        free_gb = usage.free / (1024**3)

        if free_gb < self.config.min_free_space_gb:
            self.logger.warning(
                "Low disk space: %.2f GB free. Minimum configured: %.2f GB.",
                free_gb,
                self.config.min_free_space_gb,
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
