"""Génère une photothèque de démonstration avec des .picasa.ini réalistes.

  python tests/make_sample.py /chemin/vers/Exemple
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ExifTags

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phototheque import perspective  # noqa: E402
from phototheque.render import straighten  # noqa: E402

TRUE_TILT_FACTOR = 7.5  # facteur "secret" utilisé pour simuler un enregistrement Picasa


def scene(seed: int, w: int = 1600, h: int = 1200) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.uint8)
    top, bottom = rng.integers(90, 200, 3), rng.integers(20, 120, 3)
    for y in range(h):
        img[y] = top + (bottom - top) * (y / h)
    for _ in range(140):
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        x, y, r = int(rng.integers(0, w)), int(rng.integers(0, h)), int(rng.integers(8, 90))
        if rng.random() < 0.5:
            cv2.circle(img, (x, y), r, c, -1)
        else:
            cv2.rectangle(img, (x, y), (x + r, y + int(r * 0.7)), c, -1)
    return img


def facade(w: int = 1600, h: int = 1200) -> np.ndarray:
    img = np.full((h, w, 3), (215, 205, 190), np.uint8)
    img[:120] = (235, 200, 150)
    cv2.rectangle(img, (180, 120), (1420, 1200), (190, 175, 160), -1)
    for x in range(240, 1380, 125):
        for y in range(180, 1100, 150):
            cv2.rectangle(img, (x, y), (x + 70, y + 100), (70, 80, 95), -1)
            cv2.rectangle(img, (x, y), (x + 70, y + 100), (40, 40, 40), 3)
    return img


def save(img: np.ndarray | Image.Image, path: Path, date: str | None = None, f35: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    pil = img if isinstance(img, Image.Image) else Image.fromarray(img)
    exif = Image.Exif()
    if date or f35:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        if date:
            sub[36867] = date
        if f35:
            sub[41989] = f35
    pil.save(path, "JPEG", quality=90, exif=exif)


def tilt_like_picasa(img: np.ndarray, pitch: float) -> np.ndarray:
    h, w = img.shape[:2]
    K = perspective.intrinsics(w, h, 28)
    R = cv2.Rodrigues(np.radians([pitch, 0, 0]))[0]
    return cv2.warpPerspective(img, K @ R @ np.linalg.inv(K), (w, h), borderMode=cv2.BORDER_REPLICATE)


def build(root: Path) -> None:
    vac = root / "2019 Vacances Bretagne"
    save(scene(1), vac / "IMG_0001.jpg", "2019:07:14 10:12:00")
    save(scene(2), vac / "IMG_0002.jpg", "2019:07:14 16:40:00")
    save(tilt_like_picasa(facade(), 14), vac / "IMG_0003.jpg", "2019:07:15 11:05:00", f35=28)
    save(scene(3), vac / "IMG_0004.jpg", "2019:07:16 09:30:00")
    (vac / ".picasa.ini").write_text(
        "[Picasa]\nname=2019 Vacances Bretagne\n"
        "[Contacts2]\n8e62398ebda8c1a5=Marie Dupont;;\n"
        "[.album:a1b2c3d4e5f6]\nname=Meilleures photos\ntoken=a1b2c3d4e5f6\ndate=2019-08-01T10:00:00+02:00\n"
        "[IMG_0001.jpg]\nstar=yes\nkeywords=mer,plage\n"
        "filters=tilt=1,0.200000,0.000000;crop64=1,10002000e000d000;\n"
        "faces=rect64(3f845bcb59418507),8e62398ebda8c1a5\n"
        "albums=a1b2c3d4e5f6\n"
        "[IMG_0002.jpg]\nrotate=rotate(1)\nfilters=bw=1;\n"
        "[IMG_0003.jpg]\nkeywords=architecture\nalbums=a1b2c3d4e5f6\n"
        "[IMG_0004.jpg]\nfilters=enhance=1;redeye=1;finetune2=1,0.000000,0.000000,0.190000,00000000,0.000000;\n",
        encoding="utf-8")

    noel = root / "2021" / "Noël"
    save(scene(10), noel / "IMG_2001.jpg", "2021:12:25 19:00:00")
    save(scene(11), noel / "IMG_2002.jpg", "2021:12:25 19:30:00")
    (noel / ".picasa.ini").write_text(
        "[.album:f0e1d2c3b4a5]\nname=Famille\ntoken=f0e1d2c3b4a5\n"
        "[IMG_2001.jpg]\nalbums=a1b2c3d4e5f6,f0e1d2c3b4a5\nstar=yes\n"
        "[IMG_2002.jpg]\nalbums=f0e1d2c3b4a5\nkeywords=famille\n", encoding="utf-8")

    # Photos « enregistrées » par Picasa : original dans .picasaoriginals
    cal = root / "2020 Montagne"
    for i, raw in enumerate((0.30, -0.45, 0.6)):
        orig = Image.fromarray(scene(20 + i))
        save(orig, cal / ".picasaoriginals" / f"IMG_30{i}.jpg", "2020:02:10 12:00:00")
        # Picasa : un tilt positif tourne dans le sens anti-horaire
        save(straighten(orig, -raw * TRUE_TILT_FACTOR), cal / f"IMG_30{i}.jpg", "2020:02:10 12:00:00")
    (cal / ".picasaoriginals" / ".picasa.ini").write_text(
        "".join(f"[IMG_30{i}.jpg]\nfilters=tilt=1,{raw:.6f},0.000000;\n"
                for i, raw in enumerate((0.30, -0.45, 0.6))), encoding="utf-8")

    # Une photo sans EXIF, sans .picasa.ini
    save(scene(40), root / "Divers" / "scan.jpg")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "Exemple"))
    print("ok")
