import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from cctv_scraper.config import (
    CCTVPoint,
    RuntimeConfig,
    archive_window_filename,
    ensure_dir,
    window_start_epoch,
)
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
        if not self.config.archive.enabled:
            self.logger.info("Archive encoder disabled.")
            return

        self.logger.info("Archive encoder started.")

        while not self.stop_event.is_set():
            try:
                self.encode_ready_batches()
            except Exception as exc:
                self.logger.exception("Archive encoder error: %s", exc)

            self.stop_event.wait(self.config.archive.scan_seconds)

        self.logger.info("Archive encoder stopped.")

    def encode_ready_batches(self) -> None:
        for point in self.points:
            for date_dir in self.date_dirs_for_point(point):
                if self.stop_event.is_set():
                    return
                self.encode_point_date(point, date_dir)

    def date_dirs_for_point(self, point: CCTVPoint) -> list[Path]:
        return iter_point_date_dirs(self.config.storage.output_root, point)

    def ready_raw_files(self, raw_dir: Path) -> list[Path]:
        return ready_files(raw_dir, [".ts"], self.config.archive.safe_age_seconds)

    def encode_point_date(self, point: CCTVPoint, date_dir: Path) -> None:
        raw_dir = date_dir / point.name / "videos"
        encoded_dir = date_dir / point.name / "videos_encoded"
        ensure_dir(encoded_dir)

        files = self.ready_raw_files(raw_dir)
        if not files:
            return

        grouped: dict[int, list[Path]] = {}
        for path in files:
            segment_start = self.segment_start_timestamp(point, path)
            if segment_start is None:
                segment_start = int(path.stat().st_mtime)
            window_start = window_start_epoch(segment_start, self.config.archive.interval_seconds)
            window_end = window_start + self.config.archive.interval_seconds
            # A segment starting just before the boundary is still being written at
            # window_end and only becomes readable segment_seconds + safe_age later.
            # Encoding before then would leave it stranded as a late segment.
            if time.time() < self.window_ready_at(window_end):
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

        output = encoded_dir / archive_window_filename(
            point.name, window_start, self.config.archive.interval_seconds
        )

        failure_marker = self.failure_marker_path(output)
        if output.exists() and output.stat().st_size > 0:
            self.remove_failure_marker(failure_marker)
            self.record_late_segments(output, files)
            return

        failure_state = self.read_failure_state(failure_marker)
        if failure_state.get("status") == "failed":
            return
        raw_next_retry_at = failure_state.get("next_retry_at", 0.0)
        next_retry_at = (
            float(raw_next_retry_at) if isinstance(raw_next_retry_at, (int, float)) else 0.0
        )
        if time.time() < next_retry_at:
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

            used_encoder = self.config.archive.video_encoder
            try:
                result = self.run_ffmpeg(command)
            except OSError as exc:
                self.record_failure(output, failure_marker, f"FFmpeg could not run: {exc}")
                return

            if result.returncode != 0 and self.is_hardware_encoder(
                self.config.archive.video_encoder
            ):
                fallback = self.config.archive.fallback_video_encoder
                self.logger.warning(
                    "Hardware archive encode failed for %s; retrying with %s | stderr=%s",
                    output,
                    fallback,
                    trim_stderr(result.stderr),
                )
                try:
                    tmp_output.unlink(missing_ok=True)
                except OSError:
                    pass
                command = self.build_encode_command(list_path, tmp_output, encoder=fallback)
                used_encoder = fallback
                try:
                    result = self.run_ffmpeg(command)
                except OSError as exc:
                    self.record_failure(
                        output, failure_marker, f"Fallback FFmpeg could not run: {exc}"
                    )
                    return

            if result.returncode != 0:
                self.record_failure(
                    output,
                    failure_marker,
                    f"code={result.returncode} | stderr={trim_stderr(result.stderr)}",
                )
                return

            if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
                self.record_failure(output, failure_marker, "FFmpeg produced empty output")
                return

            manifest = self.build_manifest(point, window_start, files, used_encoder)
            if not self.write_manifest(self.manifest_path(output), manifest):
                self.logger.error(
                    "Archive manifest could not be written; preserving raw files: %s", output
                )
                return

            tmp_output.replace(output)
            self.remove_failure_marker(failure_marker)
            self.logger.info("Archive saved: %s | bytes=%s", output, output.stat().st_size)

            if self.config.archive.delete_raw_after_success:
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

    @staticmethod
    def segment_start_timestamp(point: CCTVPoint, path: Path) -> int | None:
        prefix = f"{point.name}_"
        if not path.stem.startswith(prefix):
            return None
        timestamp_text = path.stem[len(prefix) :]
        try:
            return int(datetime.strptime(timestamp_text, "%Y%m%d_%H%M%S").timestamp())
        except ValueError:
            return None

    def window_ready_at(self, window_end: int) -> int:
        """Earliest time every segment belonging to a window can have become readable."""
        return (
            window_end + self.config.recorder.segment_seconds + self.config.archive.safe_age_seconds
        )

    def record_late_segments(self, output: Path, files: list[Path]) -> None:
        """Flag an already-encoded window whose raw segments turned up too late.

        The raw files are deliberately kept: they hold footage the archive does not,
        so deleting them would lose it outright.
        """
        names = sorted(path.name for path in files)
        self.logger.warning(
            "Window already encoded but %s raw segment(s) arrived late; archive is "
            "incomplete and raw segments are preserved: %s | %s",
            len(names),
            output,
            ", ".join(names),
        )

        manifest_path = self.manifest_path(output)
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        manifest: dict[str, object] = loaded if isinstance(loaded, dict) else {}

        previous = manifest.get("late_segments")
        previous_names = previous if isinstance(previous, list) else []
        manifest["late_segments"] = sorted({*names, *previous_names})
        manifest["complete"] = False
        self.write_manifest(manifest_path, manifest)

    @staticmethod
    def manifest_path(output: Path) -> Path:
        # Keep provenance files hidden so generic directory scans cannot treat them as media.
        return output.parent / f".{output.name}.manifest.json"

    def build_manifest(
        self, point: CCTVPoint, window_start: int, files: list[Path], encoder: str
    ) -> dict[str, object]:
        window_end = window_start + self.config.archive.interval_seconds
        segment_starts: list[int] = []
        for path in files:
            segment_start = self.segment_start_timestamp(point, path)
            segment_starts.append(
                segment_start if segment_start is not None else int(path.stat().st_mtime)
            )
        first_start = min(segment_starts)
        last_start = max(segment_starts)
        intervals = sorted(
            (
                max(window_start, start),
                min(window_end, start + self.config.recorder.segment_seconds),
            )
            for start in segment_starts
        )
        actual_duration = 0
        covered_start: int | None = None
        covered_end: int | None = None
        for start, end in intervals:
            if end <= start:
                continue
            if covered_start is None:
                covered_start, covered_end = start, end
            elif covered_end is not None and start > covered_end:
                actual_duration += covered_end - covered_start
                covered_start, covered_end = start, end
            else:
                assert covered_end is not None
                covered_end = max(covered_end, end)
        if covered_start is not None and covered_end is not None:
            actual_duration += covered_end - covered_start

        window_start_dt = datetime.fromtimestamp(window_start)
        window_end_dt = datetime.fromtimestamp(window_end)
        return {
            "point_name": point.name,
            "window_start": window_start,
            "window_end": window_end,
            "window_start_iso": window_start_dt.isoformat(),
            "window_end_iso": window_end_dt.isoformat(),
            "segment_count": len(files),
            "first_segment_start": first_start,
            "last_segment_start": last_start,
            "first_segment_start_iso": datetime.fromtimestamp(first_start).isoformat(),
            "last_segment_start_iso": datetime.fromtimestamp(last_start).isoformat(),
            "expected_covered_duration_seconds": self.config.archive.interval_seconds,
            "actual_covered_duration_seconds": actual_duration,
            "encoder": encoder,
            "encoder_used": encoder,
        }

    def write_manifest(self, manifest_path: Path, manifest: dict[str, object]) -> bool:
        temporary_path = manifest_path.with_name(f"{manifest_path.name}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            temporary_path.replace(manifest_path)
            return True
        except OSError as exc:
            self.logger.error("Cannot write archive manifest %s: %s", manifest_path, exc)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def failure_marker_path(output: Path) -> Path:
        """Return the persistent state file for an unsuccessfully encoded window."""
        return output.parent / f".{output.name}.failure.json"

    def read_failure_state(self, marker: Path) -> dict[str, object]:
        if not marker.exists():
            return {}
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("Cannot read archive failure marker %s: %s", marker, exc)
            return {}
        return state if isinstance(state, dict) else {}

    def remove_failure_marker(self, marker: Path) -> None:
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.warning("Cannot remove archive failure marker %s: %s", marker, exc)

    def record_failure(self, output: Path, marker: Path, reason: str) -> None:
        state = self.read_failure_state(marker)
        previous_attempts = state.get("attempts", 0)
        attempts = int(previous_attempts) if isinstance(previous_attempts, (int, float)) else 0
        attempts += 1
        max_attempts = self.config.archive.max_attempts
        terminal = attempts >= max_attempts
        failure_time = time.time()
        state = {
            "status": "failed" if terminal else "retrying",
            "attempts": attempts,
            "last_failure": reason,
            "failed_at": failure_time,
        }

        if terminal:
            self.logger.warning(
                "Archive window marked failed; skipping %s after %s attempts | reason=%s",
                output,
                attempts,
                reason,
            )
        else:
            delay = min(
                self.config.archive.retry_max_seconds,
                self.config.archive.retry_base_seconds * (2 ** (attempts - 1)),
            )
            state["next_retry_at"] = failure_time + delay
            self.logger.warning(
                "Archive encode failed for %s; retrying in %s seconds (attempt %s/%s) | reason=%s",
                output,
                delay,
                attempts,
                max_attempts,
                reason,
            )

        temporary_marker = marker.with_name(f"{marker.name}.tmp")
        try:
            temporary_marker.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            temporary_marker.replace(marker)
        except OSError as exc:
            self.logger.error("Cannot persist archive failure marker %s: %s", marker, exc)
            try:
                temporary_marker.unlink(missing_ok=True)
            except OSError:
                pass

    def write_concat_file(self, list_path: Path, files: list[Path]) -> None:
        with list_path.open("w", encoding="utf-8", newline="\n") as f:
            for path in files:
                escaped = path.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

    @staticmethod
    def is_hardware_encoder(encoder: str) -> bool:
        return encoder in {"h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi"}

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
        encoder = encoder or self.config.archive.video_encoder
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            self.config.recorder.ffmpeg_loglevel,
            "-y",
        ]
        if encoder in {"h264_vaapi", "hevc_vaapi"}:
            cmd += ["-vaapi_device", self.config.archive.vaapi_device]

        cmd += [
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
        # Drop frames before scaling or hwupload so the encoder never sees them.
        if self.config.archive.output_fps > 0:
            filters.append(f"fps={self.config.archive.output_fps}")
        if self.config.archive.output_height > 0:
            filters.append(f"scale=-2:{self.config.archive.output_height}")
        if encoder in {"h264_vaapi", "hevc_vaapi"}:
            # VA-API encoders require hardware-uploaded NV12 frames.
            filters.extend(["format=nv12", "hwupload"])
        if filters:
            cmd += ["-vf", ",".join(filters)]

        cmd += ["-c:v", encoder]

        if encoder in {"hevc_nvenc", "h264_nvenc"}:
            cmd += [
                "-preset",
                self.config.archive.preset,
                "-rc:v",
                "vbr",
                "-b:v",
                self.config.archive.target_bitrate,
                "-maxrate:v",
                self.config.archive.max_bitrate,
                "-bufsize:v",
                self.config.archive.buffer_size,
            ]
        elif encoder in {"h264_qsv", "hevc_qsv"}:
            preset = self.config.archive.preset
            if preset.startswith("p") and preset[1:].isdigit():
                preset = "veryfast"
            cmd += [
                "-preset",
                preset,
                "-b:v",
                self.config.archive.target_bitrate,
                "-maxrate:v",
                self.config.archive.max_bitrate,
                "-bufsize:v",
                self.config.archive.buffer_size,
            ]
        elif encoder in {"h264_vaapi", "hevc_vaapi"}:
            cmd += [
                "-b:v",
                self.config.archive.target_bitrate,
                "-maxrate:v",
                self.config.archive.max_bitrate,
                "-bufsize:v",
                self.config.archive.buffer_size,
            ]
        elif encoder in {"libx265", "libx264"}:
            preset = self.config.archive.preset
            if preset.startswith("p") and preset[1:].isdigit():
                preset = "medium"
            cmd += [
                "-preset",
                preset,
                "-b:v",
                self.config.archive.target_bitrate,
                "-maxrate:v",
                self.config.archive.max_bitrate,
                "-bufsize:v",
                self.config.archive.buffer_size,
            ]
        else:
            raise ValueError(
                f"ARCHIVE_VIDEO_ENCODER tidak didukung: {encoder}. "
                "Gunakan h264_qsv, hevc_qsv, h264_vaapi, hevc_vaapi, hevc_nvenc, "
                "h264_nvenc, libx265, atau libx264."
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
