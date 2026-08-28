import csv
import logging
import threading
import time
from datetime import datetime

import requests

from cctv_scraper.config import (
    API_TIMEOUT_SECONDS,
    CCTVPoint,
    RuntimeConfig,
    ensure_dir,
    now_local,
)


class MetadataCollector(threading.Thread):
    """
    Mengambil metadata eksternal per titik.

    v4:
    - TomTom dipanggil default setiap 300 detik / 5 menit.
    - Open-Meteo dipanggil default setiap 60 detik.
    - CSV metadata tetap ditulis setiap METADATA_INTERVAL_SECONDS.
    - Jika belum waktunya call ulang, nilai terakhir dipakai kembali dan diberi status cached.
    """

    def __init__(self, points: list[CCTVPoint], config: RuntimeConfig, stop_event: threading.Event):
        super().__init__(name="metadata-collector", daemon=True)
        self.points = points
        self.config = config
        self.stop_event = stop_event
        self.logger = logging.getLogger("metadata")

        self.tomtom_cache: dict[str, dict] = {}
        self.tomtom_last_fetch: dict[str, float] = {}

        self.openmeteo_cache: dict[str, dict] = {}
        self.openmeteo_last_fetch: dict[str, float] = {}

    def run(self) -> None:
        self.logger.info("Metadata collector started.")
        self.logger.info(
            "Metadata CSV write interval: %s seconds", self.config.metadata_interval_seconds
        )
        self.logger.info("TomTom API interval: %s seconds", self.config.tomtom_interval_seconds)
        self.logger.info(
            "Open-Meteo API interval: %s seconds", self.config.openmeteo_interval_seconds
        )

        while not self.stop_event.is_set():
            start = time.time()
            timestamp = now_local()

            for point in self.points:
                try:
                    metadata = self.collect_point_metadata(point, timestamp)
                    self.write_metadata(point, metadata)
                except Exception as exc:
                    self.logger.exception("Metadata failed for %s: %s", point.name, exc)

            elapsed = time.time() - start
            sleep_time = max(0, self.config.metadata_interval_seconds - elapsed)
            self.stop_event.wait(sleep_time)

        self.logger.info("Metadata collector stopped.")

    def should_fetch(
        self, cache_key: str, last_fetch: dict[str, float], interval_seconds: int
    ) -> bool:
        if cache_key not in last_fetch:
            return True

        return (time.time() - last_fetch[cache_key]) >= interval_seconds

    def collect_point_metadata(self, point: CCTVPoint, timestamp: datetime) -> dict:
        with requests.Session() as session:
            tomtom = self.get_tomtom_cached(session, point)
            weather = self.get_openmeteo_cached(session, point)

        row = {
            "cctv_name": point.name,
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "date": timestamp.strftime("%Y-%m-%d"),
            "time": timestamp.strftime("%H:%M:%S"),
            "datestamp": timestamp.strftime("%Y%m%d"),
            "timestampsafe": timestamp.strftime("%H%M%S"),
            "latitude": point.lat,
            "longitude": point.lon,
            "stream_url": point.url,
        }
        row.update(tomtom)
        row.update(weather)
        return row

    def get_tomtom_cached(self, session: requests.Session, point: CCTVPoint) -> dict:
        cache_key = point.name

        if self.should_fetch(
            cache_key, self.tomtom_last_fetch, self.config.tomtom_interval_seconds
        ):
            data = self.get_tomtom(session, point)
            data["traffic_cache_status"] = "fresh"
            data["traffic_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")

            self.tomtom_cache[cache_key] = data
            self.tomtom_last_fetch[cache_key] = time.time()
            return data

        cached = self.tomtom_cache.get(cache_key)
        if cached:
            data = dict(cached)
            data["traffic_cache_status"] = "cached"
            return data

        # Safety fallback. Normally unreachable because first call should fetch.
        data = self.get_tomtom(session, point)
        data["traffic_cache_status"] = "fresh_fallback"
        data["traffic_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        self.tomtom_cache[cache_key] = data
        self.tomtom_last_fetch[cache_key] = time.time()
        return data

    def get_openmeteo_cached(self, session: requests.Session, point: CCTVPoint) -> dict:
        cache_key = point.name

        if self.should_fetch(
            cache_key, self.openmeteo_last_fetch, self.config.openmeteo_interval_seconds
        ):
            data = self.get_openmeteo(session, point)
            data["weather_cache_status"] = "fresh"
            data["weather_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")

            self.openmeteo_cache[cache_key] = data
            self.openmeteo_last_fetch[cache_key] = time.time()
            return data

        cached = self.openmeteo_cache.get(cache_key)
        if cached:
            data = dict(cached)
            data["weather_cache_status"] = "cached"
            return data

        data = self.get_openmeteo(session, point)
        data["weather_cache_status"] = "fresh_fallback"
        data["weather_last_api_call"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        self.openmeteo_cache[cache_key] = data
        self.openmeteo_last_fetch[cache_key] = time.time()
        return data

    def get_tomtom(self, session: requests.Session, point: CCTVPoint) -> dict:
        default = {
            "traffic_speed": None,
            "traffic_freeflow": None,
            "traffic_confidence": None,
            "traffic_road_closure": None,
            "traffic_source": "tomtom",
            "traffic_status": "missing_api_key" if not self.config.tomtom_api_key else "error",
        }

        if not self.config.tomtom_api_key:
            return default

        url = (
            "https://api.tomtom.com/traffic/services/4/flowSegmentData/"
            f"absolute/10/json?key={self.config.tomtom_api_key}&point={point.lat},{point.lon}"
        )

        try:
            response = session.get(url, timeout=API_TIMEOUT_SECONDS)
            response.raise_for_status()
            flow = response.json().get("flowSegmentData", {})

            return {
                "traffic_speed": flow.get("currentSpeed"),
                "traffic_freeflow": flow.get("freeFlowSpeed"),
                "traffic_confidence": flow.get("confidence"),
                "traffic_road_closure": flow.get("roadClosure"),
                "traffic_source": "tomtom",
                "traffic_status": "ok",
            }

        except Exception as exc:
            self.logger.warning("TomTom failed for %s: %s", point.name, exc)
            return default

    def get_openmeteo(self, session: requests.Session, point: CCTVPoint) -> dict:
        default = {
            "weather_temp": None,
            "weather_humidity": None,
            "weather_rain": None,
            "weather_wind_speed": None,
            "weather_source": "open-meteo",
            "weather_status": "error",
        }

        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={point.lat}&longitude={point.lon}"
            "&current=temperature_2m,relative_humidity_2m,rain,wind_speed_10m"
            "&timezone=Asia%2FJakarta"
        )

        try:
            response = session.get(url, timeout=API_TIMEOUT_SECONDS)
            response.raise_for_status()
            current = response.json().get("current", {})

            return {
                "weather_temp": current.get("temperature_2m"),
                "weather_humidity": current.get("relative_humidity_2m"),
                "weather_rain": current.get("rain"),
                "weather_wind_speed": current.get("wind_speed_10m"),
                "weather_source": "open-meteo",
                "weather_status": "ok",
            }

        except Exception as exc:
            self.logger.warning("Open-Meteo failed for %s: %s", point.name, exc)
            return default

    def write_metadata(self, point: CCTVPoint, row: dict) -> None:
        date_folder = row["date"]
        metadata_dir = self.config.output_root / date_folder / point.name / "metadata"
        ensure_dir(metadata_dir)

        csv_path = metadata_dir / f"{point.name}_{date_folder}_metadata.csv"
        file_exists = csv_path.exists()

        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

        self.logger.info(
            "Metadata saved: %s | TomTom=%s | OpenMeteo=%s",
            csv_path,
            row.get("traffic_cache_status"),
            row.get("weather_cache_status"),
        )
