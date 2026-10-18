from __future__ import annotations

import os
import re

os.environ["NR_DISABLE_DEFAULT_APP"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def extract_csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "csrf token not found"
    return match.group(1)


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        env="test",
        db_path=tmp_path / "test.db",
        master_key_file=tmp_path / "master.key",
        host="127.0.0.1",
        port=8080,
    )
    application = create_app(settings)
    yield application
    application.state.db.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def setup_admin(client: TestClient, password: str = "test-password-123") -> None:
    page = client.get("/setup")
    assert page.status_code == 200
    token = extract_csrf(page.text)
    response = client.post(
        "/setup",
        data={
            "csrf": token,
            "admin_password": password,
            "admin_password2": password,
            "tianapi_key": "tian-test-key",
            "deepseek_key": "sk-test-key",
            "wxpusher_app_token": "AT_test_token",
            "wxpusher_uid": "UID_test_user",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.fixture
def do_setup():
    return setup_admin


@pytest.fixture
def csrf_parser():
    return extract_csrf
