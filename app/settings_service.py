"""Business settings stored in SQLite, with Fernet-encrypted secrets."""

from __future__ import annotations

import copy
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import Database, utcnow
from app.models import AuditLog, Setting
from app.security import Security


DEFAULTS: dict[str, Any] = {
    "app": {
        "timezone": "Asia/Shanghai",
        "paused": False,
        "retention": {
            "news_days": 90,
            "analysis_days": 180,
            "log_days": 30,
        },
    },
    "security": {
        "admin_password_hash": "",
        "setup_complete": False,
        "session_ttl_hours": 12,
        "login_max_attempts": 5,
    },
    "tianapi": {
        "enabled": True,
        "base_url": "https://apis.tianapi.com",
        "api_key": "",
        "daily_call_limit": 2000,
        "qps": 5,
        "timeout_sec": 15,
        "finance": {
            "enabled": True,
            "path": "/caijing/index",
            "interval_sec": 300,
            "num": 50,
        },
        "general": {
            "enabled": True,
            "path": "/generalnews/index",
            "interval_sec": 600,
            "num": 50,
        },
    },
    "rss": {
        "enabled": True,
        "timeout_sec": 15,
        "max_items_per_feed": 50,
        "user_agent": "NewsRadar/1.0",
    },
    "analysis": {
        "enabled": True,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "input_price_cny": 1.0,
        "output_price_cny": 4.0,
        "api_key": "",
        "temperature": 0.2,
        "max_output_tokens": 4096,
        "timeout_sec": 120,
        "min_interval_minutes": 120,
        "max_runs_per_day": 8,
        "batch_size": 12,
        "max_candidates_per_run": 20,
        "min_candidates": 1,
        "max_wait_hours": 6,
        "daily_input_token_budget": 100000,
        "daily_output_token_budget": 25000,
        "daily_cost_limit_cny": 2.0,
        "per_run_input_token_limit": 15000,
        "max_chars_per_item": 500,
        "max_total_input_chars": 18000,
        "soft_stop_ratio": 0.9,
        "prompt_version": "v1",
        "flash": {
            "enabled": False,
            "min_interval_minutes": 30,
            "max_runs_per_day": 3,
        },
    },
    "push": {
        "enabled": True,
        "provider": "wxpusher",
        "provider_config": {
            "mode": "standard",
            "app_token": "",
            "uid": "",
            "spt": "",
        },
        "urgent_threshold": 85,
        "min_confidence": 50,
        "digest_threshold": 60,
        "digest_times": ["08:00", "12:30", "21:30"],
        "quiet_hours": ["23:00", "07:00"],
        "quiet_urgent_threshold": 92,
        "max_items_per_digest": 5,
        "max_chars_per_message": 1200,
        "daily_push_limit": 10,
        "retry_max": 3,
        "dedupe_cooldown_hours": 24,
        "include_disclaimer": True,
    },
    "candidate": {
        "min_score": 0,
        "max_age_hours": 48,
        "dedupe_window_hours": 72,
        "simhash_hamming": 3,
        "positive_keywords": [],
        "negative_keywords": [],
        "source_tiers": {},
    },
    "sources": {
        "newsapi_key": "",
        "gnews_key": "",
        "finnhub_key": "",
    },
    "settings_version": 0,
}

SECRET_KEYS = {
    "security.admin_password_hash",
    "tianapi.api_key",
    "analysis.api_key",
    "push.provider_config",
    "sources.newsapi_key",
    "sources.gnews_key",
    "sources.finnhub_key",
}
SECRET_JSON_KEYS = {"push.provider_config"}


def decode_secret(security: Security, key: str, raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return copy.deepcopy(FLAT_DEFAULTS.get(key, default))
    text = security.decrypt(str(raw))
    if key in SECRET_JSON_KEYS:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return copy.deepcopy(FLAT_DEFAULTS.get(key, default))
    return text


def encode_secret(security: Security, key: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    if key in SECRET_JSON_KEYS:
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return security.encrypt(text)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dotted keys. Lists stay as leaf values."""

    if prefix in SECRET_JSON_KEYS:
        return {prefix: value}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return result
    return {prefix: value}


FLAT_DEFAULTS = flatten(DEFAULTS)


class SettingsService:
    def __init__(self, db: Database, security: Security):
        self.db = db
        self.security = security

    def ensure_defaults(self) -> None:
        with self.db.session() as session:
            # Older builds briefly stored provider_config as nested keys.  The
            # secret JSON leaf replaces those rows; remove stale children so
            # they cannot overwrite the decrypted config on read.
            session.execute(delete(Setting).where(Setting.key.like("push.provider_config.%")))
            existing = {row.key for row in session.scalars(select(Setting)).all()}
            rows: list[dict[str, Any]] = []
            for key, value in FLAT_DEFAULTS.items():
                if key in existing:
                    continue
                secret = key in SECRET_KEYS
                rows.append(
                    {
                        "key": key,
                        "value_json": None if secret else copy.deepcopy(value),
                        "is_secret": secret,
                        "updated_at": utcnow(),
                    }
                )
            if rows:
                # Web and worker may start at the same time; INSERT OR IGNORE
                # keeps first-run bootstrapping idempotent without a lock.
                session.execute(
                    sqlite_insert(Setting)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["key"])
                )

    def _get_row(self, key: str, session=None) -> Setting | None:
        if session is not None:
            return session.get(Setting, key)
        with self.db.session() as own_session:
            return own_session.get(Setting, key)

    def get(self, key: str, default: Any = None) -> Any:
        with self.db.session() as session:
            row = session.get(Setting, key)
            if row is None:
                return copy.deepcopy(FLAT_DEFAULTS.get(key, default))
            if row.is_secret:
                return decode_secret(self.security, key, row.value_json, default)
            return copy.deepcopy(row.value_json)

    def set(self, key: str, value: Any, *, bump: bool = True) -> None:
        secret = key in SECRET_KEYS
        encoded: Any
        if secret:
            encoded = encode_secret(self.security, key, value)
        else:
            encoded = value
        with self.db.session() as session:
            row = session.get(Setting, key)
            if row is None:
                row = Setting(key=key, is_secret=secret)
                session.add(row)
            row.value_json = encoded
            row.is_secret = secret
            row.updated_at = utcnow()
            if bump:
                version_row = session.get(Setting, "settings_version")
                current = int(version_row.value_json or 0) if version_row else 0
                if version_row is None:
                    version_row = Setting(key="settings_version", value_json=current + 1)
                    session.add(version_row)
                else:
                    version_row.value_json = current + 1
                    version_row.updated_at = utcnow()

    def get_group(self, group: str) -> dict[str, Any]:
        result = copy.deepcopy(DEFAULTS.get(group, {}))
        prefix = f"{group}."
        with self.db.session() as session:
            rows = session.scalars(select(Setting).where(Setting.key.startswith(prefix))).all()
            for row in rows:
                relative = row.key[len(prefix) :]
                value: Any
                if row.is_secret:
                    value = decode_secret(self.security, row.key, row.value_json, None)
                else:
                    value = row.value_json
                self._set_path(result, relative.split("."), value)
        return result

    @staticmethod
    def _set_path(target: dict[str, Any], parts: list[str], value: Any) -> None:
        cursor = target
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value

    def update_group(self, group: str, values: dict[str, Any]) -> None:
        flat: dict[str, Any] = {}
        for key, value in values.items():
            full_key = f"{group}.{key}"
            if full_key in SECRET_JSON_KEYS:
                flat[full_key] = value
            else:
                flat.update(flatten({key: value}, group))
        allowed = {key for key in FLAT_DEFAULTS if key.startswith(f"{group}.")}
        unknown = set(flat) - allowed
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")
        with self.db.session() as session:
            for key, value in flat.items():
                secret = key in SECRET_KEYS
                encoded: Any
                if secret:
                    encoded = encode_secret(self.security, key, value)
                else:
                    encoded = value
                row = session.get(Setting, key)
                if row is None:
                    row = Setting(key=key, is_secret=secret)
                    session.add(row)
                row.value_json = encoded
                row.is_secret = secret
                row.updated_at = utcnow()
            version_row = session.get(Setting, "settings_version")
            current = int(version_row.value_json or 0) if version_row else 0
            if version_row is None:
                session.add(Setting(key="settings_version", value_json=current + 1))
            else:
                version_row.value_json = current + 1
                version_row.updated_at = utcnow()

    def audit(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        safe_detail = dict(detail or {})
        sensitive_names = ("key", "token", "password", "secret", "provider_config", "uid")
        for key in list(safe_detail):
            if any(token in key.lower() for token in sensitive_names):
                safe_detail[key] = "***"
        with self.db.session() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    target=target,
                    detail_json=safe_detail,
                    created_at=utcnow(),
                )
            )

    @property
    def settings_version(self) -> int:
        return int(self.get("settings_version", 0) or 0)

    def is_setup_complete(self) -> bool:
        return bool(self.get("security.setup_complete", False))

    def mark_setup_complete(self) -> None:
        self.set("security.setup_complete", True)

    def admin_password_hash(self) -> str:
        return str(self.get("security.admin_password_hash", "") or "")

    def set_admin_password(self, password: str) -> None:
        self.set("security.admin_password_hash", self.security.hash_password(password))

    def secret_display(self, key: str) -> str:
        value = str(self.get(key, "") or "")
        return self.security.mask_secret(value)
