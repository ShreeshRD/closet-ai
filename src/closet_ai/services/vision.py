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
    ("maroon", (120, 40, 50)),
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
    """True for pixels likely garment (not near-white / not border background)."""
    arr = np.asarray(img, dtype=np.float32)
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
    dist = np.linalg.norm(arr - bg, axis=-1)
    near_white = (arr.min(axis=-1) > 230) & (arr.max(axis=-1) > 245)
    # Strong border color = walls/doors/tables behind the garment
    near_border_bg = dist < 45
    mask = ~(near_white | near_border_bg)
    # Prefer a solid center block when the subject is held up mid-frame
    cy, cx = h // 2, w // 2
    ry, rx = max(h // 4, 1), max(w // 4, 1)
    center = np.zeros((h, w), dtype=bool)
    center[cy - ry : cy + ry, cx - rx : cx + rx] = True
    if mask[center].sum() >= 200:
        # Dilate center a bit so edges of the garment still count
        grown = center.copy()
        grown[max(cy - ry - h // 10, 0) : cy + ry + h // 10, max(cx - rx - w // 10, 0) : cx + rx + w // 10] = True
        if mask[grown].sum() >= 200:
            return mask & grown
    if mask.sum() < 50:
        return np.ones((h, w), dtype=bool)
    return mask


def dominant_rgb(img: Image.Image) -> tuple[int, int, int]:
    arr = np.asarray(img, dtype=np.float32)
    mask = _background_mask(img)
    if mask.sum() < 50:
        mean = arr.reshape(-1, 3).mean(axis=0)
        return tuple(int(x) for x in mean)  # type: ignore[return-value]
    pixels = arr[mask]
    # Quantize and take the densest cluster near the foreground mean
    q = (pixels // 32).astype(np.int32)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    # Weight by count but stay near overall garment color (reject tiny outliers)
    fg_mean = pixels.mean(axis=0)
    best_score = -1.0
    best_rgb = fg_mean
    for k, c in zip(vals, counts):
        if c < max(20, len(pixels) * 0.02):
            continue
        sel = keys == k
        center = pixels[sel].mean(axis=0)
        # Prefer dense clusters that are not extreme outliers
        dist = float(np.linalg.norm(center - fg_mean))
        if dist > 90:
            continue
        score = c / (1.0 + dist)
        if score > best_score:
            best_score = score
            best_rgb = center
    rgb = best_rgb
    return tuple(int(np.clip(c, 0, 255)) for c in rgb)  # type: ignore[return-value]


def color_name(rgb: tuple[int, int, int]) -> str:
    r, g, b = (int(x) for x in rgb)
    best = "grey"
    best_d = float("inf")
    for name, ref in _NAMED_COLORS:
        if name == "gray":
            continue  # prefer "grey" spelling in UI
        d = sum((a - b) ** 2 for a, b in zip(rgb, ref))
        if d < best_d:
            best_d = d
            best = name
    # Maroon / dark red-brown garments: nearest-neighbor can land on grey
    # when saturation is moderate but hue is clearly red-brown.
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn > 25 and r >= g and r >= b and g < b + 30 and r > 70:
        if best in ("grey", "brown", "tan", "beige", "black") and r > g + 15 and r > b + 10:
            # dark red-leaning → maroon when brightness is low-ish
            if (r + g + b) / 3 < 150 and r > b:
                return "maroon"
    return best


def estimate_pattern(img: Image.Image) -> str:
    """Coarse pattern guess from center-crop texture. Human still confirms."""
    arr = np.asarray(img, dtype=np.float32)
    h, w, _ = arr.shape
    # Center crop — garment held up is usually mid-frame
    y0, y1 = int(h * 0.25), int(h * 0.75)
    x0, x1 = int(w * 0.25), int(w * 0.75)
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return "solid"
    # Luma
    luma = 0.299 * crop[..., 0] + 0.587 * crop[..., 1] + 0.114 * crop[..., 2]
    # High-frequency energy: difference from 8x downsampled smooth version
    small = np.asarray(
        Image.fromarray(luma.astype(np.uint8)).resize(
            (max(luma.shape[1] // 8, 1), max(luma.shape[0] // 8, 1)),
            Image.Resampling.BILINEAR,
        ),
        dtype=np.float32,
    )
    smooth = np.asarray(
        Image.fromarray(small.astype(np.uint8)).resize(
            (luma.shape[1], luma.shape[0]), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    )
    residual = luma - smooth
    detail = float(np.std(residual))
    # Color variation across the crop (florals often multi-hue)
    color_std = float(np.mean(np.std(crop.reshape(-1, 3), axis=0)))

    # Structural: column/row mean profiles for stripes
    col_means = luma.mean(axis=0)
    row_means = luma.mean(axis=1)
    # Autocorrelation-like peak: std of profile after light smoothing
    def _profile_score(profile: np.ndarray) -> float:
        if profile.size < 8:
            return 0.0
        k = max(3, profile.size // 20)
        kernel = np.ones(k) / k
        sm = np.convolve(profile, kernel, mode="same")
        return float(np.std(sm - np.mean(sm)))

    col_osc = _profile_score(col_means)
    row_osc = _profile_score(row_means)

    if detail < 4.0 and color_std < 18:
        return "solid"
    # Strong periodic row/column variation → stripes
    if col_osc > 12 and row_osc < 6 and detail > 6:
        return "striped"
    if row_osc > 12 and col_osc < 6 and detail > 6:
        return "striped"
    # Busy multi-color texture → floral / graphic / plaid-ish
    if detail > 14 and color_std > 28:
        # High color variety with soft blobs tends floral; hard edges plaid
        if color_std > 40 and detail > 18:
            return "floral"
        return "graphic"
    if detail > 10:
        return "graphic"
    if detail > 6:
        return "other"
    return "solid"


def estimate_shape(img: Image.Image) -> dict[str, str]:
    """Very light aspect/shape hints for type + sleeve prefill."""
    w, h = img.size
    # Portrait-ish tall garment often pants/shorts held up; wide often tops
    aspect = h / max(w, 1)  # >1 taller than wide
    # Default conservative guesses — human confirms before save
    type_guess = "tshirt"
    sleeve_guess = "short"
    if aspect >= 1.35:
        # Tall narrow: more like pants / trackpants / long shirt
        type_guess = "pants"
        sleeve_guess = "long"
    elif aspect <= 0.75:
        # Wide short: shorts / folded top
        type_guess = "shorts"
        sleeve_guess = "short"
    else:
        type_guess = "tshirt"
        sleeve_guess = "short"
    return {"type": type_guess, "sleeve": sleeve_guess}


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
    pattern = estimate_pattern(img)
    shape = estimate_shape(img)
    w, h = img.size
    return {
        "embedding": embedding(img),
        "color_rgb": rgb,
        "color": name,
        "pattern": pattern,
        "type": shape["type"],
        "sleeve": shape["sleeve"],
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
