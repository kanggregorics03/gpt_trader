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


def _fallback_file_sink(original_sink: str) -> str:
    original_path = Path(str(original_sink))
    fallback_root = Path(os.getenv("LOG_FALLBACK_DIR", "/tmp"))
    fallback_root.mkdir(parents=True, exist_ok=True)
    fallback_name = original_path.name or "gpt_trader.log"
    return str(fallback_root / fallback_name)


def _add_file_sink(target_sink: str, *, log_level: str, log_format: str) -> str:
    try:
        if log_format == "json":
            logger.add(
                target_sink,
                rotation="20 MB",
                retention="10 days",
                level=log_level,
                colorize=False,
                serialize=True,
                encoding="utf-8",
            )
        else:
            logger.add(
                target_sink,
                rotation="20 MB",
                retention="10 days",
                level=log_level,
                colorize=False,
                format=TEXT_LOG_FORMAT,
                encoding="utf-8",
            )
        return target_sink
    except OSError:
        fallback_sink = _fallback_file_sink(target_sink)
        if fallback_sink == target_sink:
            raise
        if log_format == "json":
            logger.add(
                fallback_sink,
                rotation="20 MB",
                retention="10 days",
                level=log_level,
                colorize=False,
                serialize=True,
                encoding="utf-8",
            )
        else:
            logger.add(
                fallback_sink,
                rotation="20 MB",
                retention="10 days",
                level=log_level,
                colorize=False,
                format=TEXT_LOG_FORMAT,
                encoding="utf-8",
            )
        logger.warning("File log sink {} is not writable; falling back to {}.", target_sink, fallback_sink)
        return fallback_sink


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
    else:
        logger.add(stdout_sink, level=log_level, colorize=True, format=TEXT_LOG_FORMAT)
    _add_file_sink(
        file_sink,
        log_level=log_level,
        log_format=log_format,
    )
    return logger


log = setup_logger()
