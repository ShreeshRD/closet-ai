"""PIN auth: first-run set PIN, then unlock with session cookie."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from closet_ai.config import SESSION_MAX_AGE, data_dir, secret_key_path

MAX_FAILS = 10
LOCKOUT_SECONDS = 300

_session_serializer: URLSafeTimedSerializer | None = None


def pin_file():
    return data_dir() / "pin.hash"


def fail_file():
    return data_dir() / "pin_fails"


def _get_secret() -> bytes:
    data_dir().mkdir(parents=True, exist_ok=True)
    path = secret_key_path()
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def get_serializer() -> URLSafeTimedSerializer:
    global _session_serializer
    if _session_serializer is None:
        _session_serializer = URLSafeTimedSerializer(
            _get_secret(), salt="closet-ai-session"
        )
    return _session_serializer


def reset_serializer() -> None:
    """Test helper: force new serializer after secret path change."""
    global _session_serializer
    _session_serializer = None


def _hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000).hex()


def pin_is_set() -> bool:
    return pin_file().exists()


def set_pin(pin: str) -> None:
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise ValueError("PIN must be 4–8 digits")
    data_dir().mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    digest = _hash_pin(pin, salt)
    pin_file().write_text(f"{salt.hex()}:{digest}\n")
    if fail_file().exists():
        fail_file().unlink()


def verify_pin(pin: str) -> bool:
    if not pin_is_set() or not pin:
        return False
    raw = pin_file().read_text().strip()
    salt_hex, digest = raw.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    candidate = _hash_pin(pin, salt)
    return hmac.compare_digest(candidate, digest)


def _read_fails() -> tuple[int, float]:
    f = fail_file()
    if not f.exists():
        return 0, 0.0
    try:
        parts = f.read_text().strip().split(":")
        return int(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return 0, 0.0


def _write_fails(count: int, ts: float) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    fail_file().write_text(f"{count}:{ts}")


def lockout_remaining() -> float:
    fails, ts = _read_fails()
    if fails < MAX_FAILS:
        return 0.0
    remaining = LOCKOUT_SECONDS - (time.time() - ts)
    return max(0.0, remaining)


def try_unlock(pin: str) -> bool:
    if lockout_remaining() > 0:
        return False
    if verify_pin(pin):
        if fail_file().exists():
            fail_file().unlink()
        return True
    fails, ts = _read_fails()
    if fails and time.time() - ts > LOCKOUT_SECONDS:
        fails = 0
    fails += 1
    _write_fails(fails, time.time() if fails == 1 else ts)
    return False


def issue_session(response) -> None:
    token = get_serializer().dumps({"v": 1})
    response.set_cookie(
        "closet_session",
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session(response) -> None:
    response.delete_cookie("closet_session", path="/")


def session_valid(request: Request) -> bool:
    token = request.cookies.get("closet_session")
    if not token:
        return False
    try:
        get_serializer().loads(token, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


def require_session(request: Request) -> None:
    if not session_valid(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="PIN required",
            headers={"HX-Redirect": "/login"},
        )
