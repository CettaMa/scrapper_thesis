import logging
import os
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cctv_scraper.config import CCTVPoint, now_local
from cctv_scraper.disk import DiskMonitor
from cctv_scraper.logging_setup import point_logger, setup_logging
from cctv_scraper.recorder import CCTVRecorder
from cctv_scraper.storage_scan import (
    has_pending_work,
    iter_point_date_dirs,
    ready_files,
    trim_stderr,
)
from tests.test_characterization import make_dummy_config


def test_trim_stderr():
    stderr_text = "\n".join([f"line {i}" for i in range(20)])
    trimmed = trim_stderr(stderr_text, max_lines=5)
    assert trimmed == "line 15 | line 16 | line 17 | line 18 | line 19"
    assert trim_stderr("") == ""


def test_ready_files_single_pass(tmp_path: Path):
    target = tmp_path / "videos"
    target.mkdir()

    file_ready = target / "cam_01.ts"
    file_new = target / "cam_02.ts"
    file_empty = target / "cam_03.ts"
    file_dot = target / ".hidden.ts"
    file_other = target / "notes.txt"

    file_ready.write_bytes(b"data1")
    file_new.write_bytes(b"data2")
    file_empty.write_bytes(b"")
    file_dot.write_bytes(b"hidden")
    file_other.write_text("notes", encoding="utf-8")

    now = 500000.0
    os.utime(file_ready, (now - 120, now - 120))  # age = 120s
    os.utime(file_new, (now - 30, now - 30))  # age = 30s
    os.utime(file_empty, (now - 120, now - 120))

    with patch("time.time", return_value=now):
        res = ready_files(target, [".ts"], min_age_seconds=90)
        assert res == [file_ready]


def test_iter_point_date_dirs_bounded_and_pending_pickup(tmp_path: Path):
    point = CCTVPoint(name="point_a", url="http://example.com/live.m3u8", lat=-6.85, lon=107.5)

    today = now_local().date()
    yesterday = today - timedelta(days=1)
    old_date_pending = today - timedelta(days=5)
    old_date_done = today - timedelta(days=6)

    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    old_pending_str = old_date_pending.strftime("%Y-%m-%d")
    old_done_str = old_date_done.strftime("%Y-%m-%d")

    # Today dir: has video
    (tmp_path / today_str / point.name / "videos").mkdir(parents=True)
    (tmp_path / today_str / point.name / "videos" / "v_today.ts").write_bytes(b"123")

    # Yesterday dir: has video
    (tmp_path / yesterday_str / point.name / "videos").mkdir(parents=True)
    (tmp_path / yesterday_str / point.name / "videos" / "v_yesterday.ts").write_bytes(b"123")

    # Old date with pending file (no marker)
    (tmp_path / old_pending_str / point.name / "videos").mkdir(parents=True)
    (tmp_path / old_pending_str / point.name / "videos" / "v_old_pending.ts").write_bytes(b"123")

    # Old date with only an archive is complete and needs no further processing.
    (tmp_path / old_done_str / point.name / "videos_encoded").mkdir(parents=True)
    (tmp_path / old_done_str / point.name / "videos_encoded" / "archive.mp4").write_bytes(b"123")

    # Logs and status dirs should be ignored
    (tmp_path / "logs").mkdir()
    (tmp_path / "status").mkdir()

    # Check pending detector directly
    assert has_pending_work(tmp_path / old_pending_str / point.name) is True
    assert has_pending_work(tmp_path / old_done_str / point.name) is False

    # Scan dirs
    matched = iter_point_date_dirs(tmp_path, point)
    matched_names = [p.name for p in matched]

    # Must contain today, yesterday, and the old pending date dir; but MUST NOT contain the old done date dir
    assert today_str in matched_names
    assert yesterday_str in matched_names
    assert old_pending_str in matched_names
    assert old_done_str not in matched_names


def test_disk_emergency_purge_deletes_oldest_and_keeps_today(tmp_path: Path, monkeypatch):
    point = CCTVPoint(name="point_a", url="http://example.com/live.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, min_free_space_gb=10.0)
    monitor = DiskMonitor(cfg, threading.Event())
    today = now_local().date()
    old_dates = [today - timedelta(days=3), today - timedelta(days=2)]
    for folder_date in [*old_dates, today]:
        (tmp_path / folder_date.strftime("%Y-%m-%d") / point.name).mkdir(parents=True)

    usage = [
        SimpleNamespace(free=1 * 1024**3),
        SimpleNamespace(free=11 * 1024**3),
    ]
    monkeypatch.setattr("cctv_scraper.disk.shutil.disk_usage", lambda _: usage.pop(0))
    monitor.check_disk()

    assert not (tmp_path / old_dates[0].strftime("%Y-%m-%d")).exists()
    assert (tmp_path / old_dates[1].strftime("%Y-%m-%d")).exists()
    assert (tmp_path / today.strftime("%Y-%m-%d")).exists()


def test_cleanup_prunes_old_logs_and_status_files(tmp_path: Path):
    cfg = make_dummy_config(tmp_path, retention_days=7)
    monitor = DiskMonitor(cfg, threading.Event())
    old_timestamp = time.time() - 8 * 24 * 60 * 60
    old_log = tmp_path / "logs" / "ffmpeg" / "point_2020.ffmpeg.log"
    new_log = tmp_path / "logs" / "scraper.log"
    old_status = tmp_path / "status" / "point_status.csv"
    for path in [old_log, new_log, old_status]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data", encoding="utf-8")
    os.utime(old_log, (old_timestamp, old_timestamp))
    os.utime(old_status, (old_timestamp, old_timestamp))

    monitor.cleanup_old_folders()

    assert not old_log.exists()
    assert not old_status.exists()
    assert new_log.exists()


def test_logging_uses_configured_rotating_handlers(tmp_path: Path):
    setup_logging(tmp_path, max_bytes=1234, backup_count=2)
    root = logging.getLogger()
    root_file_handlers = [
        handler for handler in root.handlers if isinstance(handler, RotatingFileHandler)
    ]
    assert len(root_file_handlers) == 1
    assert root_file_handlers[0].maxBytes == 1234
    assert root_file_handlers[0].backupCount == 2

    logger = point_logger(tmp_path, "rotation_point", max_bytes=5678, backup_count=3)
    point_handlers = [
        handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
    ]
    assert len(point_handlers) == 1
    assert point_handlers[0].maxBytes == 5678
    assert point_handlers[0].backupCount == 3

    for handler in [*root.handlers, *logger.handlers]:
        handler.close()


def test_recorder_escalates_repeated_expired_url_failures(tmp_path: Path, caplog):
    point = CCTVPoint(
        name="expired_status_point",
        url="http://example.com/live.m3u8",
        lat=-6.85,
        lon=107.5,
    )
    cfg = make_dummy_config(tmp_path, expired_url_escalation_threshold=3)
    recorder = CCTVRecorder(point, cfg, threading.Event())
    caplog.set_level(logging.ERROR, logger=recorder.logger.name)

    for _ in range(4):
        recorder.handle_preflight_failure("not_found_or_expired_url", "404")

    status_text = recorder.status_file().read_text(encoding="utf-8")
    assert status_text.count("EXPIRED_URL_ESCALATION") == 1
    assert "expired_url_escalated" in status_text
    assert "[EXPIRED_URL_ESCALATION]" in caplog.text


def test_recorder_single_pass_latest_video(tmp_path: Path):
    point = CCTVPoint(name="point_test", url="http://example.com/live.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, stale_file_seconds=120)
    stop_event = threading.Event()
    recorder = CCTVRecorder(point, cfg, stop_event)

    video_dir = recorder.current_video_dir()
    v1 = video_dir / "point_test_20260621_100000.ts"
    v2 = video_dir / "point_test_20260621_100100.ts"
    other_cam = video_dir / "other_cam_20260621_100200.ts"

    v1.write_bytes(b"x" * 200000)
    v2.write_bytes(b"y" * 200000)
    other_cam.write_bytes(b"z" * 200000)

    now = 500000.0
    os.utime(v1, (now - 200, now - 200))
    os.utime(v2, (now - 50, now - 50))
    os.utime(other_cam, (now - 10, now - 10))

    recorder.last_restart_at = datetime.fromtimestamp(now - 300)

    with patch("time.time", return_value=now):
        latest = recorder.latest_video_file()
        assert latest == v2
        assert recorder.is_video_stale() is False

    # Simulate stale: now is 500300 (250s since v2 was modified > 120s stale_file_seconds)
    with patch("time.time", return_value=now + 200):
        assert recorder.is_video_stale() is True
