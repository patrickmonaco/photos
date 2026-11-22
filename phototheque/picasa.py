"""Lecture des fichiers .picasa.ini (lecture seule : on ne les modifie jamais).

Format rétro-conçu (Picasa 3.x) :
  [Picasa]                      métadonnées du dossier
  [Contacts2]                   id=Nom;;  (contacts locaux, noms des visages)
  [.album:<jeton>]              name=, token=, date=, description=
  [<nom de fichier>]            star=yes, caption=, keywords=, rotate=rotate(n),
                                filters=crop64=1,<hex>;tilt=1,<angle>,<échelle>;...
                                faces=rect64(<hex>),<id>;...   albums=<jeton>,<jeton>
"""
from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")

# Filtres Picasa repris dans notre modèle d'opérations (sans paramètre).
SIMPLE_FILTERS = {
    "bw": "bw",
    "sepia": "sepia",
    "enhance": "enhance",
    "autolight": "autocontrast",
    "autocolor": "autocolor",
    "unsharp2": "sharpen",
    "warm": "warm",
}


@dataclass
class PicasaPhoto:
    filename: str
    star: bool = False
    caption: str | None = None
    keywords: list[str] = field(default_factory=list)
    ops: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    faces: list[dict] = field(default_factory=list)
    albums: list[str] = field(default_factory=list)


@dataclass
class PicasaAlbum:
    token: str
    name: str
    date: str | None = None
    description: str | None = None


def norm_name(name: str) -> str:
    """Clé de correspondance fichier <-> section : sans casse, Unicode NFC (Windows, macOS, Linux)."""
    return unicodedata.normalize("NFC", name).casefold()


@dataclass
class PicasaFolder:
    path: Path
    photos: dict[str, PicasaPhoto] = field(default_factory=dict)
    albums: dict[str, PicasaAlbum] = field(default_factory=dict)
    contacts: dict[str, str] = field(default_factory=dict)
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    def entry(self, filename: str) -> PicasaPhoto | None:
        """Section Picasa d'un fichier, même si la casse ou l'encodage du nom diffère."""
        if filename in self.photos:
            return self.photos[filename]
        if not self._index:
            self._index = {norm_name(k): v for k, v in self.photos.items()}
        return self._index.get(norm_name(filename))

    def unmatched(self, filenames: list[str]) -> list[str]:
        """Sections Picasa sans fichier correspondant (diagnostic)."""
        present = {norm_name(f) for f in filenames}
        return [k for k in self.photos if norm_name(k) not in present]


def find_ini(folder: Path) -> Path | None:
    for name in INI_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_sections(text: str) -> list[tuple[str, dict[str, str]]]:
    """Parseur tolérant (configparser refuse les doublons et certains noms de fichiers)."""
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {}
            sections.append((line[1:-1], current))
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()
    return sections


def rect64(hexstr: str) -> dict[str, float]:
    """Rectangle 64 bits -> fractions (gauche, haut, droite, bas)."""
    h = hexstr.strip().lower().zfill(16)[-16:]
    vals = [int(h[i:i + 4], 16) / 65536.0 for i in range(0, 16, 4)]
    return {"l": vals[0], "t": vals[1], "r": vals[2], "b": vals[3]}


def parse_filters(value: str, tilt_factor: float) -> tuple[list[dict], list[str]]:
    ops: list[dict] = []
    unsupported: list[str] = []
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, _, params = entry.partition("=")
        name = name.strip().lower()
        args = [a.strip() for a in params.split(",")] if params else []
        # args[0] vaut "1" quand le filtre est actif
        if args and args[0] == "0":
            continue
        if name == "crop64" and len(args) >= 2:
            ops.append({"op": "crop", **rect64(args[1])})
        elif name == "tilt" and len(args) >= 2:
            try:
                raw = float(args[1])
            except ValueError:
                unsupported.append(entry)
                continue
            if raw != 0:
                # Convention Picasa inverse de la nôtre : une valeur négative correspond à un
                # redressement positif (sens horaire) ici.
                ops.append({"op": "straighten", "deg": round(-raw * tilt_factor, 4), "picasa_tilt": raw})
        elif name in SIMPLE_FILTERS:
            ops.append({"op": SIMPLE_FILTERS[name]})
        else:
            unsupported.append(name)
    return ops, unsupported


_ROTATE_RE = re.compile(r"rotate\((\d+)\)")
_FACE_RE = re.compile(r"rect64\(([0-9a-fA-F]+)\)\s*,\s*([0-9a-fA-F]+)")


def parse_folder(folder: Path, tilt_factor: float = 10.0, rotate_clockwise: bool = True) -> PicasaFolder | None:
    ini = find_ini(folder)
    if not ini:
        return None
    result = PicasaFolder(path=folder)
    for name, kv in parse_sections(_read_text(ini)):
        low = name.lower()
        if low == "contacts2":
            for cid, val in kv.items():
                person = val.split(";")[0].strip()
                if person:
                    result.contacts[cid] = person
            continue
        if low.startswith(".album:"):
            token = kv.get("token") or name.split(":", 1)[1]
            result.albums[token] = PicasaAlbum(
                token=token,
                name=kv.get("name") or token,
                date=kv.get("date"),
                description=kv.get("description"),
            )
            continue
        if low in ("picasa", "contacts", "encoding"):
            continue
        photo = PicasaPhoto(filename=name)
        photo.star = kv.get("star", "").lower() == "yes"
        photo.caption = kv.get("caption") or None
        if kv.get("keywords"):
            photo.keywords = [k.strip() for k in kv["keywords"].split(",") if k.strip()]
        m = _ROTATE_RE.search(kv.get("rotate", ""))
        if m:
            n = int(m.group(1)) % 4
            if not rotate_clockwise:
                n = (4 - n) % 4
            if n:
                photo.ops.append({"op": "rotate90", "n": n})
        if kv.get("filters"):
            ops, unsupported = parse_filters(kv["filters"], tilt_factor)
            photo.ops.extend(ops)
            photo.unsupported = unsupported
        if kv.get("faces"):
            for hexrect, cid in _FACE_RE.findall(kv["faces"]):
                photo.faces.append({**rect64(hexrect), "contact": cid})
        if kv.get("albums"):
            photo.albums = [a.strip() for a in kv["albums"].split(",") if a.strip()]
        result.photos[name] = photo
    # Noms des personnes pour les visages
    for photo in result.photos.values():
        for f in photo.faces:
            f["person"] = result.contacts.get(f["contact"])
    return result


_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


def parse_folder_cached(folder: Path, tilt_factor: float = 10.0, rotate_clockwise: bool = True) -> PicasaFolder | None:
    """Comme parse_folder, avec un cache invalidé par la date de modification du .picasa.ini."""
    ini = find_ini(folder)
    if not ini:
        return None
    key = (str(ini), ini.stat().st_mtime_ns, tilt_factor, rotate_clockwise)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = parse_folder(folder, tilt_factor, rotate_clockwise)
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return result
