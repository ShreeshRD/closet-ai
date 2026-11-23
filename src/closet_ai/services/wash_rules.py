"""Composite wash / due rules (locked design §3.7)."""
from __future__ import annotations

from datetime import datetime, timezone

# type -> (max_wears_since_wash, dirt_weight, days_cap)
DEFAULT_THRESHOLDS: dict[str, tuple[int, str, int]] = {
    "underwear": (3, "high", 7),
    "socks": (5, "high", 14),
    "tshirt": (5, "medium", 21),
    "polo": (4, "medium", 21),
    "full_sleeve_tshirt": (5, "medium", 21),
    "three_fourths": (5, "medium", 21),
    "shirt": (4, "medium", 21),
    "shorts": (6, "medium", 30),
    "trackpants": (6, "medium", 30),
    "pants": (10, "low", 60),
    "jacket": (15, "low", 90),
}


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_since(iso: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    delta = now - _parse_iso(iso)
    return max(0, delta.days)


def evaluate_due(
    *,
    item_type: str,
    wears_since_wash: int,
    latest_dirt: int | None,
    avg_dirt: float | None,
    last_washed_at: str | None,
    created_at: str,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Return (is_due, reason)."""
    now = now or datetime.now(timezone.utc)
    max_wears, _weight, day_cap = DEFAULT_THRESHOLDS.get(item_type, (10, "medium", 60))

    if wears_since_wash >= max_wears:
        return True, f"wears {wears_since_wash} ≥ {max_wears}"
    if latest_dirt is not None and latest_dirt >= 4:
        return True, f"last dirt rating {latest_dirt} ≥ 4"
    if avg_dirt is not None and avg_dirt >= 3:
        return True, f"avg dirt {avg_dirt:.1f} ≥ 3"
    since_ref = last_washed_at or created_at
    days = days_since(since_ref, now)
    if days >= day_cap:
        return True, f"{days} days since wash ≥ {day_cap}"
    return False, None
