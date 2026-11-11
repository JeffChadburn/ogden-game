#!/usr/bin/env python3
"""Procedural (not photogrammetry, not copied) tileable material textures."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C


def _hash2(x, y, seed=0):
    n = np.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453
    return n - np.floor(n)


def value_noise(h, w, scale, seed=0):
    ys = np.linspace(0, scale, h, endpoint=False)
    xs = np.linspace(0, scale, w, endpoint=False)
    # wrap-friendly integer lattice
    xg, yg = np.meshgrid(xs, ys)
    x0 = np.floor(xg).astype(np.int32)
    y0 = np.floor(yg).astype(np.int32)
    fx = xg - x0
    fy = yg - y0
    sx = fx * fx * (3 - 2 * fx)
    sy = fy * fy * (3 - 2 * fy)
    def hsh(ix, iy):
        # periodic over `scale` if scale is int
        ix = np.mod(ix, int(scale))
        iy = np.mod(iy, int(scale))
        return _hash2(ix.astype(np.float64), iy.astype(np.float64), seed)
    v00 = hsh(x0, y0)
    v10 = hsh(x0 + 1, y0)
    v01 = hsh(x0, y0 + 1)
    v11 = hsh(x0 + 1, y0 + 1)
    v0 = v00 * (1 - sx) + v10 * sx
    v1 = v01 * (1 - sx) + v11 * sx
    return v0 * (1 - sy) + v1 * sy


def fbm(h, w, seed, octaves=5, base=4):
    acc = np.zeros((h, w), dtype=np.float64)
    amp = 1.0
    tot = 0.0
    sc = base
    for i in range(octaves):
        acc += amp * value_noise(h, w, sc, seed + i * 19)
        tot += amp
        amp *= 0.5
        sc *= 2
    return acc / tot


def to_img(arr):
    arr = np.clip(arr, 0, 1)
    if arr.ndim == 2:
        return Image.fromarray((arr * 255).astype(np.uint8), mode="L")
    return Image.fromarray((arr * 255).astype(np.uint8), mode="RGB")


def save(name, arr):
    path = C.ASSETS / "textures" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    to_img(arr).save(path, optimize=True)
    return path


def brick(h=512, w=512):
    y, x = np.indices((h, w))
    row = y // 16
    shift = (row % 2) * 32
    cx = (x + shift) % 64
    cy = y % 32
    mortar = (cx < 3) | (cy < 3)
    tone = 0.45 + 0.15 * fbm(h, w, 3, 4, 8)
    col = np.stack([tone * 0.72, tone * 0.32, tone * 0.24], axis=-1)
    mortar_c = np.array([0.55, 0.52, 0.48])
    col[mortar] = mortar_c
    return col


def siding(h=512, w=512):
    y, x = np.indices((h, w))
    plank = ((y // 18) % 7) / 7.0
    groove = (y % 18) < 2
    tone = 0.55 + 0.12 * plank[:, :, None] + 0.08 * fbm(h, w, 9, 4, 8)[:, :, None]
    col = tone * np.array([0.62, 0.55, 0.42])
    col[groove] *= 0.55
    return np.clip(col, 0, 1)


def metal(h=512, w=512):
    y, x = np.indices((h, w))
    rib = 0.5 + 0.5 * np.cos(x / 512 * 2 * np.pi * 24)
    tone = 0.35 + 0.2 * rib + 0.1 * fbm(h, w, 11, 3, 8)
    rust = np.clip(fbm(h, w, 21, 5, 6) - 0.62, 0, 1) * 2
    col = np.stack([tone, tone, tone * 1.05], axis=-1)
    col[..., 0] += rust * 0.25
    col[..., 1] += rust * 0.08
    col[..., 2] -= rust * 0.05
    return np.clip(col, 0, 1)


def concrete(h=512, w=512):
    n = fbm(h, w, 15, 5, 8)
    tone = 0.55 + 0.12 * (n - 0.5)
    return np.stack([tone, tone * 0.99, tone * 0.95], axis=-1)


def glass(h=512, w=512):
    y, x = np.indices((h, w))
    pane_x = (x % 64) > 4
    pane_y = (y % 96) > 5
    pane = pane_x & pane_y
    col = np.zeros((h, w, 3))
    col[:] = (0.22, 0.24, 0.26)  # mullion
    g = 0.35 + 0.15 * fbm(h, w, 4, 3, 4)
    col[pane] = np.stack([g * 0.45, g * 0.55, g * 0.65], axis=-1)[pane]
    return col


def asphalt(h=512, w=512):
    n = fbm(h, w, 7, 5, 16)
    grit = value_noise(h, w, 128, 99)
    tone = 0.16 + 0.08 * n + 0.04 * grit
    return np.stack([tone, tone, tone * 0.98], axis=-1)


def sidewalk(h=512, w=512):
    y, x = np.indices((h, w))
    joint = (x % 64 < 2) | (y % 64 < 2)
    tone = 0.62 + 0.08 * fbm(h, w, 18, 4, 8)
    col = np.stack([tone, tone * 0.99, tone * 0.94], axis=-1)
    col[joint] *= 0.7
    return np.clip(col, 0, 1)


def grass(h=512, w=512):
    n = fbm(h, w, 2, 6, 8)
    n2 = fbm(h, w, 5, 4, 32)
    g = 0.28 + 0.22 * n + 0.08 * n2
    col = np.stack([g * 0.45, g, g * 0.32], axis=-1)
    return np.clip(col, 0, 1)


def rock(h=512, w=512):
    n = fbm(h, w, 12, 6, 6)
    cracks = np.clip(0.55 - fbm(h, w, 30, 3, 10), 0, 1)
    tone = 0.35 + 0.25 * n - 0.15 * cracks
    return np.stack([tone * 0.95, tone * 0.92, tone * 0.88], axis=-1)


def dirt(h=512, w=512):
    n = fbm(h, w, 8, 5, 8)
    tone = 0.35 + 0.18 * n
    return np.stack([tone * 0.85, tone * 0.62, tone * 0.38], axis=-1)


def scrub(h=512, w=512):
    g = grass(h, w)
    d = dirt(h, w)
    m = fbm(h, w, 27, 4, 10)[..., None]
    return np.clip(g * (0.4 + 0.4 * m) + d * (0.6 - 0.3 * m), 0, 1)


def ballast(h=512, w=512):
    n = fbm(h, w, 33, 4, 32)
    g = value_noise(h, w, 160, 41)
    tone = 0.28 + 0.2 * n + 0.12 * g
    return np.stack([tone * 0.85, tone * 0.8, tone * 0.72], axis=-1)


def water(h=512, w=512):
    n = fbm(h, w, 40, 5, 6)
    tone = 0.25 + 0.15 * n
    return np.stack([tone * 0.35, tone * 0.55, tone * 0.7], axis=-1)


def wood(h=512, w=512):
    y, x = np.indices((h, w))
    grain = 0.5 + 0.5 * np.sin((x + 8 * fbm(h, w, 44, 3, 4) * 20) / 512 * 2 * np.pi * 10)
    tone = 0.4 + 0.2 * grain + 0.08 * fbm(h, w, 45, 3, 8)
    return np.stack([tone * 0.7, tone * 0.5, tone * 0.28], axis=-1)


def main():
    C.ensure_dirs()
    makers = {
        "grass.png": grass,
        "rock.png": rock,
        "dirt.png": dirt,
        "scrub.png": scrub,
        "pavement.png": sidewalk,
        "asphalt.png": asphalt,
        "sidewalk.png": sidewalk,
        "ballast.png": ballast,
        "brick.png": brick,
        "siding.png": siding,
        "metal.png": metal,
        "concrete.png": concrete,
        "glass.png": glass,
        "water.png": water,
        "wood.png": wood,
    }
    for name, fn in makers.items():
        p = save(name, fn())
        print(" ", p)


if __name__ == "__main__":
    main()
