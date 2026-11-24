"""Hands-free continuous voice: shoot + retake only (no analyze command)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSET_AI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLOSET_AI_ROOT", str(tmp_path))

    from closet_ai import auth
    from closet_ai.services import voice

    auth.reset_serializer()
    voice.reset_runtime_state()

    from closet_ai.main import app

    with TestClient(app) as c:
        yield c
    voice.reset_runtime_state()


def _login(client: TestClient, pin: str = "1234") -> None:
    r = client.post("/login", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303


def test_match_command_shoot_and_retake():
    from closet_ai.services.voice import match_command

    assert match_command("shoot") == "shoot"
    assert match_command("Shoot!") == "shoot"
    assert match_command("take a photo") == "shoot"
    assert match_command("snap it") == "shoot"
    assert match_command("cheese") == "shoot"

    assert match_command("retake") == "retake"
    assert match_command("Retake.") == "retake"
    assert match_command("again") == "retake"
    assert match_command("try again") == "retake"
    assert match_command("start over") == "retake"
    assert match_command("redo") == "retake"

    # analyze is no longer a command
    assert match_command("analyze") is None
    assert match_command("Analyze.") is None

    # noise
    assert match_command("hello world") is None
    assert match_command("") is None
    assert match_command("what time is it") is None
    assert match_command("add milk to the list") is None


def test_voice_endpoints_require_auth(client):
    assert client.get("/api/voice-command", follow_redirects=False).status_code == 401
    assert client.post("/api/voice/listen", follow_redirects=False).status_code == 401
    assert client.post("/api/voice/stop", follow_redirects=False).status_code == 401
    assert client.get("/api/voice/status", follow_redirects=False).status_code == 401


class _FakeMic:
    def __init__(self, chunks: list[bytes], delay_s: float = 0.0):
        self._chunks = list(chunks)
        self.closed = False
        self._delay_s = delay_s

    def read_chunk(self) -> bytes:
        if self._delay_s:
            time.sleep(self._delay_s)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


def _tone_chunks(seconds: float, amp: int = 8000) -> list[bytes]:
    import math
    import struct

    n = int(16000 * seconds)
    raw = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * 220 * i / 16000))
        raw += struct.pack("<h", v)
    step = 3200
    chunks = [bytes(raw[i : i + step]) for i in range(0, len(raw), step)]
    chunks.extend([b"\x00\x00" * 1600] * 5)
    return chunks


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_for_event(listener, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = listener.pop_command()
        if event.get("seq"):
            return event
        time.sleep(0.02)
    raise AssertionError(f"no event: {listener.status()}")


def test_listener_fires_shoot():
    from closet_ai.services.voice import VoiceListener

    calls: list[str] = []

    def transcribe(path: Path) -> str:
        calls.append(str(path))
        return "please shoot now"

    mic = _FakeMic(_tone_chunks(0.5))
    listener = VoiceListener(
        transcribe=transcribe,
        mic_factory=lambda: mic,
        clock=time.monotonic,
    )
    listener.start()
    assert _wait_until(lambda: bool(listener.status()["heard"]), 3.0), listener.status()
    event = _wait_for_event(listener)
    assert calls
    assert event["command"] == "shoot"
    listener.stop()
    assert mic.closed


def test_listener_fires_retake():
    from closet_ai.services.voice import VoiceListener

    def transcribe(path: Path) -> str:
        return "retake"

    listener = VoiceListener(
        transcribe=transcribe,
        mic_factory=lambda: _FakeMic(_tone_chunks(0.4)),
        clock=time.monotonic,
    )
    listener.start()
    assert _wait_until(lambda: bool(listener.status()["heard"]), 3.0), listener.status()
    event = _wait_for_event(listener)
    assert event["command"] == "retake"
    listener.stop()


def test_listener_ignores_analyze_and_noise():
    from closet_ai.services.voice import VoiceListener

    texts = iter(["analyze", "add milk to the list"])

    def transcribe(path: Path) -> str:
        return next(texts)

    chunks = _tone_chunks(0.35)
    chunks.extend(_tone_chunks(0.35))
    listener = VoiceListener(
        transcribe=transcribe,
        mic_factory=lambda: _FakeMic(chunks, delay_s=0.05),
        clock=time.monotonic,
    )
    listener.start()
    assert _wait_until(lambda: bool(listener.status()["heard"]), 3.0), listener.status()
    first = _wait_for_event(listener)
    assert first["command"] is None
    assert "analyze" in first["heard"].lower()
    listener.stop()


def test_start_stop_endpoints(client):
    _login(client)
    r = client.post("/api/voice/listen")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
    assert client.get("/api/voice/status").status_code == 200
    assert client.post("/api/voice/stop").json().get("running") is False
    assert client.get("/api/voice-command").json().get("command") is None


def test_shoot_then_retake_both_fire():
    from closet_ai.services.voice import VoiceListener

    chunks = _tone_chunks(0.35)
    chunks.extend(_tone_chunks(0.35))
    texts = iter(["shoot", "retake"])

    def transcribe(path: Path) -> str:
        return next(texts)

    listener = VoiceListener(
        transcribe=transcribe,
        mic_factory=lambda: _FakeMic(chunks, delay_s=0.05),
        clock=time.monotonic,
    )
    listener.start()
    assert _wait_until(lambda: bool(listener.status()["heard"]), 5.0), listener.status()
    first = _wait_for_event(listener, timeout=3.0)
    assert first["command"] == "shoot"

    second = {"command": None}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        ev = listener.pop_command()
        if ev.get("seq"):
            second = ev
            break
        time.sleep(0.02)
    assert second.get("command") == "retake", second
    listener.stop()
