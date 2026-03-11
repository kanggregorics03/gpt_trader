import json
import os
from typing import Optional
from urllib import error, request
from urllib.parse import urlparse, urlunparse

from loguru import logger


class AlertManager:
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        webhook_format: Optional[str] = None,
    ):
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL", "").strip()
        self.webhook_format = (
            webhook_format
            or os.getenv("ALERT_WEBHOOK_FORMAT", "auto")
        ).strip().lower()

    def _resolve_format(self) -> str:
        configured = self.webhook_format or "auto"
        if configured in {"json", "generic"}:
            return "generic"
        if configured in {"slack", "discord"}:
            return configured
        if configured != "auto":
            return "generic"
        if not self.webhook_url:
            return "generic"

        parsed = urlparse(self.webhook_url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        if "hooks.slack.com" in host and path.startswith("/services/"):
            return "slack"
        if host in {"discord.com", "discordapp.com"} and "/api/webhooks/" in path:
            return "discord"
        return "generic"

    def _build_payload(self, title: str, message: str, level: str) -> dict:
        webhook_format = self._resolve_format()
        normalized_level = str(level or "warning").strip().upper() or "WARNING"
        if webhook_format == "slack":
            return {
                "text": f"*{title}*\n{message}\n_Level: {normalized_level}_",
            }
        if webhook_format == "discord":
            return {
                "content": f"**{title}**\n{message}\n`level: {normalized_level}`",
            }
        return {
            "title": title,
            "message": message,
            "level": level,
        }

    def _resolved_webhook_url(self) -> str:
        if not self.webhook_url:
            return ""
        webhook_format = self._resolve_format()
        parsed = urlparse(self.webhook_url)
        host = (parsed.netloc or "").lower()
        if webhook_format == "discord" and host == "discordapp.com":
            return urlunparse(parsed._replace(netloc="discord.com"))
        return self.webhook_url

    def send(self, title: str, message: str, level: str = "warning"):
        logger.log(level.upper(), f"{title}: {message}")
        if not self.webhook_url:
            return

        webhook_format = self._resolve_format()
        target_url = self._resolved_webhook_url()
        payload = json.dumps(self._build_payload(title, message, level)).encode("utf-8")
        req = request.Request(
            target_url,
            data=payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": self.DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5):
                return
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace").strip()
            logger.warning(
                "Failed to send alert webhook: status={} reason={} host={} format={} body={}",
                exc.code,
                exc.reason,
                (urlparse(target_url).netloc or "").lower(),
                webhook_format,
                response_body[:300] or "<empty>",
            )
        except Exception as exc:
            logger.warning(
                "Failed to send alert webhook: host={} format={} error={}",
                (urlparse(target_url).netloc or "").lower(),
                webhook_format,
                exc,
            )
