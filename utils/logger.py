import os
import sys
from pathlib import Path

from loguru import logger


SUPPORTED_LOG_FORMATS = {"text", "json"}
TEXT_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<level>{message}</level>"
)


def _normalize_log_format(log_format: str) -> str:
    normalized = str(log_format or "text").strip().lower()
    if normalized not in SUPPORTED_LOG_FORMATS:
        return "text"
    return normalized


def _resolve_file_sink(file_sink: str | None) -> str:
    resolved = str(file_sink or os.getenv("LOG_FILE", "logs/gpt_trader.log")).strip()
    if not resolved:
        resolved = "logs/gpt_trader.log"
    path = Path(resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def setup_logger(
    log_level: str | None = None,
    log_format: str | None = None,
    stdout_sink=None,
    file_sink: str | None = None,
):
    log_level = log_level or os.getenv("LOG_LEVEL", "INFO")
    log_format = _normalize_log_format(log_format or os.getenv("LOG_FORMAT", "text"))
    stdout_sink = stdout_sink or sys.stdout
    file_sink = _resolve_file_sink(file_sink)
    logger.remove()
    if log_format == "json":
        logger.add(stdout_sink, level=log_level, colorize=False, serialize=True)
        logger.add(
            file_sink,
            rotation="20 MB",
            retention="10 days",
            level=log_level,
            colorize=False,
            serialize=True,
        )
    else:
        logger.add(stdout_sink, level=log_level, colorize=True, format=TEXT_LOG_FORMAT)
        logger.add(
            file_sink,
            rotation="20 MB",
            retention="10 days",
            level=log_level,
            colorize=False,
            format=TEXT_LOG_FORMAT,
        )
    return logger


log = setup_logger()
