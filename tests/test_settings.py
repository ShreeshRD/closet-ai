"""Phase 3: settings thresholds, climate_hint, stats/empty states."""
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
        json={"name": name, "type": type_, "color": color, "quantity": quantity},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_settings_requires_auth(client):
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code in (303, 307)


def test_settings_page_ok(client):
    _login(client)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Wash thresholds" in r.text
    assert "max_wears_tshirt" in r.text
    assert "Climate hint" in r.text
    assert "Reset to defaults" in r.text


def test_threshold_override_trips_due_earlier(client):
    """User lowers tshirt max wears to 2 → due after 2 undresses (default is 5)."""
    _login(client)
    item_id = _mk(client, name="Tee", type_="tshirt")

    r = client.post(
        "/settings/thresholds",
        data={"max_wears_tshirt": "2", "days_cap_tshirt": "21"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # refresh state under new threshold (2 wears < default 5, should be due at 2)
    for _ in range(2):
        rr = client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})
        assert rr.status_code == 200, rr.text

    item = client.get(f"/api/items/{item_id}").json()
    assert item["wears_since_wash"] == 2
    assert item["state"] == "due"


def test_threshold_reset_restores_default(client):
    _login(client)
    item_id = _mk(client, name="Tee", type_="tshirt")
    client.post(
        "/settings/thresholds",
        data={"max_wears_tshirt": "2", "days_cap_tshirt": "21"},
        follow_redirects=False,
    )
    for _ in range(2):
        client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})
    assert client.get(f"/api/items/{item_id}").json()["state"] == "due"

    # wash, then reset thresholds — default max is 5 so 2 wears stay available
    client.post(f"/api/wash/{item_id}")
    r = client.post("/settings/thresholds/reset", follow_redirects=False)
    assert r.status_code == 303

    for _ in range(2):
        client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})
    item = client.get(f"/api/items/{item_id}").json()
    assert item["wears_since_wash"] == 2
    assert item["state"] == "available"


def test_threshold_invalid_value_rejected(client):
    _login(client)
    r = client.post(
        "/settings/thresholds",
        data={"max_wears_tshirt": "999", "days_cap_tshirt": "21"},
        follow_redirects=False,
    )
    # 999 > MAX_WEAR_RANGE hi=60 → ValueError → redirect with error
    assert r.status_code == 303
    assert "error=" in r.headers.get("location", "")
    page = client.get("/settings?error=max_wears must be 1–60")
    # page still loads
    assert page.status_code == 200


def test_climate_hint_toggle(client):
    _login(client)
    r = client.post("/settings/climate", data={"climate": "1"}, follow_redirects=False)
    assert r.status_code == 303

    from closet_ai.db import get_db
    from closet_ai.services import app_settings

    with get_db() as conn:
        assert app_settings.climate_hint_enabled(conn) is True

    client.post("/settings/climate", follow_redirects=False)  # unchecked → off
    with get_db() as conn:
        assert app_settings.climate_hint_enabled(conn) is False


def test_item_detail_shows_wears_and_last_washed(client):
    _login(client)
    item_id = _mk(client, name="Blue jeans", type_="pants", color="blue")
    client.post("/api/log/undress", json={"ratings": {str(item_id): 3}})
    client.post(f"/api/wash/{item_id}")
    client.post("/api/log/undress", json={"ratings": {str(item_id): 2}})

    page = client.get(f"/items/{item_id}")
    assert page.status_code == 200
    assert "Wears since wash" in page.text
    assert "Last washed" in page.text
    # one wear since wash after re-log
    assert "1" in page.text


def test_item_detail_empty_history_states(client):
    _login(client)
    item_id = _mk(client, name="Fresh tee", type_="tshirt")
    page = client.get(f"/items/{item_id}")
    assert page.status_code == 200
    assert "Never washed yet" in page.text
    assert "No dirt ratings yet" in page.text
    assert "Last washed" in page.text
    assert "—" in page.text  # em-dash placeholder


def test_due_list_shows_last_washed_or_never(client):
    _login(client)
    item_id = _mk(client, name="Dirty", type_="tshirt")
    # trip due with dirt 5
    client.post("/api/log/undress", json={"ratings": {str(item_id): 5}})
    page = client.get("/due")
    assert page.status_code == 200
    assert "Dirty" in page.text
    assert "never washed" in page.text


def test_home_empty_state(client):
    _login(client)
    page = client.get("/")
    assert page.status_code == 200
    assert "No items yet" in page.text
    assert "Add your first garment" in page.text


def test_nav_includes_settings(client):
    _login(client)
    page = client.get("/")
    assert 'href="/settings"' in page.text
