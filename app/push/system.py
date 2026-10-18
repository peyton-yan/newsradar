"""System-level alerts (currently WxPusher).

This is deliberately small: M4 will add the full digest/urgent formatter and
all providers.  The cost-cap alert must work before M4.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings_service import SettingsService


logger = logging.getLogger("newsradar.push.system")
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message"


def send_system_alert(settings: SettingsService, title: str, content: str) -> bool:
    provider = str(settings.get("push.provider", "wxpusher") or "wxpusher")
    if provider != "wxpusher":
        logger.warning("push.system.unsupported_provider provider=%s", provider)
        return False
    config = settings.get("push.provider_config", {}) or {}
    if not isinstance(config, dict):
        return False
    app_token = str(config.get("app_token") or "").strip()
    uid = str(config.get("uid") or "").strip()
    if not app_token or not uid:
        logger.warning("push.system.missing_wxpusher_config")
        return False
    payload: dict[str, Any] = {
        "appToken": app_token,
        "content": content[:4000],
        "summary": title[:100],
        "contentType": 1,
        "uids": [uid],
    }
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.post(WXPUSHER_URL, json=payload)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("push.system.wxpusher_failed")
        return False
    if response.status_code >= 400 or body.get("code") != 1000:
        logger.warning(
            "push.system.wxpusher_rejected status=%s code=%s msg=%s",
            response.status_code,
            body.get("code"),
            body.get("msg"),
        )
        return False
    logger.info("push.system.sent")
    return True
