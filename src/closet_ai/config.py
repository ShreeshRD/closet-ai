"""Paths and app config for closet-ai."""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(os.environ.get("CLOSET_AI_ROOT", Path(__file__).resolve().parents[2]))


def data_dir() -> Path:
    return Path(os.environ.get("CLOSET_AI_DATA", project_root() / "data"))


def models_dir() -> Path:
    return Path(os.environ.get("CLOSET_AI_MODELS", project_root() / "models"))


def images_dir() -> Path:
    return data_dir() / "images"


def backups_dir() -> Path:
    return data_dir() / "backups"


def db_path() -> Path:
    return data_dir() / "wardrobe.db"


def secret_key_path() -> Path:
    return data_dir() / "secret_key"


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


HOST = os.environ.get("CLOSET_AI_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLOSET_AI_PORT", "8420"))
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def ensure_dirs() -> None:
    for d in (data_dir(), images_dir(), backups_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)
