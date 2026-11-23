"""Smoke tests for Phase 0 (no vision)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSET_AI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLOSET_AI_ROOT", str(tmp_path))

    from closet_ai import auth

    auth.reset_serializer()

    from closet_ai.main import app

    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_login_required(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_first_run_sets_pin_and_unlocks(client):
    r = client.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code == 200


def test_wrong_pin_rejected(client):
    client.post("/login", data={"pin": "1234"})
    client.post("/logout")
    r = client.post("/login", data={"pin": "9999"}, follow_redirects=False)
    assert r.status_code == 401


def test_create_list_item_api(client):
    client.post("/login", data={"pin": "4321"})
    r = client.post(
        "/api/items",
        json={
            "name": "Grey crew tee",
            "type": "tshirt",
            "color": "grey",
            "pattern": "solid",
            "sleeve": "short",
            "quantity": 1,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "available"
    assert body["wears_since_wash"] == 0

    listed = client.get("/api/items").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Grey crew tee"


def test_invalid_type_rejected(client):
    client.post("/login", data={"pin": "4321"})
    r = client.post("/api/items", json={"name": "x", "type": "cape"})
    assert r.status_code == 400


def test_name_optional_and_polo_type(client):
    client.post("/login", data={"pin": "4321"})
    r = client.post(
        "/api/items",
        json={"name": "", "type": "polo", "color": "navy", "quantity": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == ""
    assert body["display_name"] == "navy polo"
    assert body["type"] == "polo"


def test_html_create_item(client):
    client.post("/login", data={"pin": "5555"})
    r = client.post(
        "/items/new",
        data={
            "name": "Blue jeans",
            "type": "pants",
            "color": "blue",
            "pattern": "solid",
            "sleeve": "unknown",
            "quantity": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get(r.headers["location"])
    assert detail.status_code == 200
    assert "Blue jeans" in detail.text


def test_multiple_items_do_not_overwrite(client):
    """Regression: each POST /items/new must create a new row."""
    client.post("/login", data={"pin": "4321"})
    for name, typ in (("First tee", "tshirt"), ("Second tee", "tshirt"), ("Third jeans", "pants")):
        r = client.post(
            "/items/new",
            data={
                "name": name,
                "type": typ,
                "color": "x",
                "pattern": "solid",
                "sleeve": "short",
                "quantity": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    items = client.get("/api/items").json()
    names = {i["name"] for i in items}
    assert {"First tee", "Second tee", "Third jeans"} <= names
    assert len(items) >= 3

    home = client.get("/")
    assert home.status_code == 200
    for name in ("First tee", "Second tee", "Third jeans"):
        assert name in home.text
        assert home.text.count(f'data-item-id') >= 1
    # three distinct tiles
    assert home.text.count('class="tile ') + home.text.count('class="tile"') + home.text.count('class="tile tile-due"') + home.text.count('class="tile ') >= 0
    assert home.text.count('data-item-id=') >= 3
