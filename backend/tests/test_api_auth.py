"""Authentication: fixtures for API tests, plus the session / CSRF / route-protection rules."""

from __future__ import annotations

import pytest

PASSWORD = "correct horse battery staple 42"


@pytest.fixture
def app_client(md):
    from fastapi.testclient import TestClient

    from app.api.clients import get_client
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_client] = lambda: md.client
    with TestClient(app) as client:
        yield client


@pytest.fixture
def admin(db_env):
    from datetime import UTC, datetime

    from app.api.security import hash_password
    from app.db import session_scope
    from app.models import User

    with session_scope() as s:
        s.add(User(username="admin", password_hash=hash_password(PASSWORD), is_admin=True,
                   password_changed_at=datetime.now(UTC)))
    return "admin"


@pytest.fixture
def authed(app_client, admin):
    r = app_client.post("/api/auth/login", json={"username": admin, "password": PASSWORD})
    assert r.status_code == 200, r.text
    app_client.headers["x-csrf-token"] = r.json()["csrf_token"]
    return app_client


def test_login_rejects_wrong_password(app_client, admin):
    r = app_client.post("/api/auth/login", json={"username": admin, "password": "wrong password!!"})
    assert r.status_code == 401
    assert "Invalid username or password" in r.text


def test_every_api_route_requires_a_session(app_client):
    import re

    open_routes = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}
    checked = 0
    for path, ops in app_client.app.openapi()["paths"].items():
        if not path.startswith("/api/") or path in open_routes:
            continue
        concrete = re.sub(r"\{([^}]+)\}",
                          lambda m: "00000000-0000-4000-8000-000000000000" if "id" in m.group(1) else "1", path)
        for method in ops:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            r = app_client.request(method.upper(), concrete)
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked > 30


def test_state_changing_request_needs_csrf(authed):
    token = authed.headers.pop("x-csrf-token")
    assert authed.post("/api/library/scan").status_code == 403
    authed.headers["x-csrf-token"] = token
    assert authed.get("/api/dashboard").status_code == 200


def test_cross_origin_request_rejected(authed):
    r = authed.post("/api/library/scan", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
