import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from cctv_scraper.config import CCTVPoint, RuntimeConfig, ensure_dir
from cctv_scraper.storage_scan import iter_point_date_dirs, ready_files, trim_stderr


class ArchiveEncoder(threading.Thread):
    """Encode available raw content into fixed five-minute local time windows.

    A window may contain less than five minutes of media when the camera was
    unavailable. The output filename still identifies the complete window; no
    synthetic black/silent padding is generated.
    """

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
        return iter_point_date_dirs(self.config.output_root, point)

    def ready_raw_files(self, raw_dir: Path) -> list[Path]:
        return ready_files(raw_dir, [".ts"], self.config.archive_safe_age_seconds)

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

    def encode_batch(
        self, point: CCTVPoint, encoded_dir: Path, window_start: int, files: list[Path]
    ) -> None:
        if not files:
            return

        start_dt = datetime.fromtimestamp(window_start)
        end_dt = datetime.fromtimestamp(window_start + self.config.archive_interval_seconds)
        output = encoded_dir / (
            f"{point.name}_{start_dt.strftime('%Y%m%d_%H%M%S')}_{end_dt.strftime('%H%M%S')}.mp4"
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

            result = self.run_ffmpeg(command)

            if result.returncode != 0 and self.is_qsv_encoder(self.config.archive.video_encoder):
                fallback = self.config.archive.fallback_video_encoder
                self.logger.warning(
                    "QuickSync archive encode failed for %s; retrying with %s | stderr=%s",
                    output,
                    fallback,
                    trim_stderr(result.stderr),
                )
                try:
                    tmp_output.unlink(missing_ok=True)
                except OSError:
                    pass
                command = self.build_encode_command(list_path, tmp_output, encoder=fallback)
                result = self.run_ffmpeg(command)

            if result.returncode != 0:
                self.logger.warning(
                    "Archive encode failed for %s | code=%s | stderr=%s",
                    output,
                    result.returncode,
                    trim_stderr(result.stderr),
                )
                return

            if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
                self.logger.warning("Archive encode produced empty output: %s", tmp_output)
                return

            tmp_output.replace(output)
            self.logger.info("Archive saved: %s | bytes=%s", output, output.stat().st_size)

            if self.config.archive_delete_raw_after_success:
                self.delete_raw_files(files)

        finally:
            try:
                list_path.unlink(missing_ok=True)
            except OSError:
                pass
            if tmp_output.exists():
                try:
                    tmp_output.unlink()
                except OSError:
                    pass

    def write_concat_file(self, list_path: Path, files: list[Path]) -> None:
        with list_path.open("w", encoding="utf-8", newline="\n") as f:
            for path in files:
                escaped = path.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

    @staticmethod
    def is_qsv_encoder(encoder: str) -> bool:
        return encoder in {"h264_qsv", "hevc_qsv"}

    def run_ffmpeg(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def build_encode_command(
        self, list_path: Path, output: Path, encoder: str | None = None
    ) -> list[str]:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            self.config.ffmpeg_loglevel,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-fflags",
            "+genpts",
            "-avoid_negative_ts",
            "make_zero",
            "-an",
        ]

        filters = []
        if self.config.archive_output_height > 0:
            filters.append(f"scale=-2:{self.config.archive_output_height}")
        if filters:
            cmd += ["-vf", ",".join(filters)]

        encoder = encoder or self.config.archive_video_encoder
        cmd += ["-c:v", encoder]

        if encoder in {"hevc_nvenc", "h264_nvenc"}:
            cmd += [
                "-preset",
                self.config.archive_preset,
                "-rc:v",
                "vbr",
                "-b:v",
                self.config.archive_target_bitrate,
                "-maxrate:v",
                self.config.archive_max_bitrate,
                "-bufsize:v",
                self.config.archive_buffer_size,
            ]
        elif encoder in {"h264_qsv", "hevc_qsv"}:
            preset = self.config.archive_preset
            if preset.startswith("p") and preset[1:].isdigit():
                preset = "veryfast"
            cmd += [
                "-preset",
                preset,
                "-b:v",
                self.config.archive_target_bitrate,
                "-maxrate:v",
                self.config.archive_max_bitrate,
                "-bufsize:v",
                self.config.archive_buffer_size,
            ]
        elif encoder in {"libx265", "libx264"}:
            preset = self.config.archive_preset
            if preset.startswith("p") and preset[1:].isdigit():
                preset = "medium"
            cmd += [
                "-preset",
                preset,
                "-b:v",
                self.config.archive_target_bitrate,
                "-maxrate:v",
                self.config.archive_max_bitrate,
                "-bufsize:v",
                self.config.archive_buffer_size,
            ]
        else:
            raise ValueError(
                f"ARCHIVE_VIDEO_ENCODER tidak didukung: {encoder}. "
                "Gunakan h264_qsv, hevc_qsv, hevc_nvenc, h264_nvenc, libx265, atau libx264."
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
            except OSError as exc:
                self.logger.warning("Failed deleting raw segment %s: %s", path, exc)

        self.logger.info("Deleted %s raw segments after archive encode.", deleted)
