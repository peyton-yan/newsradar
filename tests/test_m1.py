from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import utcnow
from app.models import Setting, WorkerHeartbeat


def test_first_run_redirects_to_setup(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    page = client.get("/setup")
    assert page.status_code == 200
    assert "首次设置" in page.text
    assert "管理员密码" in page.text


def test_setup_requires_a_strong_password(client: TestClient, csrf_parser):
    page = client.get("/setup")
    token = csrf_parser(page.text)
    response = client.post(
        "/setup",
        data={
            "csrf": token,
            "admin_password": "short",
            "admin_password2": "short",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"


def test_setup_login_and_logout(client: TestClient, do_setup, csrf_parser):
    do_setup(client)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "今天先看这几件事" in dashboard.text

    token = csrf_parser(dashboard.text)
    logout = client.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"

    login_page = client.get("/login")
    token = csrf_parser(login_page.text)
    bad = client.post(
        "/login",
        data={"csrf": token, "password": "wrong-password", "next": "/"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert bad.headers["location"] == "/login"

    login_page = client.get("/login")
    token = csrf_parser(login_page.text)
    good = client.post(
        "/login",
        data={"csrf": token, "password": "test-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_setup_encrypts_secrets(app, client: TestClient, do_setup):
    do_setup(client)
    with app.state.db.session() as session:
        api_row = session.get(Setting, "tianapi.api_key")
        push_row = session.get(Setting, "push.provider_config")
        assert api_row is not None and api_row.is_secret
        assert api_row.value_json != "tian-test-key"
        assert "tian-test-key" not in str(api_row.value_json)
        assert push_row is not None and push_row.is_secret
        assert "AT_test_token" not in str(push_row.value_json)

    settings = app.state.settings_service
    assert settings.get("tianapi.api_key") == "tian-test-key"
    config = settings.get("push.provider_config")
    assert config["app_token"] == "AT_test_token"
    assert config["uid"] == "UID_test_user"


def test_csrf_is_required(client: TestClient, do_setup):
    do_setup(client)
    response = client.post(
        "/settings/analysis",
        data={"min_interval_minutes": 120},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_analysis_settings_save_and_preset(client: TestClient, do_setup, csrf_parser):
    do_setup(client)
    page = client.get("/settings/analysis")
    token = csrf_parser(page.text)
    response = client.post(
        "/settings/analysis",
        data={
            "csrf": token,
            "min_interval_minutes": "240",
            "max_runs_per_day": "4",
            "batch_size": "8",
            "max_candidates_per_run": "20",
            "min_candidates": "3",
            "daily_input_token_budget": "60000",
            "daily_output_token_budget": "15000",
            "urgent_threshold": "85",
            "digest_threshold": "60",
            "api_key": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.app.state.settings_service.get("analysis.min_interval_minutes") == 240
    assert client.app.state.settings_service.get("analysis.daily_input_token_budget") == 60000

    page = client.get("/settings/analysis")
    token = csrf_parser(page.text)
    preset = client.post(
        "/settings/analysis/preset",
        data={"csrf": token, "preset": "aggressive"},
        follow_redirects=False,
    )
    assert preset.status_code == 303
    assert client.app.state.settings_service.get("analysis.min_interval_minutes") == 60
    assert client.app.state.settings_service.get("analysis.daily_output_token_budget") == 50000


def test_push_settings_validation_and_save(client: TestClient, do_setup, csrf_parser):
    do_setup(client)
    page = client.get("/settings/push")
    token = csrf_parser(page.text)
    response = client.post(
        "/settings/push",
        data={
            "csrf": token,
            "provider": "wxpusher",
            "app_token": "",
            "uid": "UID_test_user",
            "digest_times": "08:00,12:30,21:30",
            "quiet_start": "23:00",
            "quiet_end": "07:00",
            "urgent_threshold": "85",
            "digest_threshold": "60",
            "quiet_urgent_threshold": "92",
            "max_items_per_digest": "5",
            "max_chars_per_message": "1200",
            "daily_push_limit": "10",
            "include_disclaimer": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    settings = client.app.state.settings_service
    assert settings.get("push.digest_times") == ["08:00", "12:30", "21:30"]
    assert settings.get("push.quiet_hours") == ["23:00", "07:00"]
    assert settings.get("push.include_disclaimer") is True
    config = settings.get("push.provider_config")
    assert config["app_token"] == "AT_test_token"
    assert config["uid"] == "UID_test_user"


def test_healthz_reports_degraded_without_heartbeat(app, client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 503
    with app.state.db.session() as session:
        heartbeat = session.get(WorkerHeartbeat, 1)
        assert heartbeat is not None
        heartbeat.last_beat_at = utcnow()
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db"] is True


def test_api_status_requires_login_after_logout(client: TestClient, do_setup, csrf_parser):
    do_setup(client)
    dashboard = client.get("/")
    token = csrf_parser(dashboard.text)
    client.post("/logout", data={"csrf": token}, follow_redirects=False)
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.json()["ok"] is False
