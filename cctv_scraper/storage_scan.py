import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from cctv_scraper.config import CCTVPoint, now_local


def trim_stderr(stderr: str, max_lines: int = 12) -> str:
    lines = (stderr or "").splitlines()
    return " | ".join(lines[-max_lines:])


def parse_date_dir_name(name: str) -> date | None:
    try:
        return datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def has_pending_work(point_dir: Path) -> bool:
    """Check whether a point directory has un-uploaded or un-archived video/metadata files."""
    if not point_dir.is_dir():
        return False

    subdirs = ["videos", "videos_encoded", "metadata"]
    for subdir_name in subdirs:
        sub_path = point_dir / subdir_name
        if not sub_path.is_dir():
            continue
        try:
            with os.scandir(sub_path) as it:
                entries = list(it)
                markers = {e.name for e in entries if e.name.endswith(".uploaded")}
                for e in entries:
                    if not e.is_file():
                        continue
                    name = e.name
                    if name.endswith(".uploaded") or name.startswith("."):
                        continue
                    ext = Path(name).suffix.lower()
                    if ext in {".ts", ".mp4", ".csv"}:
                        if f"{name}.uploaded" not in markers:
                            return True
        except OSError:
            continue
    return False


def iter_point_date_dirs(
    root: Path,
    point: CCTVPoint,
    *,
    since_date: date | None = None,
    check_pending: bool = True,
) -> list[Path]:
    """
    Iterate date directories for a CCTV point using os.scandir.

    By default bounds the scan window to recent date directories (today + yesterday)
    and any older date directory that still contains pending un-uploaded/un-archived work.
    """
    if not root.exists():
        return []

    if since_date is None:
        today = now_local().date()
        since_date = today - timedelta(days=1)

    matching_dirs: list[Path] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if not entry.is_dir() or entry.name in {"logs", "status"}:
                    continue

                folder_date = parse_date_dir_name(entry.name)
                if folder_date is None:
                    continue

                point_dir = Path(entry.path) / point.name
                has_videos = (point_dir / "videos").exists()
                has_encoded = (point_dir / "videos_encoded").exists()
                has_meta = (point_dir / "metadata").exists()

                if not (has_videos or has_encoded or has_meta):
                    continue

                if folder_date >= since_date:
                    matching_dirs.append(Path(entry.path))
                elif check_pending and has_pending_work(point_dir):
                    matching_dirs.append(Path(entry.path))
    except OSError:
        return []

    return sorted(matching_dirs, key=lambda p: p.name)


def ready_files(
    target_dir: Path | str,
    patterns: list[str] | tuple[str, ...],
    min_age_seconds: float | dict[str, float],
    *,
    skip_marker: str | None = None,
) -> list[Path]:
    """
    Scan a directory in a single scandir pass, checking size and mtime from DirEntry.stat().

    - patterns: list of extensions, e.g. ['.ts', '.mp4'] or ['*.ts', '*.mp4']
    - min_age_seconds: float/int or dict mapping extension (e.g. '.csv': 86400) to min age
    - skip_marker: if specified (e.g. '.uploaded'), files with a corresponding marker are skipped
    """
    target_path = Path(target_dir)
    if not target_path.is_dir():
        return []

    normalized_exts = {p.lstrip("*").lower() for p in patterns}
    current_ts = time.time()
    results: list[Path] = []

    try:
        with os.scandir(target_path) as it:
            entries = list(it)

            markers: set[str] = set()
            if skip_marker:
                markers = {e.name for e in entries if e.name.endswith(skip_marker)}

            for entry in entries:
                if not entry.is_file():
                    continue

                name = entry.name
                if name.startswith("."):
                    continue

                if skip_marker and name.endswith(skip_marker):
                    continue

                ext = Path(name).suffix.lower()
                if ext not in normalized_exts:
                    continue

                if skip_marker and f"{name}{skip_marker}" in markers:
                    continue

                try:
                    st = entry.stat()
                except OSError:
                    continue

                if st.st_size <= 0:
                    continue

                if isinstance(min_age_seconds, dict):
                    required_age = min_age_seconds.get(ext, min_age_seconds.get("*", 0.0))
                else:
                    required_age = float(min_age_seconds)

                if (current_ts - st.st_mtime) < required_age:
                    continue

                results.append(Path(entry.path))
    except OSError:
        return []

    return sorted(results, key=lambda p: p.name)
