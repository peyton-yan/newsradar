"""HTTP routes and view helpers for the M1 admin UI."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text

from app.config import APP_VERSION
from app.db import Database, utcnow
from app.ingest.rss import RssError, validate_feed_url
from app.models import (
    Analysis,
    AuditLog,
    DailyUsage,
    NewsCluster,
    NewsItem,
    PushMessage,
    Source,
    WorkerHeartbeat,
)
from app.security import Security
from app.settings_service import SettingsService


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()

SHANGHAI = ZoneInfo("Asia/Shanghai")
TIME_FORMAT = "%H:%M"


class LoginLimiter:
    def __init__(self, max_attempts: int = 5, window_minutes: int = 15):
        self.max_attempts = max_attempts
        self.window = timedelta(minutes=window_minutes)
        self._attempts: dict[str, list[datetime]] = {}

    def _prune(self, key: str, now: datetime) -> list[datetime]:
        values = [item for item in self._attempts.get(key, []) if now - item < self.window]
        self._attempts[key] = values
        return values

    def allowed(self, key: str) -> bool:
        now = utcnow()
        return len(self._prune(key, now)) < self.max_attempts

    def fail(self, key: str) -> None:
        now = utcnow()
        values = self._prune(key, now)
        values.append(now)
        self._attempts[key] = values

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)


def _db(request: Request) -> Database:
    return request.app.state.db


def _settings(request: Request) -> SettingsService:
    return request.app.state.settings_service


def _security(request: Request) -> Security:
    return request.app.state.security


def _bootstrap(request: Request):
    return request.app.state.bootstrap


def _ingest(request: Request):
    return request.app.state.ingest


def _push(request: Request):
    return request.app.state.push


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin"))


def setup_complete(request: Request) -> bool:
    return _settings(request).is_setup_complete()


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = Security.new_csrf_token()
        request.session["csrf"] = token
    return str(token)


def verify_csrf(request: Request, provided: str | None) -> None:
    expected = request.session.get("csrf")
    if not expected or not provided:
        raise HTTPException(status_code=403, detail="CSRF token 缺失")
    import hmac

    if not hmac.compare_digest(str(expected), str(provided)):
        raise HTTPException(status_code=403, detail="CSRF token 无效")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    items = list(request.session.get("flashes", []))
    items.append({"message": message, "kind": kind})
    request.session["flashes"] = items[-5:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return list(request.session.pop("flashes", []))


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
):
    ctx: dict[str, Any] = {
        "request": request,
        "csrf_token": csrf_token(request),
        "flashes": pop_flashes(request),
        "logged_in": is_logged_in(request),
        "setup_complete": setup_complete(request),
        "version": APP_VERSION,
    }
    ctx.update(context or {})
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=ctx,
        status_code=status_code,
    )


def require_login(request: Request) -> RedirectResponse | None:
    if not setup_complete(request):
        return RedirectResponse("/setup", status_code=303)
    if not is_logged_in(request):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return None


def _check_setup_test_allowed(request: Request) -> None:
    if setup_complete(request) and not is_logged_in(request):
        raise HTTPException(status_code=403, detail="设置已完成，请先登录")


def _provider_config(settings: SettingsService) -> dict[str, Any]:
    value = settings.get("push.provider_config", {})
    if isinstance(value, dict):
        result = dict(value)
    elif isinstance(value, str) and value:
        try:
            result = json.loads(value)
        except json.JSONDecodeError:
            result = {}
    else:
        result = {}
    result.setdefault("mode", "standard")
    result.setdefault("app_token", "")
    result.setdefault("uid", "")
    result.setdefault("spt", "")
    return result


def _http_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise RuntimeError("请求超时") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"网络错误: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"返回不是 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        message = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
        raise RuntimeError(str(message))
    return payload


def _test_tianapi(api_key: str, base_url: str, path: str) -> str:
    payload = _http_json(
        "GET",
        f"{base_url.rstrip('/')}{path}",
        params={"key": api_key, "num": 1, "form": 1, "page": 1},
    )
    code = payload.get("code")
    if code != 200:
        raise RuntimeError(f"天行返回 {code}: {payload.get('msg', '未知错误')}")
    result = payload.get("result") or {}
    items = result.get("list") or result.get("newslist") or []
    return f"天行连接成功，返回 {len(items)} 条"


def _test_deepseek(api_key: str, base_url: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = _http_json(
        "GET",
        f"{base_url.rstrip('/')}/models",
        headers=headers,
    )
    models = payload.get("data") or []
    return f"LLM 连接成功，可用模型 {len(models)} 个"


def _query_wxpusher_uid(app_token: str) -> str:
    payload = _http_json(
        "GET",
        "https://wxpusher.zjiecode.com/api/fun/wxuser",
        params={"appToken": app_token, "page": 1, "pageSize": 20},
    )
    if payload.get("code") != 1000:
        raise RuntimeError(f"WxPusher 返回 {payload.get('code')}: {payload.get('msg', '')}")
    records = ((payload.get("data") or {}).get("records") or [])
    if not records:
        raise RuntimeError("该 appToken 下没有关注用户，请先扫码关注")
    return str(records[0].get("uid") or "")


def _send_wxpusher(app_token: str, uid: str, summary: str, content: str) -> str:
    if not uid:
        uid = _query_wxpusher_uid(app_token)
    payload = _http_json(
        "POST",
        "https://wxpusher.zjiecode.com/api/send/message",
        json={
            "appToken": app_token,
            "content": content[:4000],
            "summary": summary[:100],
            "contentType": 1,
            "uids": [uid],
        },
    )
    code = payload.get("code")
    if code != 1000:
        raise RuntimeError(f"WxPusher 返回 {code}: {payload.get('msg', '未知错误')}")
    return f"测试推送已提交，UID {uid[-6:]}"


def _parse_times(value: str, field_name: str, *, sort: bool = True) -> list[str]:
    parts = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    result: list[str] = []
    for part in parts:
        try:
            parsed = datetime.strptime(part, TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(f"{field_name} 时间格式应为 HH:MM") from exc
        normalized = parsed.strftime(TIME_FORMAT)
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError(f"{field_name} 至少填一个时间")
    return sorted(result) if sort else result


def _dashboard_context(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    db = _db(request)
    now = datetime.now(SHANGHAI)
    today = now.date().isoformat()
    db_ok = True
    db_error = ""
    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)

    heartbeat = None
    try:
        with db.session() as session:
            heartbeat = session.get(WorkerHeartbeat, 1)
    except Exception:
        heartbeat = None
    beat_at = heartbeat.last_beat_at if heartbeat else None
    beat_age = int((utcnow() - beat_at).total_seconds()) if beat_at else None

    usage = None
    queued = 0
    analyzed = 0
    total_news = 0
    filtered = 0
    duplicates = 0
    latest_news: list[NewsItem] = []
    try:
        with db.session() as session:
            usage = session.get(DailyUsage, today)
            queued = int(
                session.scalar(
                    select(func.count()).select_from(NewsItem).where(NewsItem.status == "queued")
                )
                or 0
            )
            analyzed = int(
                session.scalar(
                    select(func.count()).select_from(NewsItem).where(NewsItem.status == "analyzed")
                )
                or 0
            )
            total_news = int(session.scalar(select(func.count()).select_from(NewsItem)) or 0)
            filtered = int(
                session.scalar(
                    select(func.count()).select_from(NewsItem).where(NewsItem.status == "filtered")
                )
                or 0
            )
            duplicates = int(
                session.scalar(
                    select(func.count()).select_from(NewsItem).where(NewsItem.status == "duplicate")
                )
                or 0
            )
            latest_news = list(
                session.scalars(
                    select(NewsItem).order_by(NewsItem.id.desc()).limit(4)
                ).all()
            )
    except Exception:
        pass

    recent_audit: list[AuditLog] = []
    try:
        with db.session() as session:
            recent_audit = list(
                session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(8)).all()
            )
    except Exception:
        pass

    disk = shutil.disk_usage(db.db_path.parent)
    db_size = db.db_path.stat().st_size if db.db_path.exists() else 0
    push_group = settings.get_group("push")
    provider_config = _provider_config(settings)
    cost_limit = float(settings.get("analysis.daily_cost_limit_cny", 0) or 0)
    cost_usd = float(getattr(usage, "cost_usd", 0) or 0) if usage else 0.0
    cost_cny = float(getattr(usage, "cost_cny", 0) or 0) if usage else 0.0
    return {
        "today": today,
        "now": now,
        "db_ok": db_ok,
        "db_error": db_error,
        "db_path": str(db.db_path),
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "disk_free_mb": int(disk.free / 1024 / 1024),
        "heartbeat": heartbeat,
        "beat_age": beat_age,
        "worker_alive": beat_age is not None and beat_age < 300,
        "usage": usage,
        "queued": queued,
        "analyzed": analyzed,
        "total_news": total_news,
        "filtered": filtered,
        "duplicates": duplicates,
        "latest_news": latest_news,
        "recent_audit": recent_audit,
        "paused": bool(settings.get("app.paused", False)),
        "settings_version": settings.settings_version,
        "tianapi_configured": bool(settings.get("tianapi.api_key", "")),
        "deepseek_configured": bool(settings.get("analysis.api_key", "")),
        "wxpusher_configured": bool(provider_config.get("app_token")),
        "wxpusher_uid": provider_config.get("uid", ""),
        "push_provider": push_group.get("provider", "wxpusher"),
        "min_interval": settings.get("analysis.min_interval_minutes"),
        "max_runs": settings.get("analysis.max_runs_per_day"),
        "input_budget": settings.get("analysis.daily_input_token_budget"),
        "output_budget": settings.get("analysis.daily_output_token_budget"),
        "cost_limit": cost_limit,
        "cost_usd": cost_usd,
        "cost_cny": cost_cny,
        "cost_percent": min(100, round(cost_cny / cost_limit * 100, 1)) if cost_limit > 0 else 0,
        "cost_alert_sent": bool(getattr(usage, "cost_alert_sent", False)) if usage else False,
        "model": settings.get("analysis.model", "deepseek-flash"),
        "push_limit": int(settings.get("push.daily_push_limit", 0) or 0),
    }


@router.get("/healthz")
def healthz(request: Request):
    db = _db(request)
    db_ok = True
    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    beat_age = None
    try:
        with db.session() as session:
            heartbeat = session.get(WorkerHeartbeat, 1)
            if heartbeat and heartbeat.last_beat_at:
                beat_age = int((utcnow() - heartbeat.last_beat_at).total_seconds())
    except Exception:
        pass
    disk_free_mb = int(shutil.disk_usage(db.db_path.parent).free / 1024 / 1024)
    degraded = (not db_ok) or beat_age is None or beat_age > 300 or disk_free_mb < 500
    return JSONResponse(
        {
            "status": "degraded" if degraded else "ok",
            "db": db_ok,
            "worker_heartbeat_age_sec": beat_age,
            "disk_free_mb": disk_free_mb,
            "version": APP_VERSION,
        },
        status_code=503 if degraded else 200,
    )


@router.get("/setup")
def setup_page(request: Request):
    if setup_complete(request):
        if not is_logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/", status_code=303)
    return render(request, "setup.html")


@router.post("/setup")
def setup_submit(
    request: Request,
    csrf: str = Form(""),
    admin_password: str = Form(""),
    admin_password2: str = Form(""),
    tianapi_key: str = Form(""),
    deepseek_key: str = Form(""),
    wxpusher_app_token: str = Form(""),
    wxpusher_uid: str = Form(""),
):
    if setup_complete(request):
        return RedirectResponse("/login", status_code=303)
    verify_csrf(request, csrf)
    if len(admin_password) < 12:
        flash(request, "管理员密码至少 12 位", "error")
        return RedirectResponse("/setup", status_code=303)
    if admin_password != admin_password2:
        flash(request, "两次输入的密码不一致", "error")
        return RedirectResponse("/setup", status_code=303)

    settings = _settings(request)
    settings.set_admin_password(admin_password)
    settings.set("tianapi.api_key", tianapi_key.strip())
    settings.set("analysis.api_key", deepseek_key.strip())
    settings.set(
        "push.provider_config",
        {
            "mode": "standard",
            "app_token": wxpusher_app_token.strip(),
            "uid": wxpusher_uid.strip(),
            "spt": "",
        },
    )
    settings.set("push.provider", "wxpusher")
    settings.mark_setup_complete()
    settings.audit("admin", "setup.complete", "system", {"wxpusher_configured": bool(wxpusher_app_token)})
    request.session["admin"] = True
    request.session["csrf"] = Security.new_csrf_token()
    flash(request, "设置完成，已自动登录")
    return RedirectResponse("/", status_code=303)


@router.post("/setup/test/tianapi")
def setup_test_tianapi(
    request: Request,
    csrf: str = Form(""),
    tianapi_key: str = Form(""),
):
    _check_setup_test_allowed(request)
    verify_csrf(request, csrf)
    settings = _settings(request)
    key = tianapi_key.strip() or str(settings.get("tianapi.api_key", "") or "")
    if not key:
        return JSONResponse({"ok": False, "message": "请先填写天行 API Key"}, status_code=400)
    try:
        message = _test_tianapi(
            key,
            str(settings.get("tianapi.base_url")),
            str(settings.get("tianapi.finance.path")),
        )
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/setup/test/deepseek")
def setup_test_deepseek(
    request: Request,
    csrf: str = Form(""),
    deepseek_key: str = Form(""),
):
    _check_setup_test_allowed(request)
    verify_csrf(request, csrf)
    settings = _settings(request)
    key = deepseek_key.strip() or str(settings.get("analysis.api_key", "") or "")
    if not key:
        return JSONResponse({"ok": False, "message": "请先填写 DeepSeek API Key"}, status_code=400)
    try:
        message = _test_deepseek(key, str(settings.get("analysis.base_url")))
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/setup/test/wxpusher")
def setup_test_wxpusher(
    request: Request,
    csrf: str = Form(""),
    wxpusher_app_token: str = Form(""),
    wxpusher_uid: str = Form(""),
):
    _check_setup_test_allowed(request)
    verify_csrf(request, csrf)
    settings = _settings(request)
    config = _provider_config(settings)
    app_token = wxpusher_app_token.strip() or str(config.get("app_token", "") or "")
    uid = wxpusher_uid.strip() or str(config.get("uid", "") or "")
    if not app_token:
        return JSONResponse({"ok": False, "message": "请先填写 WxPusher appToken"}, status_code=400)
    try:
        message = _send_wxpusher(
            app_token,
            uid,
            "NewsRadar 测试",
            "NewsRadar 测试推送\n如果你收到这条消息，WxPusher 配置可用。",
        )
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not setup_complete(request):
        return RedirectResponse("/setup", status_code=303)
    if is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login_submit(
    request: Request,
    csrf: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    if not setup_complete(request):
        return RedirectResponse("/setup", status_code=303)
    verify_csrf(request, csrf)
    limiter: LoginLimiter = request.app.state.login_limiter
    client = request.client.host if request.client else "unknown"
    if not limiter.allowed(client):
        flash(request, "登录失败次数过多，请 15 分钟后再试", "error")
        return RedirectResponse("/login", status_code=303)
    settings = _settings(request)
    if not _security(request).verify_password(settings.admin_password_hash(), password):
        limiter.fail(client)
        settings.audit("anonymous", "login.failed", client)
        flash(request, "密码错误", "error")
        return RedirectResponse("/login", status_code=303)
    limiter.reset(client)
    request.session["admin"] = True
    request.session["csrf"] = Security.new_csrf_token()
    settings.audit("admin", "login.success", client)
    flash(request, "已登录")
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/")
def dashboard(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    return render(request, "dashboard.html", {"status": _dashboard_context(request)})


@router.post("/pause")
def toggle_pause(request: Request, csrf: str = Form(""), paused: str = Form("")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    value = paused == "1"
    settings.set("app.paused", value)
    settings.audit("admin", "pause.toggle", "system", {"paused": value})
    flash(request, "已暂停全部任务" if value else "已恢复任务")
    return RedirectResponse("/", status_code=303)


@router.get("/settings/analysis")
def analysis_settings(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    settings = _settings(request)
    group = settings.get_group("analysis")
    push_group = settings.get_group("push")
    masked = settings.secret_display("analysis.api_key")
    tianapi_masked = settings.secret_display("tianapi.api_key")
    today = datetime.now(SHANGHAI).date().isoformat()
    with _db(request).session() as session:
        usage = session.get(DailyUsage, today)
    cost_limit = float(group.get("daily_cost_limit_cny") or 0)
    cost_cny = float(usage.cost_cny or 0) if usage else 0.0
    return render(
        request,
        "settings_analysis.html",
        {
            "analysis": group,
            "push": push_group,
            "candidate": settings.get_group("candidate"),
            "prompt_template": request.app.state.analysis.prompt_template_text(),
            "usage": usage,
            "cost_limit": cost_limit,
            "cost_cny": cost_cny,
            "cost_percent": min(100, round(cost_cny / cost_limit * 100, 1)) if cost_limit > 0 else 0,
            "api_key_masked": masked,
            "tianapi_key_masked": tianapi_masked,
            "settings_version": settings.settings_version,
        },
    )


@router.post("/settings/analysis")
def analysis_settings_save(
    request: Request,
    csrf: str = Form(""),
    min_interval_minutes: int = Form(120),
    max_runs_per_day: int = Form(0),
    batch_size: int = Form(12),
    max_candidates_per_run: int = Form(20),
    min_candidates: int = Form(1),
    daily_input_token_budget: int = Form(100000),
    daily_output_token_budget: int = Form(25000),
    daily_cost_limit_cny: float = Form(2.0),
    candidate_min_score: int = Form(0),
    prompt_extra: str = Form(""),
    provider: str = Form("deepseek"),
    base_url: str = Form("https://api.deepseek.com"),
    model: str = Form("deepseek-flash"),
    input_price_cny: float = Form(1.0),
    output_price_cny: float = Form(4.0),
    urgent_threshold: int = Form(85),
    digest_threshold: int = Form(60),
    api_key: str = Form(""),
    tianapi_key: str = Form(""),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    errors: list[str] = []
    if not 5 <= min_interval_minutes <= 1440:
        errors.append("最小间隔需在 5～1440 分钟")
    if not 0 <= max_runs_per_day <= 288:
        errors.append("每日次数需在 0～288；0 表示不限")
    if not 1 <= batch_size <= 20:
        errors.append("批量需在 1～20")
    if not 1 <= max_candidates_per_run <= 50:
        errors.append("候选上限需在 1～50")
    if batch_size > max_candidates_per_run:
        errors.append("批量不能大于候选上限")
    if not 1 <= min_candidates <= max_candidates_per_run:
        errors.append("最少候选数不合法")
    if daily_input_token_budget <= 0 or daily_output_token_budget <= 0:
        errors.append("token 预算必须为正")
    allowed_providers = {
        "deepseek",
        "openai",
        "moonshot",
        "zhipu",
        "qwen",
        "openrouter",
        "siliconflow",
        "ollama",
        "custom",
    }
    if provider not in allowed_providers:
        errors.append("LLM 服务商选择不合法")
    if not base_url.startswith(("http://", "https://")):
        errors.append("Base URL 必须是 http/https")
    if not model.strip():
        errors.append("模型名不能为空")
    if daily_cost_limit_cny < 0:
        errors.append("每日费用上限不能为负；0 表示不限制")
    if not 0 <= candidate_min_score <= 200:
        errors.append("候选分门槛需在 0～200；0 表示不过滤")
    prompt_extra = prompt_extra.strip()
    if len(prompt_extra) > 2000:
        errors.append("提示词补充最多 2000 字")
    if "\x00" in prompt_extra:
        errors.append("提示词补充包含非法字符")
    if input_price_cny < 0 or output_price_cny < 0:
        errors.append("token 单价不能为负")
    if not 0 <= digest_threshold <= 100 or not 0 <= urgent_threshold <= 100:
        errors.append("阈值需在 0～100")
    if urgent_threshold < digest_threshold:
        errors.append("紧急阈值不能低于摘要阈值")
    if errors:
        for message in errors:
            flash(request, message, "error")
        return RedirectResponse("/settings/analysis", status_code=303)

    settings = _settings(request)
    updates: dict[str, Any] = {
        "min_interval_minutes": min_interval_minutes,
        "max_runs_per_day": max_runs_per_day,
        "batch_size": batch_size,
        "max_candidates_per_run": max_candidates_per_run,
        "min_candidates": min_candidates,
        "daily_input_token_budget": daily_input_token_budget,
        "daily_output_token_budget": daily_output_token_budget,
        "daily_cost_limit_cny": round(float(daily_cost_limit_cny), 2),
        "prompt_extra": prompt_extra,
        "provider": provider,
        "base_url": base_url.rstrip("/"),
        "model": model.strip(),
        "input_price_cny": round(float(input_price_cny), 4),
        "output_price_cny": round(float(output_price_cny), 4),
    }
    push_updates = {
        "urgent_threshold": urgent_threshold,
        "digest_threshold": digest_threshold,
    }
    if api_key.strip():
        updates["api_key"] = api_key.strip()
    settings.update_group("analysis", updates)
    settings.update_group("candidate", {"min_score": candidate_min_score})
    settings.update_group("push", push_updates)
    if tianapi_key.strip():
        settings.update_group("tianapi", {"api_key": tianapi_key.strip()})
    settings.audit(
        "admin",
        "settings.analysis.update",
        "analysis",
        {**updates, **push_updates},
    )
    flash(request, "分析配置已保存，worker 会在 60 秒内重载")
    return RedirectResponse("/settings/analysis", status_code=303)


@router.post("/settings/analysis/preset")
def analysis_preset(request: Request, csrf: str = Form(""), preset: str = Form("balanced")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    presets: dict[str, dict[str, int]] = {
        "saver": {
            "min_interval_minutes": 240,
            "max_runs_per_day": 0,
            "batch_size": 8,
            "min_candidates": 1,
            "max_candidates_per_run": 20,
            "daily_input_token_budget": 60000,
            "daily_output_token_budget": 15000,
        },
        "balanced": {
            "min_interval_minutes": 120,
            "max_runs_per_day": 0,
            "batch_size": 12,
            "min_candidates": 1,
            "max_candidates_per_run": 20,
            "daily_input_token_budget": 100000,
            "daily_output_token_budget": 25000,
        },
        "aggressive": {
            "min_interval_minutes": 60,
            "max_runs_per_day": 0,
            "batch_size": 15,
            "min_candidates": 1,
            "max_candidates_per_run": 20,
            "daily_input_token_budget": 200000,
            "daily_output_token_budget": 50000,
        },
        "extreme": {
            "min_interval_minutes": 5,
            "max_runs_per_day": 0,
            "batch_size": 20,
            "min_candidates": 1,
            "max_candidates_per_run": 20,
            "daily_input_token_budget": 300000,
            "daily_output_token_budget": 80000,
        },
    }
    values = presets.get(preset)
    if not values:
        flash(request, "未知预设", "error")
        return RedirectResponse("/settings/analysis", status_code=303)
    settings = _settings(request)
    settings.update_group("analysis", values)
    settings.audit("admin", "settings.analysis.preset", preset, values)
    flash(request, f"已切换预设：{preset}")
    return RedirectResponse("/settings/analysis", status_code=303)


@router.post("/settings/analysis/test/tianapi")
def analysis_test_tianapi(request: Request, csrf: str = Form("")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    key = str(settings.get("tianapi.api_key", "") or "")
    if not key:
        return JSONResponse({"ok": False, "message": "请先保存天行 API Key"}, status_code=400)
    try:
        message = _test_tianapi(
            key,
            str(settings.get("tianapi.base_url")),
            str(settings.get("tianapi.finance.path")),
        )
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/settings/analysis/test")
def analysis_test(request: Request, csrf: str = Form("")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    key = str(settings.get("analysis.api_key", "") or "")
    if not key:
        return JSONResponse({"ok": False, "message": "请先保存 DeepSeek API Key"}, status_code=400)
    try:
        message = _test_deepseek(key, str(settings.get("analysis.base_url")))
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.get("/settings/push")
def push_settings(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    settings = _settings(request)
    group = settings.get_group("push")
    config = _provider_config(settings)
    with _db(request).session() as session:
        push_history = list(
            session.scalars(select(PushMessage).order_by(PushMessage.id.desc()).limit(20)).all()
        )
    return render(
        request,
        "settings_push.html",
        {
            "push": group,
            "provider_config": config,
            "push_history": push_history,
            "app_token_masked": _security(request).mask_secret(str(config.get("app_token", ""))),
            "digest_times_text": ", ".join(group.get("digest_times", [])),
            "quiet_start": (group.get("quiet_hours") or ["23:00", "07:00"])[0],
            "quiet_end": (group.get("quiet_hours") or ["23:00", "07:00"])[1],
        },
    )


@router.post("/settings/push")
def push_settings_save(
    request: Request,
    csrf: str = Form(""),
    provider: str = Form("wxpusher"),
    app_token: str = Form(""),
    uid: str = Form(""),
    pushplus_token: str = Form(""),
    sendkey: str = Form(""),
    webhook: str = Form(""),
    digest_times: str = Form("08:00,12:30,21:30"),
    quiet_start: str = Form("23:00"),
    quiet_end: str = Form("07:00"),
    urgent_threshold: int = Form(85),
    min_confidence: int = Form(50),
    digest_threshold: int = Form(60),
    quiet_urgent_threshold: int = Form(92),
    max_items_per_digest: int = Form(5),
    max_chars_per_message: int = Form(1200),
    daily_push_limit: int = Form(0),
    include_disclaimer: bool = Form(False),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    errors: list[str] = []
    try:
        times = _parse_times(digest_times, "摘要时间")
        quiet = _parse_times(f"{quiet_start},{quiet_end}", "静默时段", sort=False)
        if len(quiet) != 2:
            raise ValueError("静默时段需要开始和结束两个时间")
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/push", status_code=303)
    if len(times) > 3:
        errors.append("摘要时间最多 3 个")
    if not 0 <= min_confidence <= 100 or not 0 <= urgent_threshold <= 100:
        errors.append("置信度/紧急阈值需在 0～100")
    if urgent_threshold < min_confidence:
        errors.append("紧急阈值不能低于最低置信度")
    if daily_push_limit < 0:
        errors.append("每日推送上限不能为负；0 表示不限")
    if max_items_per_digest < 1 or max_items_per_digest > 10:
        errors.append("每条摘要新闻数需在 1～10")
    if max_chars_per_message < 300 or max_chars_per_message > 4000:
        errors.append("单条消息字符需在 300～4000")
    if provider not in {"wxpusher", "pushplus", "serverchan", "wecom_bot"}:
        errors.append("推送渠道选择不合法")
    if errors:
        for message in errors:
            flash(request, message, "error")
        return RedirectResponse("/settings/push", status_code=303)

    config = _provider_config(settings)
    if app_token.strip():
        config["app_token"] = app_token.strip()
    if uid.strip():
        config["uid"] = uid.strip()
    if pushplus_token.strip():
        config["token"] = pushplus_token.strip()
    if sendkey.strip():
        config["sendkey"] = sendkey.strip()
    if webhook.strip():
        config["webhook"] = webhook.strip()
    config["mode"] = "standard"
    updates: dict[str, Any] = {
        "provider": provider,
        "provider_config": config,
        "digest_times": times,
        "quiet_hours": quiet,
        "urgent_threshold": urgent_threshold,
        "min_confidence": min_confidence,
        "digest_threshold": digest_threshold,
        "quiet_urgent_threshold": quiet_urgent_threshold,
        "max_items_per_digest": max_items_per_digest,
        "max_chars_per_message": max_chars_per_message,
        "daily_push_limit": daily_push_limit,
        "include_disclaimer": include_disclaimer,
    }
    settings.update_group("push", updates)
    settings.audit("admin", "settings.push.update", "push", updates)
    flash(request, "推送配置已保存")
    return RedirectResponse("/settings/push", status_code=303)


@router.post("/settings/push/test")
def push_test(request: Request, csrf: str = Form("")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    config = _provider_config(settings)
    provider_name = str(settings.get("push.provider", "wxpusher") or "wxpusher")
    title = "NewsRadar 测试"
    content = "NewsRadar 测试推送\n如果你收到这条消息，当前推送渠道配置可用。"
    try:
        if provider_name == "wxpusher":
            app_token = str(config.get("app_token", "") or "")
            uid = str(config.get("uid", "") or "")
            if not app_token:
                return JSONResponse(
                    {"ok": False, "message": "请先保存 WxPusher appToken"},
                    status_code=400,
                )
            message = _send_wxpusher(app_token, uid, title, content)
        else:
            _name, provider = _push(request)._provider()
            if provider is None:
                return JSONResponse({"ok": False, "message": "该渠道暂未实现"}, status_code=400)
            result = provider.send(title=title, content=content, url=None)
            if not result.ok:
                raise RuntimeError(result.error or "发送失败")
            message = "测试推送已提交"
        settings.audit("admin", "push.test", provider_name, {"ok": True})
        return JSONResponse({"ok": True, "message": message})
    except RuntimeError as exc:
        settings.audit("admin", "push.test", provider_name, {"ok": False, "error": str(exc)})
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.get("/settings/security")
def security_settings(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    bootstrap = _bootstrap(request)
    return render(
        request,
        "settings_security.html",
        {
            "db_path": str(bootstrap.db_path),
            "master_key_file": str(bootstrap.master_key_file),
            "settings_version": _settings(request).settings_version,
        },
    )


@router.post("/settings/security/password")
def change_password(
    request: Request,
    csrf: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    if not _security(request).verify_password(settings.admin_password_hash(), current_password):
        flash(request, "当前密码错误", "error")
        return RedirectResponse("/settings/security", status_code=303)
    if len(new_password) < 12:
        flash(request, "新密码至少 12 位", "error")
        return RedirectResponse("/settings/security", status_code=303)
    if new_password != new_password2:
        flash(request, "两次输入的新密码不一致", "error")
        return RedirectResponse("/settings/security", status_code=303)
    settings.set_admin_password(new_password)
    settings.audit("admin", "security.password.change", "admin")
    flash(request, "管理员密码已修改")
    return RedirectResponse("/settings/security", status_code=303)


@router.get("/news")
def news_list(
    request: Request,
    status: str = "",
    q: str = "",
    page: int = 1,
):
    redirect = require_login(request)
    if redirect:
        return redirect
    page = max(1, min(page, 10000))
    per_page = 30
    query = select(NewsItem)
    count_query = select(func.count()).select_from(NewsItem)
    if status:
        query = query.where(NewsItem.status == status)
        count_query = count_query.where(NewsItem.status == status)
    if q.strip():
        needle = f"%{q.strip()[:100]}%"
        query = query.where(NewsItem.title.like(needle))
        count_query = count_query.where(NewsItem.title.like(needle))
    with _db(request).session() as session:
        total = int(session.scalar(count_query) or 0)
        items = list(
            session.scalars(
                query.order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
        )
        cluster_ids = [item.cluster_id for item in items if item.cluster_id]
        clusters = {}
        if cluster_ids:
            for cluster in session.scalars(
                select(NewsCluster).where(NewsCluster.id.in_(cluster_ids))
            ).all():
                clusters[cluster.id] = cluster
    pages = max(1, (total + per_page - 1) // per_page)
    return render(
        request,
        "news_list.html",
        {
            "items": items,
            "clusters": clusters,
            "total": total,
            "page": page,
            "pages": pages,
            "status": status,
            "q": q,
        },
    )


@router.get("/news/{news_id}")
def news_detail(request: Request, news_id: int):
    redirect = require_login(request)
    if redirect:
        return redirect
    with _db(request).session() as session:
        item = session.get(NewsItem, news_id)
        if item is None:
            flash(request, "新闻不存在", "error")
            return RedirectResponse("/news", status_code=303)
        cluster = session.get(NewsCluster, item.cluster_id) if item.cluster_id else None
        analysis = session.scalar(select(Analysis).where(Analysis.news_id == news_id).limit(1))
    return render(
        request,
        "news_detail.html",
        {"item": item, "cluster": cluster, "analysis": analysis},
    )


@router.get("/analyses")
def analyses_list(request: Request, page: int = 1):
    redirect = require_login(request)
    if redirect:
        return redirect
    page = max(1, min(page, 10000))
    per_page = 30
    with _db(request).session() as session:
        total = int(session.scalar(select(func.count()).select_from(Analysis)) or 0)
        rows = session.execute(
            select(Analysis, NewsItem)
            .join(NewsItem, NewsItem.id == Analysis.news_id)
            .order_by(Analysis.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
        usage = session.get(DailyUsage, datetime.now(SHANGHAI).date().isoformat())
    pages = max(1, (total + per_page - 1) // per_page)
    return render(
        request,
        "analyses.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "usage": usage,
        },
    )


@router.get("/sources")
def sources_page(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    settings = _settings(request)
    with _db(request).session() as session:
        sources = list(session.scalars(select(Source).order_by(Source.id)).all())
    return render(
        request,
        "sources.html",
        {
            "sources": sources,
            "newsapi_key_masked": settings.secret_display("sources.newsapi_key"),
            "gnews_key_masked": settings.secret_display("sources.gnews_key"),
            "finnhub_key_masked": settings.secret_display("sources.finnhub_key"),
        },
    )


@router.post("/sources/{source_id}/toggle")
def source_toggle(
    request: Request,
    source_id: int,
    csrf: str = Form(""),
    enabled: str = Form("0"),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    with _db(request).session() as session:
        source = session.get(Source, source_id)
        if source is None:
            flash(request, "采集源不存在", "error")
            return RedirectResponse("/sources", status_code=303)
        source.enabled = enabled == "1"
        name = source.name
        state = source.enabled
    _settings(request).audit("admin", "source.toggle", name, {"enabled": state})
    flash(request, f"{name} 已{'启用' if state else '停用'}")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/interval")
def source_interval(
    request: Request,
    source_id: int,
    csrf: str = Form(""),
    interval_sec: int = Form(600),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    if not 30 <= interval_sec <= 86400:
        flash(request, "采集间隔需在 30～86400 秒", "error")
        return RedirectResponse("/sources", status_code=303)
    with _db(request).session() as session:
        source = session.get(Source, source_id)
        if source is None:
            flash(request, "采集源不存在", "error")
            return RedirectResponse("/sources", status_code=303)
        source.interval_sec = interval_sec
        name = source.name
    _settings(request).audit("admin", "source.interval", name, {"interval_sec": interval_sec})
    flash(request, f"{name} 间隔已改为 {interval_sec} 秒")
    return RedirectResponse("/sources", status_code=303)


@router.post("/settings/sources/keys")
def source_keys_save(
    request: Request,
    csrf: str = Form(""),
    newsapi_key: str = Form(""),
    gnews_key: str = Form(""),
    finnhub_key: str = Form(""),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    settings = _settings(request)
    updates = {
        key: value.strip()
        for key, value in {
            "newsapi_key": newsapi_key,
            "gnews_key": gnews_key,
            "finnhub_key": finnhub_key,
        }.items()
        if value.strip()
    }
    if updates:
        settings.update_group("sources", updates)
        settings.audit("admin", "sources.keys.update", "sources", updates)
        flash(request, "全球新闻 API Key 已保存")
    else:
        flash(request, "没有填写新 Key")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/rss")
def source_add_rss(
    request: Request,
    csrf: str = Form(""),
    name: str = Form(""),
    url: str = Form(""),
    interval_sec: int = Form(600),
    tier: int = Form(8),
):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    clean_name = name.strip() or "RSS 源"
    clean_url = url.strip()
    if not clean_url:
        flash(request, "RSS 地址不能为空", "error")
        return RedirectResponse("/sources", status_code=303)
    try:
        validate_feed_url(clean_url, allow_private=False)
    except RssError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/sources", status_code=303)
    if not 30 <= interval_sec <= 86400:
        flash(request, "采集间隔需在 30～86400 秒", "error")
        return RedirectResponse("/sources", status_code=303)
    with _db(request).session() as session:
        session.add(
            Source(
                kind="rss",
                name=clean_name[:160],
                config_json={"url": clean_url},
                enabled=True,
                tier=max(0, min(20, tier)),
                interval_sec=interval_sec,
            )
        )
    _settings(request).audit("admin", "source.add_rss", clean_name, {"url": clean_url})
    flash(request, f"已添加 RSS 源：{clean_name}")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/test")
def source_test(request: Request, source_id: int, csrf: str = Form("")):
    redirect = require_login(request)
    if redirect:
        return redirect
    verify_csrf(request, csrf)
    result = _ingest(request).collect_source(source_id)
    if result.get("error"):
        flash(request, f"抓取失败：{result['error']}", "error")
    else:
        flash(
            request,
            "抓取完成：获取 {fetched} 条，新增 {new}，重复 {duplicate}，入队 {queued}，过滤 {filtered}".format(
                fetched=result.get("fetched", 0),
                new=result.get("new", 0),
                duplicate=result.get("duplicate", 0),
                queued=result.get("queued", 0),
                filtered=result.get("filtered", 0),
            ),
        )
    _settings(request).audit("admin", "source.test", str(source_id), {"result": result})
    return RedirectResponse("/sources", status_code=303)


@router.get("/logs")
def logs_placeholder(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    settings = _settings(request)
    with _db(request).session() as session:
        logs = list(session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all())
    return render(
        request,
        "placeholder.html",
        {
            "title": "运行日志",
            "phase": "M1/M5",
            "description": "当前显示审计日志；结构化运行日志与脱敏导出在 M5 完善。",
            "audit_logs": logs,
            "settings_version": settings.settings_version,
        },
    )


@router.get("/api/status")
def api_status(request: Request):
    redirect = require_login(request)
    if redirect:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    status = _dashboard_context(request)
    return JSONResponse(
        {
            "ok": True,
            "db_ok": status["db_ok"],
            "worker_alive": status["worker_alive"],
            "beat_age": status["beat_age"],
            "disk_free_mb": status["disk_free_mb"],
            "queued": status["queued"],
            "analyzed": status["analyzed"],
            "today": status["today"],
            "usage": {
                "input_tokens": status["usage"].input_tokens if status["usage"] else 0,
                "output_tokens": status["usage"].output_tokens if status["usage"] else 0,
                "analysis_runs": status["usage"].analysis_runs if status["usage"] else 0,
                "pushes": status["usage"].pushes if status["usage"] else 0,
                "cost_cny": status["cost_cny"],
                "cost_limit_cny": status["cost_limit"],
                "cost_percent": status["cost_percent"],
            },
            "paused": status["paused"],
            "settings_version": status["settings_version"],
        }
    )
