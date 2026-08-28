import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cctv_scraper import (
    ArchiveConfig,
    ArchiveEncoder,
    CCTVPoint,
    CCTVRecorder,
    DriveConfig,
    GoogleDriveUploader,
    MetadataConfig,
    NetworkConfig,
    RecorderConfig,
    RuntimeConfig,
    StorageConfig,
    load_cctv_points,
    parse_coordinate,
)


def make_dummy_config(output_root: Path, **kwargs: Any) -> RuntimeConfig:
    recorder_defaults: dict[str, Any] = dict(
        segment_seconds=60,
        video_container="ts",
        restart_delay_seconds=5,
        health_check_seconds=30,
        stale_file_seconds=240,
        ffmpeg_loglevel="warning",
        ffmpeg_transport_mode="copy",
        ffmpeg_user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        ffmpeg_referer="",
        ffmpeg_origin="",
        ffmpeg_analyzeduration="10000000",
        ffmpeg_probesize="10000000",
        hls_reconnect_at_eof=False,
        segment_atclocktime=False,
        hls_live_start_index="-1",
        ffmpeg_rw_timeout="10000000",
        ffmpeg_reconnect_delay_max="10",
        ffmpeg_reconnect_on_http_error="5xx",
        output_fps=10,
        transcode_preset="p4",
        segment_keyframe_seconds=2,
        video_encoder="hevc_nvenc",
        target_bitrate="650k",
        max_bitrate="900k",
        buffer_size="1300k",
        output_height=0,
    )
    metadata_defaults: dict[str, Any] = dict(
        metadata_interval_seconds=60,
        tomtom_interval_seconds=300,
        openmeteo_interval_seconds=60,
        default_lat=-6.851117,
        default_lon=107.496586,
        tomtom_api_key=None,
    )
    archive_defaults: dict[str, Any] = dict(
        enabled=True,
        interval_seconds=300,
        scan_seconds=60,
        safe_age_seconds=90,
        delete_raw_after_success=True,
        video_encoder="hevc_nvenc",
        preset="p4",
        target_bitrate="650k",
        max_bitrate="900k",
        buffer_size="1300k",
        output_height=0,
    )
    drive_defaults: dict[str, Any] = dict(
        enabled=False,
        auth_file=output_root / "secrets" / "token.json",
        folder_id="",
        scan_seconds=60,
        safe_age_seconds=90,
        delete_local_after_upload=False,
    )
    storage_defaults: dict[str, Any] = dict(
        output_root=output_root,
        retention_days=7,
        min_free_space_gb=20.0,
        disk_check_seconds=300,
    )
    network_defaults: dict[str, Any] = dict(
        preflight_check=True,
        offline_retry_seconds=300,
        network_retry_seconds=60,
    )

    config_file = kwargs.pop("config_file", output_root / "cctv_points.csv")

    for k, v in kwargs.items():
        if k in recorder_defaults:
            recorder_defaults[k] = v
        elif k in metadata_defaults:
            metadata_defaults[k] = v
        elif k in archive_defaults:
            archive_defaults[k] = v
        elif k == "archive_encoder_enabled":
            archive_defaults["enabled"] = v
        elif k == "archive_interval_seconds":
            archive_defaults["interval_seconds"] = v
        elif k == "archive_scan_seconds":
            archive_defaults["scan_seconds"] = v
        elif k == "archive_safe_age_seconds":
            archive_defaults["safe_age_seconds"] = v
        elif k == "archive_delete_raw_after_success":
            archive_defaults["delete_raw_after_success"] = v
        elif k == "archive_video_encoder":
            archive_defaults["video_encoder"] = v
        elif k == "archive_preset":
            archive_defaults["preset"] = v
        elif k == "archive_target_bitrate":
            archive_defaults["target_bitrate"] = v
        elif k == "archive_max_bitrate":
            archive_defaults["max_bitrate"] = v
        elif k == "archive_buffer_size":
            archive_defaults["buffer_size"] = v
        elif k == "archive_output_height":
            archive_defaults["output_height"] = v
        elif k in drive_defaults:
            drive_defaults[k] = v
        elif k == "drive_upload_enabled":
            drive_defaults["enabled"] = v
        elif k == "drive_auth_file":
            drive_defaults["auth_file"] = v
        elif k == "drive_folder_id":
            drive_defaults["folder_id"] = v
        elif k == "drive_scan_seconds":
            drive_defaults["scan_seconds"] = v
        elif k == "drive_safe_age_seconds":
            drive_defaults["safe_age_seconds"] = v
        elif k == "drive_delete_local_after_upload":
            drive_defaults["delete_local_after_upload"] = v
        elif k in storage_defaults:
            storage_defaults[k] = v
        elif k in network_defaults:
            network_defaults[k] = v

    return RuntimeConfig(
        config_file=config_file,
        recorder=RecorderConfig(**recorder_defaults),
        metadata=MetadataConfig(**metadata_defaults),
        archive=ArchiveConfig(**archive_defaults),
        drive=DriveConfig(**drive_defaults),
        storage=StorageConfig(**storage_defaults),
        network=NetworkConfig(**network_defaults),
    )


# ============================================================================
# 1. load_cctv_points & parse_coordinate tests
# ============================================================================
def test_parse_coordinate() -> None:
    fallback = -6.851117
    # Plain numbers
    assert parse_coordinate("-6.851117", fallback) == pytest.approx(-6.851117)
    assert parse_coordinate(107.496586, fallback) == pytest.approx(107.496586)
    # Messy comma coordinate
    assert parse_coordinate("107.4967733,237", fallback) == pytest.approx(107.4967733)
    assert parse_coordinate("-6.851117,999", fallback) == pytest.approx(-6.851117)
    # Invalid fallback
    assert parse_coordinate("invalid", fallback) == pytest.approx(fallback)
    assert parse_coordinate("", fallback) == pytest.approx(fallback)


def test_load_cctv_points_with_header(tmp_path: Path) -> None:
    csv_file = tmp_path / "cctv_points.csv"
    csv_file.write_text(
        "name,url,lat,lon\n"
        "Cam Alpha,http://example.com/cam1.m3u8,-6.123,107.456\n"
        "Cam Beta,https://example.com/cam2.m3u8,-6.789,107.999\n",
        encoding="utf-8",
    )
    cfg = make_dummy_config(tmp_path, config_file=csv_file)
    points = load_cctv_points(cfg)

    assert len(points) == 2
    assert points[0].name == "cam_alpha"
    assert points[0].url == "http://example.com/cam1.m3u8"
    assert points[0].lat == pytest.approx(-6.123)
    assert points[0].lon == pytest.approx(107.456)

    assert points[1].name == "cam_beta"
    assert points[1].url == "https://example.com/cam2.m3u8"
    assert points[1].lat == pytest.approx(-6.789)
    assert points[1].lon == pytest.approx(107.999)


def test_load_cctv_points_alternative_headers(tmp_path: Path) -> None:
    csv_file = tmp_path / "cctv_points.csv"
    csv_file.write_text(
        "name,link,latitude,longitude\nSimpang Tiga,http://example.com/s3.m3u8,-6.500,107.200\n",
        encoding="utf-8",
    )
    cfg = make_dummy_config(tmp_path, config_file=csv_file)
    points = load_cctv_points(cfg)

    assert len(points) == 1
    assert points[0].name == "simpang_tiga"
    assert points[0].url == "http://example.com/s3.m3u8"
    assert points[0].lat == pytest.approx(-6.500)
    assert points[0].lon == pytest.approx(107.200)


def test_load_cctv_points_headerless(tmp_path: Path) -> None:
    csv_file = tmp_path / "cctv_points.csv"
    csv_file.write_text(
        "cam_one,http://example.com/one.m3u8\ncam_two,https://example.com/two.m3u8\n",
        encoding="utf-8",
    )
    cfg = make_dummy_config(tmp_path, config_file=csv_file)
    points = load_cctv_points(cfg)

    assert len(points) == 2
    assert points[0].name == "cam_one"
    assert points[0].url == "http://example.com/one.m3u8"
    assert points[0].lat == pytest.approx(cfg.default_lat)
    assert points[0].lon == pytest.approx(cfg.default_lon)


def test_load_cctv_points_duplicate_name_rejection(tmp_path: Path) -> None:
    csv_file = tmp_path / "cctv_points.csv"
    csv_file.write_text(
        "name,url\nCam 1,http://example.com/1.m3u8\ncam_1,http://example.com/2.m3u8\n",
        encoding="utf-8",
    )
    cfg = make_dummy_config(tmp_path, config_file=csv_file)
    with pytest.raises(ValueError, match="duplikat"):
        load_cctv_points(cfg)


def test_load_cctv_points_invalid_url_rejection(tmp_path: Path) -> None:
    csv_file = tmp_path / "cctv_points.csv"
    csv_file.write_text(
        "name,url\nCam-1,rtsp://example.com/1\n",
        encoding="utf-8",
    )
    cfg = make_dummy_config(tmp_path, config_file=csv_file)
    with pytest.raises(ValueError, match="URL CCTV tidak valid"):
        load_cctv_points(cfg)


# ============================================================================
# 2. build_ffmpeg_command tests
# ============================================================================
def test_build_ffmpeg_command_copy_m3u8(tmp_path: Path) -> None:
    point = CCTVPoint(name="padalarang", url="https://stream.test/live.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        video_container="ts",
        ffmpeg_transport_mode="copy",
        segment_seconds=60,
        ffmpeg_loglevel="warning",
        hls_reconnect_at_eof=False,
        segment_atclocktime=False,
    )
    stop_event = threading.Event()
    recorder = CCTVRecorder(point, cfg, stop_event)
    cmd = recorder.build_ffmpeg_command()

    expected_output_pattern = str(
        tmp_path
        / recorder.current_date_folder()
        / "padalarang"
        / "videos"
        / "padalarang_%Y%m%d_%H%M%S.ts"
    )

    expected = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt+nobuffer",
        "-err_detect",
        "ignore_err",
        "-rw_timeout",
        "10000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "5xx",
        "-reconnect_delay_max",
        "10",
        "-http_persistent",
        "0",
        "-multiple_requests",
        "0",
        "-user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "-headers",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
        "-analyzeduration",
        "10000000",
        "-probesize",
        "10000000",
        "-live_start_index",
        "-1",
        "-i",
        "https://stream.test/live.m3u8",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-max_muxing_queue_size",
        "1024",
        "-f",
        "segment",
        "-segment_time",
        "60",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-segment_format",
        "mpegts",
        expected_output_pattern,
    ]
    assert cmd == expected


def test_build_ffmpeg_command_transcode_hevc_nvenc(tmp_path: Path) -> None:
    point = CCTVPoint(name="padalarang", url="https://stream.test/live.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        video_container="ts",
        ffmpeg_transport_mode="transcode",
        video_encoder="hevc_nvenc",
        output_fps=10,
        segment_keyframe_seconds=2,
        transcode_preset="p4",
        target_bitrate="650k",
        max_bitrate="900k",
        buffer_size="1300k",
        output_height=720,
        hls_reconnect_at_eof=True,
        segment_atclocktime=True,
    )
    stop_event = threading.Event()
    recorder = CCTVRecorder(point, cfg, stop_event)
    cmd = recorder.build_ffmpeg_command()

    expected_output_pattern = str(
        tmp_path
        / recorder.current_date_folder()
        / "padalarang"
        / "videos"
        / "padalarang_%Y%m%d_%H%M%S.ts"
    )

    expected = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt+nobuffer",
        "-err_detect",
        "ignore_err",
        "-rw_timeout",
        "10000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "5xx",
        "-reconnect_delay_max",
        "10",
        "-http_persistent",
        "0",
        "-multiple_requests",
        "0",
        "-user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "-headers",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
        "-analyzeduration",
        "10000000",
        "-probesize",
        "10000000",
        "-reconnect_at_eof",
        "1",
        "-live_start_index",
        "-1",
        "-i",
        "https://stream.test/live.m3u8",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "fps=10,scale=-2:720,format=yuv420p",
        "-fps_mode",
        "cfr",
        "-c:v",
        "hevc_nvenc",
        "-preset",
        "p4",
        "-rc:v",
        "vbr",
        "-b:v",
        "650k",
        "-maxrate:v",
        "900k",
        "-bufsize:v",
        "1300k",
        "-g",
        "20",
        "-keyint_min",
        "20",
        "-sc_threshold",
        "0",
        "-max_muxing_queue_size",
        "1024",
        "-f",
        "segment",
        "-segment_time",
        "60",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-segment_atclocktime",
        "1",
        "-segment_format",
        "mpegts",
        expected_output_pattern,
    ]
    assert cmd == expected


def test_build_ffmpeg_command_transcode_libx264(tmp_path: Path) -> None:
    point = CCTVPoint(name="padalarang", url="https://stream.test/live.flv", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        video_container="ts",
        ffmpeg_transport_mode="transcode",
        video_encoder="libx264",
        output_fps=15,
        segment_keyframe_seconds=2,
        transcode_preset="p4",  # Should convert to "veryfast" for cpu encoder
        target_bitrate="650k",
        max_bitrate="900k",
        buffer_size="1300k",
        output_height=0,
    )
    stop_event = threading.Event()
    recorder = CCTVRecorder(point, cfg, stop_event)
    cmd = recorder.build_ffmpeg_command()

    expected_output_pattern = str(
        tmp_path
        / recorder.current_date_folder()
        / "padalarang"
        / "videos"
        / "padalarang_%Y%m%d_%H%M%S.ts"
    )

    # Note: url is not .m3u8, so -live_start_index is omitted
    expected = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt+nobuffer",
        "-err_detect",
        "ignore_err",
        "-rw_timeout",
        "10000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "5xx",
        "-reconnect_delay_max",
        "10",
        "-http_persistent",
        "0",
        "-multiple_requests",
        "0",
        "-user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "-headers",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
        "-analyzeduration",
        "10000000",
        "-probesize",
        "10000000",
        "-i",
        "https://stream.test/live.flv",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "fps=15,format=yuv420p",
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "650k",
        "-maxrate:v",
        "900k",
        "-bufsize:v",
        "1300k",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-max_muxing_queue_size",
        "1024",
        "-f",
        "segment",
        "-segment_time",
        "60",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-segment_format",
        "mpegts",
        expected_output_pattern,
    ]
    assert cmd == expected


def test_build_ffmpeg_command_mp4_container(tmp_path: Path) -> None:
    point = CCTVPoint(name="padalarang", url="https://stream.test/live.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        video_container="mp4",
        ffmpeg_transport_mode="copy",
    )
    stop_event = threading.Event()
    recorder = CCTVRecorder(point, cfg, stop_event)
    cmd = recorder.build_ffmpeg_command()

    expected_output_pattern = str(
        tmp_path
        / recorder.current_date_folder()
        / "padalarang"
        / "videos"
        / "padalarang_%Y%m%d_%H%M%S.mp4"
    )

    assert "-segment_format" in cmd
    idx = cmd.index("-segment_format")
    assert cmd[idx : idx + 4] == ["-segment_format", "mp4", "-movflags", "+faststart"]
    assert cmd[-1] == expected_output_pattern


# ============================================================================
# 3. ArchiveEncoder window grouping test
# ============================================================================
def test_archive_encoder_window_grouping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        archive_interval_seconds=300,
        archive_safe_age_seconds=90,
    )
    stop_event = threading.Event()
    encoder = ArchiveEncoder([point], cfg, stop_event)

    date_str = "2026-06-21"
    raw_dir = tmp_path / date_str / point.name / "videos"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Window 0: [300000, 300300)
    # Window 1: [300300, 300600)
    file1 = raw_dir / "point_a_01.ts"
    file2 = raw_dir / "point_a_02.ts"
    file3 = raw_dir / "point_a_03.ts"

    file1.write_bytes(b"dummy1")
    file2.write_bytes(b"dummy2")
    file3.write_bytes(b"dummy3")

    # Set mtimes
    os.utime(file1, (300050, 300050))
    os.utime(file2, (300100, 300100))
    os.utime(file3, (300350, 300350))

    # Test 1: Simulated time is 300350
    # Window 0 ends at 300300, safe cutoff = 300300 + 90 = 300390
    # At 300350 < 300390, Window 0 is NOT yet ready
    with patch("time.time", return_value=300350):
        batches_encoded: list[tuple[int, list[str]]] = []
        monkeypatch.setattr(
            encoder,
            "encode_batch",
            lambda pt, enc_dir, ws, fls: batches_encoded.append((ws, [f.name for f in fls])),
        )
        encoder.encode_point_date(point, tmp_path / date_str)
        assert len(batches_encoded) == 0

    # Test 2: Simulated time is 300450
    # Window 0 safe cutoff (300390) passed -> Window 0 encodes file1 and file2
    # Window 1 ends at 300600 (safe cutoff 300690) -> not yet ready
    with patch("time.time", return_value=300450):
        batches_encoded = []
        monkeypatch.setattr(
            encoder,
            "encode_batch",
            lambda pt, enc_dir, ws, fls: batches_encoded.append((ws, [f.name for f in fls])),
        )
        encoder.encode_point_date(point, tmp_path / date_str)
        assert len(batches_encoded) == 1
        ws, files = batches_encoded[0]
        assert ws == 300000
        assert set(files) == {"point_a_01.ts", "point_a_02.ts"}

    # Test 3: Simulated time is 300700
    # Both Window 0 and Window 1 safe cutoffs have passed
    with patch("time.time", return_value=300700):
        batches_encoded = []
        monkeypatch.setattr(
            encoder,
            "encode_batch",
            lambda pt, enc_dir, ws, fls: batches_encoded.append((ws, [f.name for f in fls])),
        )
        encoder.encode_point_date(point, tmp_path / date_str)
        assert len(batches_encoded) == 2
        assert batches_encoded[0][0] == 300000
        assert set(batches_encoded[0][1]) == {"point_a_01.ts", "point_a_02.ts"}
        assert batches_encoded[1][0] == 300300
        assert set(batches_encoded[1][1]) == {"point_a_03.ts"}


# ============================================================================
# 4. GoogleDriveUploader.ready_files cutoff logic test
# ============================================================================
def test_google_drive_uploader_ready_files(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        drive_safe_age_seconds=90,
    )
    stop_event = threading.Event()
    uploader = GoogleDriveUploader([point], cfg, stop_event)

    video_dir = tmp_path / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    # Videos: safe age is 90s
    v_old = video_dir / "old.ts"
    v_new = video_dir / "new.ts"
    v_empty = video_dir / "empty.ts"
    v_uploaded = video_dir / "done.ts"
    v_uploaded_marker = video_dir / "done.ts.uploaded"

    v_old.write_bytes(b"content")
    v_new.write_bytes(b"content")
    v_empty.write_bytes(b"")
    v_uploaded.write_bytes(b"content")
    v_uploaded_marker.write_text("2026-06-21,file_id", encoding="utf-8")

    current_ts = 1000000.0
    os.utime(v_old, (current_ts - 100, current_ts - 100))  # 100s old > 90s safe age
    os.utime(v_new, (current_ts - 30, current_ts - 30))  # 30s old < 90s safe age
    os.utime(v_empty, (current_ts - 100, current_ts - 100))
    os.utime(v_uploaded, (current_ts - 100, current_ts - 100))

    # CSV: safe age is 86400s (24h)
    csv_old = metadata_dir / "old.csv"
    csv_new = metadata_dir / "new.csv"
    csv_old.write_text("col1,col2\n1,2\n", encoding="utf-8")
    csv_new.write_text("col1,col2\n1,2\n", encoding="utf-8")
    os.utime(csv_old, (current_ts - 90000, current_ts - 90000))  # 90000s > 86400s
    os.utime(csv_new, (current_ts - 1000, current_ts - 1000))  # 1000s < 86400s

    with patch("time.time", return_value=current_ts):
        ready_videos = uploader.ready_files(video_dir, ["*.ts", "*.mp4"])
        assert ready_videos == [v_old]

        ready_csvs = uploader.ready_files(metadata_dir, ["*.csv"])
        assert ready_csvs == [csv_old]
