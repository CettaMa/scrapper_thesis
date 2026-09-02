# CCTV 24×7 Scraper Refactor & Efficiency Pass

## 1. File Restructuring Map

The original 1,926-line monolith `main.py` has been decomposed into a clean, modular Python package (`cctv_scraper/`) and a thin CLI entrypoint:

| Module | Responsibility |
|---|---|
| `main.py` | Minimal 3-line application entrypoint. |
| `cctv_scraper/__init__.py` | Top-level exports and public API definitions. |
| `cctv_scraper/config.py` | Typed frozen sub-configs, table-driven env/CLI parser, `load_cctv_points`, sanitizers, validators. |
| `cctv_scraper/logging_setup.py` | Root and per-camera loggers with formatting and directory guarantees. |
| `cctv_scraper/storage_scan.py` | High-performance `os.scandir` utilities: single-pass file scanning, cached stat reads, date directory bounding, pending work detection, stderr trimmer. |
| `cctv_scraper/recorder.py` | `CCTVRecorder` thread managing FFmpeg subprocesses, preflight health checks, status CSV logging, and single-pass staleness detection. |
| `cctv_scraper/metadata.py` | `MetadataCollector` daemon thread with persistent session management, caching for TomTom & Open-Meteo APIs, and periodic CSV persistence. |
| `cctv_scraper/archive.py` | `ArchiveEncoder` thread grouping raw `.ts` segments into five-minute local MP4 files using FFmpeg QuickSync/NVENC/CPU encoding. Missing segments are not padded; available content represents the window. |
| `cctv_scraper/disk.py` | `DiskMonitor` thread checking free storage thresholds and pruning date directories past retention limits. |
| `cctv_scraper/doh.py` | `DoHResolver` with negative result caching, thread locks, size-bounded LRU/FIFO maps, and urllib3 connection monkeypatching. |
| `cctv_scraper/app.py` | `CCTVApp` coordinator validating hardware/software video encoders and orchestrating worker threads. |
| `cctv_scraper/cli.py` | Argument parser handling `--config`, `--output`, `--segment-seconds`, `--retention-days`, `--min-free-space-gb`, `--video-container`. |

---

## 2. Efficiency Gains Achieved

### Filesystem & I/O
- **Single-Pass Directory Scanning**: Replaced multi-stage `glob()` and redundant `os.stat()` / `Path.stat()` calls in recorder and archive scans with single `os.scandir` passes. Uses cached `DirEntry.stat()` to read `st_size` and `st_mtime` without additional OS syscalls.
- **Bounded Scan Window**: Archive scans `today`, `yesterday`, and only older date directories that contain raw segments awaiting local archiving.
- **Fast Retention Pruning**: Disk cleanup scans only date directory names via `scandir` without traversing sub-files.

### Network & DNS
- **Persistent HTTP Sessions**: `MetadataCollector` maintains a single `requests.Session` across the thread lifetime, enabling TCP connection pooling and TLS handshake reuse for TomTom and Open-Meteo API requests.
- **DoH Negative Caching**: `DoHResolver` now caches negative DNS lookups (when system DNS succeeds or when a host lookup fails) for the 5-minute TTL, eliminating repeated `getaddrinfo` syscalls on every socket creation for standard hosts.
- **Bounded In-Memory Maps**: `DoHResolver._cache` and `_doh_patched_hosts` are bounded to a maximum of 256 entries with thread locks and FIFO eviction, preventing memory leaks.

### Docker & Container Packaging
- **Multi-Stage Build**: Separated build dependencies into an Ubuntu `builder` stage, copying only the compiled `/opt/venv` into the final runtime image. Removed unnecessary package manager caches and dev tools.
- **Explicit `.dockerignore`**: Excluded `dataset/`, `logs/`, `status/`, `secrets/*.json`, `.git`, `.pytest_cache`, `.mypy_cache`, and `__pycache__` from Docker build context.
- **Image Size Reduction**: Estimated uncompressed image footprint reduced from ~1.45GB to ~1.28GB (~150MB+ savings).

---

## 3. Reconciled Defaults (Code vs `.env.example`)

Where hardcoded defaults in the codebase differed from `.env.example`, the code defaults were preserved as the source of truth and `.env.example` was updated accordingly:

| Setting | Code Default | Previous `.env.example` | Action / Reconciled Value |
|---|---|---|---|
| `SEGMENT_SECONDS` | `60` | `28` | Reconciled `.env.example` to `60` |
| `METADATA_INTERVAL_SECONDS` | `60` | `300` | Reconciled `.env.example` to `60` |
| `OPENMETEO_INTERVAL_SECONDS` | `60` | `300` | Reconciled `.env.example` to `60` |
| `VIDEO_ENCODER` | `"hevc_nvenc"` | `"libx264"` | Intel-only `.env.example` uses `h264_qsv`; the code default remains `hevc_nvenc` for backwards compatibility |
| `ARCHIVE_ENCODER_ENABLED` | `True` | `false` | Reconciled `.env.example` to `true` |
| `ARCHIVE_VIDEO_ENCODER` | `"h264_vaapi"` | `"libx264"` | Reconciled `.env.example` to `"h264_vaapi"`; startup falls back to `libx264` if the Intel device is unavailable |

---

## 4. Deprecations & Removals

1. **Removed Dead Config `TRANSCODE_CRF`**: `TRANSCODE_CRF` / `transcode_crf` was loaded into config and logged in startup routines, but never passed to FFmpeg (FFmpeg uses bitrate limits `-b:v`, `-maxrate:v`, `-bufsize:v`, and `-rc:v vbr`). Removed dead field completely.
2. **Specific Exception Handling**: Replaced bare `except:` and generic `except Exception:` with specific exceptions (`requests.RequestException`, `OSError`, `subprocess.TimeoutExpired`, `ValueError`, `KeyError`) across file operations, socket wrappers, and HTTP calls.
3. **Removed Outdated Version Strings**: Cleaned up legacy version identifiers (e.g. `v4`, `v7 Storage Optimized`) from log messages and comments.

---

## 5. Verification & Test Suite

All checks run with zero warnings/errors on Python 3.10 / 3.12:

- **Ruff Linter**: `ruff check .` -> `All checks passed!`
- **Ruff Formatter**: `ruff format --check .` -> all files already formatted
- **Mypy Type Checker**: `mypy` -> `Success: no issues found in 16 source files`
- **Pytest**: `python -m pytest -q` -> `39 passed`

### Test Suites Included:
- `tests/test_characterization.py`: FFmpeg argv byte-exact equality across copy/transcode/NVENC/libx264/ts/mp4, CSV parser variants, coordinate regex handling, duplicate/invalid URL rejections, archive windowing, archive provenance manifests, and local retention behavior.
- `tests/test_config.py` (4 tests): Sub-config dataclass instantiation, environment overrides, CLI precedence, validation error handling.
- `tests/test_storage_scan.py` (4 tests): Single-pass `ready_files` filtering, bounded `iter_point_date_dirs` with pending file pickup, single-pass `is_video_stale` detection, `trim_stderr`.
- `tests/test_doh_and_network.py` (4 tests): Negative DNS caching, positive DoH caching, FIFO cache eviction, `MetadataCollector` session reuse and cache interval verification.
