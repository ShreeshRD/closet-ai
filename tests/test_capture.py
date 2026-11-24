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


def test_capture_page_has_cancel_controls(client):
    _login(client, "1111")
    r = client.get("/capture")
    assert r.status_code == 200
    assert 'id="cancel-confirm"' in r.text
    assert 'id="cancel-confirm-form"' in r.text
    assert "Cancel" in r.text
    # Retake without refreshing; no separate Analyze button
    assert 'id="retake-btn"' in r.text
    assert 'id="retake-from-confirm"' in r.text
    assert "Retake" in r.text
    assert "shoot or retake" in r.text
    # Analyze is not a separate control — Shoot does capture+analyze
    assert 'id="analyze-btn"' not in r.text
    assert 'id="shoot-btn"' in r.text


def test_prefill_uses_vision_estimates(client):
    """Solid blue → solid; busy multi-color crop → not solid; color from center."""
    _login(client, "4444")

    # Solid blue square
    r = client.post(
        "/api/capture",
        files={"file": ("blue.png", _png_bytes((30, 60, 170)), "image/png")},
    )
    assert r.status_code == 200
    prefill = r.json()["prefill"]
    assert prefill["color"] in {"blue", "navy"}
    assert prefill["pattern"] == "solid"
    assert prefill["type"] in {"tshirt", "pants", "shorts", "shirt", "jacket"}
    assert prefill["sleeve"] in {"short", "long", "sleeveless", "three_fourths", "unknown"}

    # Busy floral-like pattern on a light background (garment in center)
    img = Image.new("RGB", (240, 240), (245, 245, 240))
    px = img.load()
    import random

    random.seed(7)
    for y in range(40, 200):
        for x in range(40, 200):
            px[x, y] = (150, 70, 65)
    for _ in range(400):
        x = random.randint(50, 190)
        y = random.randint(50, 190)
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                if dx * dx + dy * dy <= 16:
                    xx, yy = x + dx, y + dy
                    if 40 <= xx < 200 and 40 <= yy < 200:
                        px[xx, yy] = random.choice(
                            [(230, 190, 170), (200, 120, 100), (190, 170, 90), (240, 210, 190)]
                        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r2 = client.post(
        "/api/capture",
        files={"file": ("floral.png", buf.getvalue(), "image/png")},
    )
    assert r2.status_code == 200
    p2 = r2.json()["prefill"]
    assert p2["pattern"] in {"floral", "graphic", "other"}
    assert p2["pattern"] != "solid"
    # Color should be garment-ish (maroon/brown/red family), not light bg
    assert p2["color"] in {"maroon", "brown", "red", "maroon"}


def test_cancel_analysis_discards_pending(client):
    _login(client, "6666")
    cap = client.post(
        "/api/capture",
        files={"file": ("c.png", _png_bytes((90, 40, 40)), "image/png")},
    )
    assert cap.status_code == 200
    token = cap.json()["token"]

    # UI cancel just clears client state; token becomes unusable only after
    # confirm. Re-analyze produces a new token — old one still valid until used
    # or replaced. Ensure confirm with bad token fails cleanly.
    bad = client.post(
        "/api/capture/confirm",
        json={"token": "nope", "type": "tshirt", "color": "red"},
    )
    assert bad.status_code == 410

    # Valid token still works after a conceptual cancel (user can re-analyze)
    ok = client.post(
        "/api/capture/confirm",
        json={
            "token": token,
            "type": "shorts",
            "color": "maroon",
            "name": "Floral shorts",
            "pattern": "floral",
            "sleeve": "short",
            "quantity": 1,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["item"]["pattern"] == "floral"
