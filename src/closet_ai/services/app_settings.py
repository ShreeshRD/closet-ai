"""App settings: wash threshold overrides + climate_hint (Phase 3)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from closet_ai.db import ITEM_TYPES
from closet_ai.services.wash_rules import DEFAULT_THRESHOLDS

KEY_THRESHOLDS = "threshold_overrides"
KEY_CLIMATE = "climate_hint"

# dirt rules stay global in v1 (explainability); only per-type caps are tunable
MAX_WEAR_RANGE = (1, 60)
DAY_CAP_RANGE = (1, 365)


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def climate_hint_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, KEY_CLIMATE, "0") == "1"


def set_climate_hint(conn: sqlite3.Connection, enabled: bool) -> None:
    set_setting(conn, KEY_CLIMATE, "1" if enabled else "0")


def get_threshold_overrides(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """type → {max_wears, days_cap} (only keys present in overrides)."""
    raw = get_setting(conn, KEY_THRESHOLDS)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for t, v in data.items():
        if t not in ITEM_TYPES or not isinstance(v, dict):
            continue
        clean: dict[str, int] = {}
        for k in ("max_wears", "days_cap"):
            if k in v:
                try:
                    clean[k] = int(v[k])
                except (TypeError, ValueError):
                    continue
        if clean:
            out[t] = clean
    return out


def effective_thresholds(conn: sqlite3.Connection) -> dict[str, tuple[int, str, int]]:
    """Defaults merged with user overrides (for evaluate_due + settings UI)."""
    overrides = get_threshold_overrides(conn)
    merged: dict[str, tuple[int, str, int]] = {}
    for t in ITEM_TYPES:
        base = DEFAULT_THRESHOLDS.get(t, (10, "medium", 60))
        ov = overrides.get(t, {})
        max_wears = int(ov.get("max_wears", base[0]))
        days_cap = int(ov.get("days_cap", base[2]))
        merged[t] = (max_wears, base[1], days_cap)
    return merged


def save_threshold_overrides(
    conn: sqlite3.Connection, overrides: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """Validate and persist overrides; empty dict clears to defaults."""
    clean: dict[str, dict[str, int]] = {}
    for t, v in overrides.items():
        if t not in ITEM_TYPES or not isinstance(v, dict):
            continue
        entry: dict[str, int] = {}
        if "max_wears" in v:
            n = int(v["max_wears"])
            lo, hi = MAX_WEAR_RANGE
            if n < lo or n > hi:
                raise ValueError(f"max_wears must be {lo}–{hi}")
            entry["max_wears"] = n
        if "days_cap" in v:
            n = int(v["days_cap"])
            lo, hi = DAY_CAP_RANGE
            if n < lo or n > hi:
                raise ValueError(f"days_cap must be {lo}–{hi}")
            entry["days_cap"] = n
        if entry:
            clean[t] = entry
    set_setting(conn, KEY_THRESHOLDS, json.dumps(clean))
    return clean


def clear_threshold_overrides(conn: sqlite3.Connection) -> None:
    set_setting(conn, KEY_THRESHOLDS, "{}")


def rows_for_form(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-type rows for the settings table UI."""
    merged = effective_thresholds(conn)
    overrides = get_threshold_overrides(conn)
    rows = []
    for t in ITEM_TYPES:
        max_wears, weight, days_cap = merged[t]
        rows.append(
            {
                "type": t,
                "max_wears": max_wears,
                "days_cap": days_cap,
                "dirt_weight": weight,
                "overridden": t in overrides,
            }
        )
    return rows
