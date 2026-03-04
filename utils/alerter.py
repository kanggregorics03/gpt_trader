import json
import os
from typing import Optional
from urllib import request

from loguru import logger


class AlertManager:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL", "").strip()

    def send(self, title: str, message: str, level: str = "warning"):
        logger.log(level.upper(), f"{title}: {message}")
        if not self.webhook_url:
            return

        payload = json.dumps(
            {
                "title": title,
                "message": message,
                "level": level,
            }
        ).encode("utf-8")
        req = request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5):
                return
        except Exception as exc:
            logger.warning(f"Failed to send alert webhook: {exc}")
