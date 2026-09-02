import logging
import os
import shutil
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

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
            self.emergency_purge(usage)
        else:
            self.logger.info("Disk free space: %.2f GB.", free_gb)

    def emergency_purge(self, initial_usage: Any = None) -> None:
        """Delete oldest complete date directories until the disk is safe again."""
        usage = initial_usage or shutil.disk_usage(self.config.output_root)
        today = now_local().date()
        candidates: list[tuple[date, Path]] = []

        try:
            with os.scandir(self.config.output_root) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    folder_date = parse_date_dir_name(entry.name)
                    if folder_date is not None and folder_date < today:
                        candidates.append((folder_date, Path(entry.path)))
        except OSError as exc:
            self.logger.warning("Error during emergency purge scan: %s", exc)
            return

        candidates.sort(key=lambda item: item[0])
        for _, target in candidates:
            if usage.free / (1024**3) >= self.config.min_free_space_gb:
                return

            self.logger.warning("Emergency low-disk purge deleting footage folder: %s", target)
            try:
                shutil.rmtree(target)
            except OSError as exc:
                self.logger.warning("Emergency purge failed for %s: %s", target, exc)
            usage = shutil.disk_usage(self.config.output_root)

        if usage.free / (1024**3) < self.config.min_free_space_gb:
            self.logger.warning(
                "Emergency purge could not reach configured free space; %.2f GB remains.",
                usage.free / (1024**3),
            )

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

        self.cleanup_old_files(self.config.output_root / "logs")
        self.cleanup_old_files(self.config.output_root / "status")

    def cleanup_old_files(self, directory: Path) -> None:
        """Prune aged logs and status files without following links."""
        if not directory.is_dir():
            return

        cutoff_ts = time.time() - self.config.retention_days * 24 * 60 * 60
        for root, _, filenames in os.walk(directory, followlinks=False):
            for filename in filenames:
                path = Path(root) / filename
                try:
                    if path.stat().st_mtime < cutoff_ts:
                        self.logger.warning("Deleting old log/status file: %s", path)
                        path.unlink()
                except OSError as exc:
                    self.logger.warning("Failed deleting old log/status file %s: %s", path, exc)
