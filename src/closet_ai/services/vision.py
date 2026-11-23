"""Local photo embedding + light attribute heuristics (interim, no GPU model).

Plan Phase 1 interim: full-frame features for identity match and form prefill.
Human still confirms type/pattern/sleeve at catalog time.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# Thumbnail used for embedding (keep small + fast)
_THUMB = 32
# Embedding: flattened RGB thumb (32*32*3) + coarse hist (8*8*8 / simplified)
_HIST_BINS = 8

# Named colors for prefill (centroid RGB approx)
_NAMED_COLORS: list[tuple[str, tuple[int, int, int]]] = [
    ("black", (20, 20, 20)),
    ("white", (245, 245, 245)),
    ("grey", (128, 128, 128)),
    ("gray", (128, 128, 128)),
    ("navy", (20, 30, 70)),
    ("blue", (50, 100, 180)),
    ("red", (180, 50, 50)),
    ("green", (40, 130, 70)),
    ("yellow", (220, 200, 60)),
    ("orange", (220, 130, 50)),
    ("pink", (220, 140, 170)),
    ("purple", (120, 70, 150)),
    ("brown", (110, 75, 40)),
    ("beige", (210, 195, 160)),
    ("tan", (195, 165, 120)),
]


def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    # Cap very large phone photos
    max_side = 1280
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def save_jpeg(img: Image.Image, dest: Path, quality: int = 88) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="JPEG", quality=quality, optimize=True)
    return str(dest)


def _background_mask(img: Image.Image) -> np.ndarray:
    """True for pixels likely background (near white / very light edges)."""
    arr = np.asarray(img, dtype=np.float32)
    # Sample border for background estimate
    h, w, _ = arr.shape
    border = np.concatenate(
        [
            arr[0:2, :, :].reshape(-1, 3),
            arr[-2:, :, :].reshape(-1, 3),
            arr[:, 0:2, :].reshape(-1, 3),
            arr[:, -2:, :].reshape(-1, 3),
        ],
        axis=0,
    )
    bg = np.median(border, axis=0)
    # Also treat near-white as bg
    dist = np.linalg.norm(arr - bg, axis=-1)
    near_white = (arr.min(axis=-1) > 230) & (arr.max(axis=-1) > 245)
    return (dist > 40) & ~near_white


def dominant_rgb(img: Image.Image) -> tuple[int, int, int]:
    arr = np.asarray(img, dtype=np.float32)
    mask = _background_mask(img)
    if mask.sum() < 50:
        # Fallback: overall mean
        mean = arr.reshape(-1, 3).mean(axis=0)
        return tuple(int(x) for x in mean)  # type: ignore[return-value]
    pixels = arr[mask]
    # k-means-lite: quantize to 8 levels and take mode-ish center
    q = (pixels // 32).astype(np.int32)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    top = vals[np.argsort(counts)[-3:]]
    centers = []
    for k in top:
        sel = keys == k
        centers.append(pixels[sel].mean(axis=0))
    stacked = np.stack(centers, axis=0)
    # Pick the cluster closest to overall mean of foreground (stable)
    fg_mean = pixels.mean(axis=0)
    idx = int(np.argmin(np.linalg.norm(stacked - fg_mean, axis=-1)))
    rgb = stacked[idx]
    return tuple(int(np.clip(c, 0, 255)) for c in rgb)  # type: ignore[return-value]


def color_name(rgb: tuple[int, int, int]) -> str:
    best = "grey"
    best_d = float("inf")
    for name, ref in _NAMED_COLORS:
        if name == "gray":
            continue  # prefer "grey" spelling in UI
        d = sum((a - b) ** 2 for a, b in zip(rgb, ref))
        if d < best_d:
            best_d = d
            best = name
    return best


def _hist(img: Image.Image) -> np.ndarray:
    arr = np.asarray(img, dtype=np.float32) / 255.0
    hist = np.zeros((_HIST_BINS, _HIST_BINS, _HIST_BINS), dtype=np.float32)
    idx = (arr * _HIST_BINS).astype(np.int32)
    np.clip(idx, 0, _HIST_BINS - 1, out=idx)
    for i in range(_HIST_BINS):
        for j in range(_HIST_BINS):
            for k in range(_HIST_BINS):
                hist[i, j, k] = np.sum((idx[..., 0] == i) & (idx[..., 1] == j) & (idx[..., 2] == k))
    hist = hist.reshape(-1)
    s = hist.sum()
    return hist / s if s > 0 else hist


def embedding(img: Image.Image) -> bytes:
    """Compact feature vector: RGB thumb + color histogram (L2-normalized)."""
    thumb = img.resize((_THUMB, _THUMB), Image.Resampling.BILINEAR)
    flat = np.asarray(thumb, dtype=np.float32).reshape(-1) / 255.0
    hist = _hist(img)
    vec = np.concatenate([flat * 0.35, hist * 0.65]).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    # pack as float32 LE
    return struct.pack(f"<{vec.size}f", *vec.tolist())


def load_embedding(blob: bytes) -> np.ndarray | None:
    if not blob or len(blob) % 4 != 0:
        return None
    n = len(blob) // 4
    return np.array(struct.unpack(f"<{n}f", blob), dtype=np.float32)


def similarity(a: bytes, b: bytes) -> float:
    va, vb = load_embedding(a), load_embedding(b)
    if va is None or vb is None or va.size != vb.size:
        return 0.0
    return float(np.dot(va, vb))


def analyze(data: bytes) -> dict[str, Any]:
    """Full-frame analysis for confirm-form prefill + identity embedding."""
    img = load_image(data)
    rgb = dominant_rgb(img)
    name = color_name(rgb)
    # Aspect hint only — sleeve/pattern still human-confirmed
    w, h = img.size
    return {
        "embedding": embedding(img),
        "color_rgb": rgb,
        "color": name,
        "width": w,
        "height": h,
        "image": img,
    }


def find_matches(
    emb: bytes,
    candidates: list[tuple[int, bytes]],
    *,
    threshold: float = 0.92,
    limit: int = 3,
) -> list[tuple[int, float]]:
    """Return (item_id, score) sorted desc, above threshold."""
    scored: list[tuple[int, float]] = []
    for item_id, other in candidates:
        if not other:
            continue
        s = similarity(emb, other)
        if s >= threshold:
            scored.append((item_id, s))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]
