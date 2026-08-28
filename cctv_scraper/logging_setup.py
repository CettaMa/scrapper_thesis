import logging
import sys
from pathlib import Path

from cctv_scraper.config import ensure_dir


def setup_logging(output_root: Path) -> None:
    ensure_dir(output_root)
    ensure_dir(output_root / "logs")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(
        output_root / "logs" / "scraper.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def point_logger(output_root: Path, point_name: str) -> logging.Logger:
    logger = logging.getLogger(f"cctv.{point_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = True

    marker = f"{point_name}.log"
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename.endswith(marker):
            return logger

    ensure_dir(output_root / "logs")
    handler = logging.FileHandler(output_root / "logs" / marker, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger
