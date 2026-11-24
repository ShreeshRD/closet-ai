"""Phase 2: undress log, dirt ratings, due list, wash, backup."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSET_AI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLOSET_AI_ROOT", str(tmp_path))

    from closet_ai import auth
    from closet_ai.routes import capture

    auth.reset_serializer()
    capture._pending.clear()

    from closet_ai.main import app

    with TestClient(app) as c:
        yield c


def _login(client: TestClient, pin: str = "1234") -> None:
    r = client.post("/login", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303


def _mk(client, name="Tee", type_="tshirt", color="blue", quantity=1):
    r = client.post(
        "/api/items",
        json={
            "name": name,
            "type": type_,
            "color": color,
            "quantity": quantity,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_log_page_requires_auth(client):
    r = client.get("/log", follow_redirects=False)
    assert r.status_code in (303, 307)
    r2 = client.get("/due", follow_redirects=False)
    assert r2.status_code in (303, 307)


def test_log_page_ok_when_logged_in(client):
    _login(client)
    _mk(client)
    r = client.get("/log")
    assert r.status_code == 200
    assert "Log undress" in r.text
    # Tile grid + rating popup (not inline list-only)
    assert 'id="log-tiles"' in r.text
    assert 'id="rate-overlay"' in r.text
    assert "Save undress" in r.text
    assert 'class="tile log-tile"' in r.text


def test_due_page_ok(client):
    _login(client)
    r = client.get("/due")
    assert r.status_code == 200
    assert "Wash list" in r.text


def test_undress_increments_wear_and_rating(client):
    _login(client)
    item_id = _mk(client)
    r = client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    res = body["results"][0]
    assert res["wears_since_wash"] == 1
    assert res["state"] == "available"

    item = client.get(f"/api/items/{item_id}").json()
    assert item["wears_since_wash"] == 1

    # Second undress
    r2 = client.post("/api/log/undress", json={"ratings": {str(item_id): 4}})
    assert r2.status_code == 200
    assert r2.json()["results"][0]["wears_since_wash"] == 2


def test_single_mid_dirt_does_not_trip_due(client):
    """One shirt at dirt 3 must stay available (avg needs several samples)."""
    _login(client)
    item_id = _mk(client, name="Shirt", type_="shirt")
    r = client.post("/api/log/undress", json={"ratings": {str(item_id): 3}})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["state"] == "available"
    assert res["due_reason"] is None
    assert client.get(f"/api/items/{item_id}").json()["state"] == "available"


def test_high_dirt_trips_due_immediately(client):
    _login(client)
    item_id = _mk(client, name="Dirty pants", type_="pants")
    r = client.post("/api/log/undress", json={"ratings": {str(item_id): 5}})
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["state"] == "due"
    assert res["due_reason"]

    due = client.get("/due")
    assert due.status_code == 200
    assert "Dirty pants" in due.text


def test_qty_pool_single_wear_not_times_quantity(client):
    """Pool of 2: one undress → wears += 1 (not 2)."""
    _login(client)
    item_id = _mk(client, name="Socks pair", type_="socks", color="white", quantity=2)
    assert client.get(f"/api/items/{item_id}").json()["quantity"] == 2

    r2 = client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})
    assert r2.status_code == 200, r2.text
    body = r2.json()["results"][0]
    assert body["quantity"] == 2
    assert body["wears_added"] == 1
    assert body["wears_since_wash"] == 1
    # Not forced to laundry by dirt 2
    assert body["state"] == "available"


def test_undress_rejects_bad_dirt_and_due_item(client):
    _login(client)
    item_id = _mk(client)
    bad = client.post("/api/log/undress", json={"ratings": {str(item_id): 9}})
    assert bad.status_code == 400

    # Trip due with dirt 5
    client.post("/api/log/undress", json={"ratings": {str(item_id): 5}})
    again = client.post("/api/log/undress", json={"ratings": {str(item_id): 1}})
    assert again.status_code == 400


def test_undo_last_rating(client):
    _login(client)
    item_id = _mk(client)
    client.post("/api/log/undress", json={"ratings": {str(item_id): 3}})
    assert client.get(f"/api/items/{item_id}").json()["wears_since_wash"] == 1

    r = client.post("/api/log/undo", data={"item_id": str(item_id)})
    assert r.status_code == 200
    assert client.get(f"/api/items/{item_id}").json()["wears_since_wash"] == 0

    # Nothing left
    r2 = client.post("/api/log/undo", data={"item_id": str(item_id)})
    assert r2.status_code == 404


def test_mark_washed_resets_and_keeps_history(client):
    _login(client)
    item_id = _mk(client, name="Jeans", type_="pants")
    client.post("/api/log/undress", json={"ratings": {str(item_id): 5}})
    assert client.get(f"/api/items/{item_id}").json()["state"] == "due"

    r = client.post(f"/api/wash/{item_id}")
    assert r.status_code == 200
    item = client.get(f"/api/items/{item_id}").json()
    assert item["state"] == "available"
    assert item["wears_since_wash"] == 0

    # History retained on detail page
    detail = client.get(f"/items/{item_id}")
    assert detail.status_code == 200
    assert "Wash history" in detail.text
    assert "wash" in detail.text.lower()


def test_batch_mark_washed_from_due_form(client):
    _login(client)
    a = _mk(client, name="A", type_="socks")
    b = _mk(client, name="B", type_="socks")
    client.post("/api/log/undress", json={"ratings": {str(a): 5, str(b): 5}})

    r = client.post(
        "/due/wash",
        data={"item_ids": [str(a), str(b)]},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert client.get(f"/api/items/{a}").json()["state"] == "available"
    assert client.get(f"/api/items/{b}").json()["state"] == "available"


def test_wear_count_trips_due_at_threshold(client):
    _login(client)
    # tshirt max wears = 5
    item_id = _mk(client, name="Tee", type_="tshirt")
    for i in range(5):
        r = client.post(
            "/api/log/undress", json={"ratings": {str(item_id): 2}}
        )
        assert r.status_code == 200, r.text
    item = client.get(f"/api/items/{item_id}").json()
    assert item["wears_since_wash"] == 5
    assert item["state"] == "due"


def test_backlog_pending_rating(client):
    _login(client)
    item_id = _mk(client)
    # Insert wear without rating via raw path: use service then delete rating
    from closet_ai.db import get_db
    from closet_ai.services import logging as wearlog

    with get_db() as conn:
        conn.execute(
            "INSERT INTO wear_events (item_id, event_at) VALUES (?, ?)",
            (item_id, "2020-01-01T00:00:00+00:00"),
        )
        backlog = wearlog.pending_backlog(conn)
    assert any(b["id"] == item_id for b in backlog)

    page = client.get("/log")
    assert page.status_code == 200
    assert "Rate what you took off" in page.text


def test_backup_db_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSET_AI_DATA", str(tmp_path / "data"))
    from closet_ai.config import ensure_dirs
    from closet_ai.db import init_db
    from closet_ai.services.logging import backup_db

    ensure_dirs()
    init_db()
    dest = backup_db()
    assert dest is not None and dest.is_file()
    # Second call same day is idempotent
    dest2 = backup_db()
    assert dest2 == dest


def test_api_log_requires_auth(client):
    r = client.post("/api/log/undress", json={"ratings": {"1": 3}}, follow_redirects=False)
    assert r.status_code == 401
    r2 = client.post("/api/wash", data={"item_ids": "1"}, follow_redirects=False)
    assert r2.status_code == 401


def test_recently_worn_shown_dimmed_in_main_grid(client):
    """After rating, item stays in the main grid with tile-recent (dimmed)."""
    _login(client)
    item_id = _mk(client, name="Grey tee", type_="tshirt", color="grey")
    r = client.post("/api/log/undress", json={"ratings": {str(item_id): 3}})
    assert r.status_code == 200

    page = client.get("/log")
    assert page.status_code == 200
    assert 'id="log-tiles"' in page.text
    assert "Grey tee" in page.text
    assert "tile-recent" in page.text
    assert "recent" in page.text  # badge on the tile
    # Still one grid, not a separate recent-only section
    assert 'id="recent-tiles"' not in page.text

    due = client.get("/due")
    assert "Grey tee" not in due.text
