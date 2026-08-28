import logging
import os
import shutil
import threading
from datetime import timedelta
from pathlib import Path

from cctv_scraper.config import RuntimeConfig, ensure_dir, now_local
from cctv_scraper.storage_scan import parse_date_dir_name


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

        try:
            with os.scandir(self.config.output_root) as it:
                for entry in it:
                    if not entry.is_dir() or entry.name in {"logs", "status"}:
                        continue

                    folder_date = parse_date_dir_name(entry.name)
                    if folder_date is None:
                        continue

                    if folder_date < cutoff_date:
                        target = Path(entry.path)
                        self.logger.warning("Deleting old footage folder: %s", target)
                        shutil.rmtree(target, ignore_errors=True)
        except OSError as exc:
            self.logger.warning("Error during disk cleanup scan: %s", exc)
