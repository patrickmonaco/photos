"""Lecture des métadonnées d'une image : dimensions, date, focale, mots-clés IPTC/XMP."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags, IptcImagePlugin

TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_DATETIME = 306
TAG_ORIENTATION = 274
TAG_FOCAL_35 = 41989
TAG_FOCAL = 37386
TAG_MAKE = 271
TAG_MODEL = 272
TAG_LENS = 42036


def _parse_exif_date(value) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s[:19], fmt)
            if d.year > 1900:
                return d
        except ValueError:
            continue
    return None


def _xmp_keywords(img: Image.Image) -> list[str]:
    try:
        xmp = img.getxmp()  # nécessite defusedxml
    except Exception:
        return []
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "subject":
                    walk_list(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    def walk_list(v):
        if isinstance(v, dict):
            items = v.get("Bag", v.get("Seq", {})).get("li", [])
            if isinstance(items, str):
                items = [items]
            found.extend(str(i) for i in items if isinstance(i, str))
        elif isinstance(v, str):
            found.append(v)

    walk(xmp)
    return found


def _iptc_keywords(img: Image.Image) -> tuple[list[str], str | None]:
    try:
        info = IptcImagePlugin.getiptcinfo(img) or {}
    except Exception:
        return [], None
    kws = info.get((2, 25), [])
    if isinstance(kws, bytes):
        kws = [kws]
    keywords = [_decode(k) for k in kws]
    caption = info.get((2, 120))
    if isinstance(caption, list):
        caption = caption[0] if caption else None
    return [k for k in keywords if k], (_decode(caption) if caption else None)


def _decode(b) -> str:
    if isinstance(b, str):
        return b.strip()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace").strip()


def read_metadata(path: Path) -> dict:
    meta: dict = {"width": None, "height": None, "taken_at": None, "date_source": "file",
                  "f35": None, "keywords": [], "caption": None, "camera": "", "orientation": 1,
                  "media": "image", "duration": None, "codec": None}
    from . import media
    kind = media.kind_of(path)
    if kind == "video":
        info = media.probe(path)
        meta.update(media="video", width=info["width"], height=info["height"], duration=info["duration"],
                    codec=info["codec"])
        if info["created"]:
            meta["taken_at"], meta["date_source"] = info["created"], "exif"
    elif kind == "raw":
        info = media.raw_metadata(path)
        meta.update({k: info[k] for k in ("width", "height", "camera", "orientation", "f35")})
        if info["taken_at"]:
            meta["taken_at"], meta["date_source"] = info["taken_at"], "exif"
    if kind != "image":
        if not meta["taken_at"]:
            meta["taken_at"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        return meta
    try:
        with Image.open(path) as img:
            w, h = img.size
            exif = img.getexif()
            orientation = exif.get(TAG_ORIENTATION, 1) if exif else 1
            meta["orientation"] = int(orientation or 1)
            meta["camera"] = camera_name(exif)
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            meta["width"], meta["height"] = w, h
            if exif:
                sub = exif.get_ifd(ExifTags.IFD.Exif)
                d = (_parse_exif_date(sub.get(TAG_DATETIME_ORIGINAL))
                     or _parse_exif_date(sub.get(TAG_DATETIME_DIGITIZED))
                     or _parse_exif_date(exif.get(TAG_DATETIME)))
                if d:
                    meta["taken_at"] = d.isoformat(timespec="seconds")
                    meta["date_source"] = "exif"
                f35 = sub.get(TAG_FOCAL_35)
                if f35:
                    meta["f35"] = float(f35)
            kws, caption = _iptc_keywords(img)
            xmp_kws = _xmp_keywords(img)
            seen = set()
            meta["keywords"] = [k for k in kws + xmp_kws if not (k.lower() in seen or seen.add(k.lower()))]
            meta["caption"] = caption
    except Exception:
        pass
    if not meta["taken_at"]:
        meta["taken_at"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return meta


_DATE_PARTS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def date_parts(iso: str | None) -> tuple[int | None, int | None, int | None]:
    m = _DATE_PARTS.match(iso or "")
    if not m:
        return None, None, None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def camera_name(exif) -> str:
    """« Marque Modèle » (sans répéter la marque), plus l'objectif s'il est connu."""
    if not exif:
        return ""
    clean = lambda v: str(v or "").replace("\x00", "").strip()
    make, model = clean(exif.get(TAG_MAKE)), clean(exif.get(TAG_MODEL))
    if make and model.lower().startswith(make.split()[0].lower()):
        name = model
    else:
        name = f"{make} {model}".strip()
    try:
        lens = clean(exif.get_ifd(ExifTags.IFD.Exif).get(TAG_LENS))
    except Exception:
        lens = ""
    return f"{name} · {lens}" if name and lens and lens.lower() not in name.lower() else name


def read_camera(path: Path) -> tuple[str, int]:
    """Lecture rapide du modèle d'appareil et de l'orientation EXIF."""
    from . import media
    kind = media.kind_of(path)
    if kind == "video":
        return "", 1
    if kind == "raw":
        info = media.raw_metadata(path)
        return info["camera"], info["orientation"]
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            return camera_name(exif), int((exif or {}).get(TAG_ORIENTATION, 1) or 1)
    except Exception:
        return "", 1
