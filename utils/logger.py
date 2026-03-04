import sys
from loguru import logger


def setup_logger(log_level: str = "INFO"):
    logger.remove()
    logger.add(sys.stdout, level=log_level, colorize=True, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
    logger.add("logs/gpt_trader.log", rotation="20 MB", retention="10 days", level=log_level)
    return logger

log = setup_logger()
