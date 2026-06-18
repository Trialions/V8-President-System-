# logger.py — Loglama yardimcilari
import logging
import os
from pathlib import Path

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "trbot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
_logger = logging.getLogger("trbot")


def log_info(msg: str):
    _logger.info(msg)

def log_error(msg: str):
    _logger.error(msg)

def log_event(etype: str, **kw):
    parts = " ".join(f"{k}={v}" for k, v in kw.items())
    _logger.info(f"EVENT {etype} {parts}")
