"""Logging setup: every run writes to a fresh timestamped file in logs/
and mirrors to the console. All bot actions (price reads, order attempts,
order results, rejections) go through loggers obtained from here.
"""

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"

_configured = False


def setup_logging() -> Path:
    """Configure root logging once per process. Returns the log file path."""
    global _configured
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"bot_{stamp}.log"
    if _configured:
        return log_file

    fmt = logging.Formatter(
        "%(asctime)sZ | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt.converter = time.gmtime

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _configured = True
    logging.getLogger("trade_logger").info("Logging to %s", log_file)
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
