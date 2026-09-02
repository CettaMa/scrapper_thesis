import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cctv_scraper import (
    ArchiveConfig,
    ArchiveEncoder,
    CCTVPoint,
    CCTVRecorder,
    MetadataCollector,
    MetadataConfig,
    NetworkConfig,
    RecorderConfig,
    RuntimeConfig,
    StorageConfig,
    load_cctv_points,
    parse_coordinate,
    validate_video_encoder,
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
        min_pass_interval_seconds=5,
        failure_backoff_base_seconds=5,
        failure_backoff_max_seconds=300,
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
        fallback_video_encoder="libx264",
        preset="p4",
        target_bitrate="650k",
        max_bitrate="900k",
        buffer_size="1300k",
        output_height=0,
        output_fps=0,
        vaapi_device="/dev/dri/renderD128",
        retry_base_seconds=60,
        retry_max_seconds=3600,
        max_attempts=3,
    )
    storage_defaults: dict[str, Any] = dict(
        output_root=output_root,
        retention_days=7,
        min_free_space_gb=20.0,
        disk_check_seconds=300,
        log_max_bytes=10 * 1024 * 1024,
        log_backup_count=5,
    )
    network_defaults: dict[str, Any] = dict(
        preflight_check=True,
        offline_retry_seconds=300,
        network_retry_seconds=60,
        expired_url_escalation_threshold=3,
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
        elif k == "archive_output_fps":
            archive_defaults["output_fps"] = v
        elif k == "archive_vaapi_device":
            archive_defaults["vaapi_device"] = v
        elif k == "archive_retry_base_seconds":
            archive_defaults["retry_base_seconds"] = v
        elif k == "archive_retry_max_seconds":
            archive_defaults["retry_max_seconds"] = v
        elif k == "archive_max_attempts":
            archive_defaults["max_attempts"] = v
        elif k == "metadata_min_pass_interval_seconds":
            metadata_defaults["min_pass_interval_seconds"] = v
        elif k == "metadata_failure_backoff_base_seconds":
            metadata_defaults["failure_backoff_base_seconds"] = v
        elif k == "metadata_failure_backoff_max_seconds":
            metadata_defaults["failure_backoff_max_seconds"] = v
        elif k == "log_max_bytes":
            storage_defaults["log_max_bytes"] = v
        elif k == "log_backup_count":
            storage_defaults["log_backup_count"] = v
        elif k == "expired_url_escalation_threshold":
            network_defaults["expired_url_escalation_threshold"] = v
        elif k in storage_defaults:
            storage_defaults[k] = v
        elif k in network_defaults:
            network_defaults[k] = v

    return RuntimeConfig(
        config_file=config_file,
        recorder=RecorderConfig(**recorder_defaults),
        metadata=MetadataConfig(**metadata_defaults),
        archive=ArchiveConfig(**archive_defaults),
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
    assert points[0].lat == pytest.approx(cfg.metadata.default_lat)
    assert points[0].lon == pytest.approx(cfg.metadata.default_lon)


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
def test_archive_groups_by_segment_start_not_finish_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, archive_interval_seconds=300, archive_safe_age_seconds=90)
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    date_dir = tmp_path / "2026-06-21"
    raw_dir = date_dir / point.name / "videos"
    raw_dir.mkdir(parents=True)

    first_start = 300290
    second_start = 300350
    first = raw_dir / f"{point.name}_{datetime.fromtimestamp(first_start):%Y%m%d_%H%M%S}.ts"
    second = raw_dir / f"{point.name}_{datetime.fromtimestamp(second_start):%Y%m%d_%H%M%S}.ts"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    # The first segment finished after the 300000 boundary, but started before it.
    os.utime(first, (300350, 300350))
    os.utime(second, (300400, 300400))

    batches: list[tuple[int, list[str]]] = []
    monkeypatch.setattr(
        encoder,
        "encode_batch",
        lambda pt, enc_dir, ws, fls: batches.append((ws, [file.name for file in fls])),
    )
    with patch("time.time", return_value=301000):
        encoder.encode_point_date(point, date_dir)

    assert batches == [
        (300000, [first.name]),
        (300300, [second.name]),
    ]


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
    # Window 0 ends at 300300; cutoff = 300300 + segment_seconds 60 + safe_age 90 = 300450
    # At 300350 < 300450, Window 0 is NOT yet ready
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
    # Window 0 cutoff (300450) reached -> Window 0 encodes file1 and file2
    # Window 1 ends at 300600 (cutoff 300750) -> not yet ready
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

    # Test 3: Simulated time is 300800
    # Both Window 0 (300450) and Window 1 (300750) cutoffs have passed
    with patch("time.time", return_value=300800):
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
# 4. Local archive retention behavior
# ============================================================================
def test_archive_uses_qsv_and_represents_sparse_window(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, archive_video_encoder="h264_qsv", archive_preset="veryfast")
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    command = encoder.build_encode_command(tmp_path / "concat.txt", tmp_path / "window.mp4")

    assert "-c:v" in command
    assert command[command.index("-c:v") + 1] == "h264_qsv"
    assert "-fflags" in command
    assert "+genpts" in command
    assert "-avoid_negative_ts" in command
    assert "make_zero" in command


def test_archive_builds_exact_vaapi_command_with_device_before_input(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        archive_video_encoder="h264_vaapi",
        archive_vaapi_device="/dev/dri/renderD129",
    )
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    list_path = tmp_path / "concat.txt"
    output = tmp_path / "window.mp4"

    assert encoder.build_encode_command(list_path, output) == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-vaapi_device",
        "/dev/dri/renderD129",
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
        "-vf",
        "format=nv12,hwupload",
        "-c:v",
        "h264_vaapi",
        "-b:v",
        "650k",
        "-maxrate:v",
        "900k",
        "-bufsize:v",
        "1300k",
        "-movflags",
        "+faststart",
        str(output),
    ]


@pytest.mark.parametrize("hardware_encoder", ["h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi"])
def test_archive_retries_hardware_failure_with_fallback_and_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardware_encoder: str
) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        archive_video_encoder=hardware_encoder,
        archive_delete_raw_after_success=False,
    )
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    encoded_dir = tmp_path / "videos_encoded"
    encoded_dir.mkdir()
    raw_file = tmp_path / f"point_a_{datetime.fromtimestamp(300000):%Y%m%d_%H%M%S}.ts"
    raw_file.write_bytes(b"raw")
    commands: list[list[str]] = []

    def run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, "", "no device")
        Path(command[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(encoder, "run_ffmpeg", run_ffmpeg)
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])

    assert len(commands) == 2
    assert commands[0][commands[0].index("-c:v") + 1] == hardware_encoder
    assert commands[1][commands[1].index("-c:v") + 1] == "libx264"
    archives = list(encoded_dir.glob("*.mp4"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == b"mp4"
    manifest = json.loads(encoder.manifest_path(archives[0]).read_text(encoding="utf-8"))
    assert manifest["point_name"] == "point_a"
    assert manifest["window_start"] == 300000
    assert manifest["window_end"] == 300300
    assert manifest["segment_count"] == 1
    assert manifest["first_segment_start"] == 300000
    assert manifest["last_segment_start"] == 300000
    assert manifest["encoder"] == "libx264"
    assert manifest["expected_covered_duration_seconds"] == 300
    assert manifest["actual_covered_duration_seconds"] == 60


def test_archive_failure_backoff_marks_window_failed_and_preserves_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        archive_video_encoder="libx264",
        archive_retry_base_seconds=10,
        archive_retry_max_seconds=100,
        archive_max_attempts=3,
    )
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    encoded_dir = tmp_path / "videos_encoded"
    encoded_dir.mkdir()
    raw_file = tmp_path / "segment.ts"
    raw_file.write_bytes(b"raw")
    calls = 0
    clock = [100.0]

    def run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, "", "broken input")

    monkeypatch.setattr(encoder, "run_ffmpeg", run_ffmpeg)
    monkeypatch.setattr("cctv_scraper.archive.time.time", lambda: clock[0])

    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    marker = next(encoded_dir.glob("*.failure.json"))
    assert calls == 1
    assert marker.exists()
    assert '"attempts": 1' in marker.read_text(encoding="utf-8")

    clock[0] = 109
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 1
    clock[0] = 110
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 2
    clock[0] = 129
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 2
    clock[0] = 130
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 3
    encoder.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 3
    restarted = ArchiveEncoder([point], cfg, threading.Event())
    monkeypatch.setattr(restarted, "run_ffmpeg", run_ffmpeg)
    restarted.encode_batch(point, encoded_dir, 300000, [raw_file])
    assert calls == 3
    assert '"attempts": 3' in marker.read_text(encoding="utf-8")
    assert '"status": "failed"' in marker.read_text(encoding="utf-8")
    assert raw_file.exists()


def test_archive_deletes_raw_files_after_success(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, archive_delete_raw_after_success=True)
    encoder = ArchiveEncoder([point], cfg, threading.Event())
    raw_files = [tmp_path / "one.ts", tmp_path / "two.ts"]
    for path in raw_files:
        path.write_bytes(b"content")

    encoder.delete_raw_files(raw_files)

    assert all(not path.exists() for path in raw_files)


def test_archive_hardware_probe_switches_to_fallback(tmp_path: Path) -> None:
    cfg = make_dummy_config(tmp_path, archive_video_encoder="h264_vaapi")
    encoders = subprocess.CompletedProcess(["ffmpeg"], 0, "h264_vaapi libx264", "")
    probe = subprocess.CompletedProcess(["ffmpeg"], 1, "", "No VA display")

    with patch("cctv_scraper.app.subprocess.run", side_effect=[encoders, probe]) as run:
        validated = validate_video_encoder(cfg)

    assert validated.archive.video_encoder == "libx264"
    probe_command = run.call_args_list[1].args[0]
    assert probe_command[probe_command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert probe_command[probe_command.index("-t") + 1] == "1"


def test_archive_fps_filter_precedes_scale_and_hwupload(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(
        tmp_path,
        archive_video_encoder="h264_vaapi",
        archive_output_fps=10,
        archive_output_height=480,
    )
    encoder = ArchiveEncoder([point], cfg, threading.Event())

    command = encoder.build_encode_command(tmp_path / "concat.txt", tmp_path / "window.mp4")
    filters = command[command.index("-vf") + 1]

    # Dropping frames first keeps scaling and the hardware upload off discarded frames.
    assert filters == "fps=10,scale=-2:480,format=nv12,hwupload"


def test_metadata_row_names_the_archive_window_it_belongs_to(tmp_path: Path) -> None:
    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, archive_interval_seconds=300)
    collector = MetadataCollector([point], cfg, threading.Event())

    # A sample taken mid-window must name that window, not the nearest boundary.
    row = collector.build_metadata_row(point, datetime(2026, 9, 2, 17, 54, 31), {}, {}, 300)

    assert row["archive_window_start"] == "2026-09-02 17:50:00"
    assert row["archive_video"] == "point_a_20260902_175000_175500.mp4"

    encoder = ArchiveEncoder([point], cfg, threading.Event())
    window_start = int(datetime(2026, 9, 2, 17, 50, 0).timestamp())
    encoded_dir = tmp_path / "videos_encoded"
    encoded_dir.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "point_a_20260902_175431.ts"
    raw.write_bytes(b"x")

    # The encoder must land on exactly the filename the metadata row promised.
    captured: list[str] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured.append(command[-1])
        return subprocess.CompletedProcess(command, 1, "", "boom")

    encoder.run_ffmpeg = fake_run  # type: ignore[method-assign]
    encoder.encode_batch(point, encoded_dir, window_start, [raw])

    assert Path(captured[0]).name.endswith(".mp4")
    assert row["archive_video"] in Path(captured[0]).name


def test_late_segments_mark_window_incomplete_and_are_preserved(tmp_path: Path) -> None:
    from cctv_scraper.config import archive_window_filename

    point = CCTVPoint(name="point_a", url="https://stream.test/a.m3u8", lat=-6.85, lon=107.5)
    cfg = make_dummy_config(tmp_path, archive_interval_seconds=300)
    encoder = ArchiveEncoder([point], cfg, threading.Event())

    encoded_dir = tmp_path / "videos_encoded"
    encoded_dir.mkdir(parents=True, exist_ok=True)
    window_start = 300000
    output = encoded_dir / archive_window_filename(point.name, window_start, 300)
    output.write_bytes(b"already encoded")

    late = tmp_path / "point_a_20260902_175431.ts"
    late.write_bytes(b"footage the archive does not have")

    def fail_if_called(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("an already-encoded window must not be re-encoded")

    encoder.run_ffmpeg = fail_if_called  # type: ignore[method-assign]
    encoder.encode_batch(point, encoded_dir, window_start, [late])

    # The raw segment holds footage the MP4 lacks, so it must survive.
    assert late.exists()

    manifest = json.loads((encoded_dir / f".{output.name}.manifest.json").read_text("utf-8"))
    assert manifest["complete"] is False
    assert manifest["late_segments"] == [late.name]
