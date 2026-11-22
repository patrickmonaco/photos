"""Étalonnage de la valeur "tilt" de Picasa.

Quand une photo a été « Enregistrée » dans Picasa, l'original est déplacé dans
.picasaoriginals et la version redressée le remplace. En comparant les deux images
(points d'intérêt ORB + transformation de similitude), on mesure l'angle réellement
appliqué et on en déduit le nombre de degrés par unité de "tilt".
"""
from __future__ import annotations

import math
import os
import statistics
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import IMAGE_EXTENSIONS
from .picasa import find_ini, parse_sections, _read_text


def _raw_tilts(folder: Path) -> dict[str, float]:
    ini = find_ini(folder)
    out: dict[str, float] = {}
    if not ini:
        return out
    for name, kv in parse_sections(_read_text(ini)):
        for entry in kv.get("filters", "").split(";"):
            fname, _, params = entry.strip().partition("=")
            if fname.strip().lower() == "tilt":
                args = params.split(",")
                if len(args) >= 2 and args[0].strip() == "1":
                    try:
                        val = float(args[1])
                        if val:
                            out[name] = val
                    except ValueError:
                        pass
    return out


def _gray(path: Path, max_side: int = 1400) -> np.ndarray:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("L")
        im.thumbnail((max_side, max_side))
        return np.asarray(im)


def measure_rotation(original: Path, saved: Path) -> float | None:
    """Angle (degrés, positif = sens horaire) appliqué pour passer de l'original au fichier enregistré."""
    a, b = _gray(original), _gray(saved)
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 20 or len(kb) < 20:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 20:
        return None
    src = np.float32([ka[m.queryIdx].pt for m in matches])
    dst = np.float32([kb[m.trainIdx].pt for m in matches])
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if M is None or inl is None or inl.sum() < 15:
        return None
    return math.degrees(math.atan2(M[1, 0], M[0, 0]))


def calibrate(roots: list[str]) -> dict:
    samples: list[dict] = []
    for root in roots:
        for dirpath, dirnames, _ in os.walk(root):
            if ".picasaoriginals" not in dirnames:
                continue
            folder = Path(dirpath)
            orig_dir = folder / ".picasaoriginals"
            tilts = {**_raw_tilts(orig_dir), **_raw_tilts(folder)}
            for name, raw in tilts.items():
                orig, saved = orig_dir / name, folder / name
                if not (orig.is_file() and saved.is_file()) or orig.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                deg = measure_rotation(orig, saved)
                if deg is not None and abs(deg) > 0.05:
                    # Même convention que picasa.py : degrés = -tilt × facteur
                    samples.append({"file": str(saved), "tilt": raw, "deg": round(deg, 3),
                                    "factor": round(-deg / raw, 4)})
    result = {"samples": samples, "count": len(samples), "factor": None}
    if samples:
        result["factor"] = round(statistics.median(s["factor"] for s in samples), 4)
        if len(samples) > 1:
            result["spread"] = round(statistics.pstdev(s["factor"] for s in samples), 4)
    return result
