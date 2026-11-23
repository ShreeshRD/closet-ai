"""Phase 2: capture + identity tests."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSET_AI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLOSET_AI_ROOT", str(tmp_path))

    from closet_ai import auth

    auth.reset_serializer()
    # Clear any pending captures between tests
    from closet_ai.routes import capture

    capture._pending.clear()

    from closet_ai.main import app

    with TestClient(app) as c:
        yield c


def _login(client: TestClient, pin: str = "1234") -> None:
    r = client.post("/login", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (200, 200)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_capture_page_requires_auth(client):
    r = client.get("/capture", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_capture_page_ok_when_logged_in(client):
    _login(client)
    r = client.get("/capture")
    assert r.status_code == 200
    assert "Capture" in r.text


def test_api_capture_requires_pin(client):
    r = client.post(
        "/api/capture",
        files={"file": ("x.png", _png_bytes((30, 60, 160)), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_capture_analyze_and_confirm_creates_item(client):
    _login(client, "5555")
    r = client.post(
        "/api/capture",
        files={"file": ("shirt.png", _png_bytes((40, 90, 170)), "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert body["photo_url"].startswith("/images/")
    assert body["prefill"]["color"] in {
        "blue", "navy", "black", "grey", "white", "red", "green",
        "brown", "beige", "tan", "yellow", "orange", "pink", "purple",
    }
    assert isinstance(body["matches"], list)

    # Second identical image should match first item after confirm
    r2 = client.post(
        "/api/capture/confirm",
        json={
            "token": body["token"],
            "type": "tshirt",
            "color": body["prefill"]["color"],
            "name": "Blue tee",
            "pattern": "solid",
            "sleeve": "short",
            "quantity": 1,
        },
    )
    assert r2.status_code == 200, r2.text
    created = r2.json()
    assert created["item"]["name"] == "Blue tee"
    assert created["item"]["primary_photo_path"]
    assert created["redirect"].startswith("/items/")

    # Same image again → identity match
    r3 = client.post(
        "/api/capture",
        files={"file": ("shirt2.png", _png_bytes((40, 90, 170)), "image/png")},
    )
    assert r3.status_code == 200
    matches = r3.json()["matches"]
    assert matches, "expected identity match for identical color image"
    assert matches[0]["item_id"] == created["item"]["id"]
    assert matches[0]["score"] >= 0.90


def test_capture_same_as_attaches_photo(client):
    _login(client, "9999")
    # Create item manually first
    r = client.post(
        "/api/items",
        json={"name": "Existing", "type": "pants", "color": "black"},
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    cap = client.post(
        "/api/capture",
        files={"file": ("p.png", _png_bytes((15, 15, 15)), "image/png")},
    )
    assert cap.status_code == 200
    token = cap.json()["token"]

    same = client.post(
        "/api/capture/same-as",
        data={"token": token, "item_id": str(item_id)},
    )
    assert same.status_code == 200, same.text
    assert same.json()["item"]["primary_photo_path"]


def test_capture_rejects_non_image(client):
    _login(client, "2222")
    r = client.post(
        "/api/capture",
        files={"file": ("a.txt", b"not an image at all really", "text/plain")},
    )
    assert r.status_code == 400


def test_image_serve_requires_auth_and_stays_in_base(client):
    _login(client, "3333")
    cap = client.post(
        "/api/capture",
        files={"file": ("z.png", _png_bytes((100, 50, 20)), "image/png")},
    )
    url = cap.json()["photo_url"]
    ok = client.get(url)
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/")

    # path traversal blocked
    bad = client.get("/images/../wardrobe.db")
    assert bad.status_code in (400, 404)
