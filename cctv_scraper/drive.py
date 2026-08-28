import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from cctv_scraper.config import CCTVPoint, RuntimeConfig, now_local


class GoogleDriveUploader(threading.Thread):
    DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
    MIME_TYPES = {".ts": "video/mp2t", ".mp4": "video/mp4", ".csv": "text/csv"}

    def __init__(self, points: list[CCTVPoint], config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="google-drive-uploader", daemon=True)
        self.points = points
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("gdrive")
        self.service: Any = None
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

    def build_service(self) -> Any:
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/drive.file"]

        with open(self.config.drive_auth_file) as f:
            auth_data = json.load(f)

        if "type" in auth_data and auth_data["type"] == "service_account":
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                self.config.drive_auth_file,
                scopes=scopes,
            )
        else:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

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
            if ext in {"*.csv", ".csv"}:
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
                f"{now_local().strftime('%Y-%m-%d %H:%M:%S')},{created.get('id', '')}\n",
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
