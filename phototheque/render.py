"""Rendu à la volée : application des opérations non destructives, avec cache disque.

Opérations reconnues (appliquées dans l'ordre de la liste) :
  rotate90 {n}                 quarts de tour, sens horaire
  straighten {deg}             redressement (positif = sens horaire), zoom auto qui masque les coins
  crop {l,t,r,b}               recadrage en fractions de l'image courante
  perspective {pitch, roll, strength, f35?}   lignes fuyantes (voir perspective.py)
  autoenhance {wb, black, white, gamma, clahe, vibrance, strength}   amélioration automatique
  bw, sepia, enhance, autocontrast, autocolor, sharpen, warm   (repris des filtres Picasa)
"""
from __future__ import annotations

import io
import math
import os
import threading
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from . import enhance, perspective

Image.MAX_IMAGE_PIXELS = 400_000_000

SIZES = {"thumb": 360, "preview": 1600, "full": 0}


def straighten(img: Image.Image, deg: float) -> Image.Image:
    if abs(deg) < 1e-3:
        return img
    w, h = img.size
    rot = img.rotate(-deg, resample=Image.BICUBIC, expand=False)
    theta = math.radians(abs(deg))
    s = math.cos(theta) + math.sin(theta) * max(w, h) / min(w, h)
    cw, ch = w / s, h / s
    left, top = (w - cw) / 2, (h - ch) / 2
    return rot.crop((round(left), round(top), round(left + cw), round(top + ch)))


def crop(img: Image.Image, l: float, t: float, r: float, b: float) -> Image.Image:
    w, h = img.size
    l, r = sorted((max(0.0, min(1.0, l)), max(0.0, min(1.0, r))))
    t, b = sorted((max(0.0, min(1.0, t)), max(0.0, min(1.0, b))))
    if r - l < 0.01 or b - t < 0.01:
        return img
    return img.crop((round(l * w), round(t * h), round(r * w), round(b * h)))


def sepia(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    return ImageOps.colorize(g, black=(40, 26, 13), white=(255, 240, 192), mid=(162, 128, 101))


def warm(img: Image.Image) -> Image.Image:
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * 1.06)))
    b = b.point(lambda v: int(v * 0.92))
    return Image.merge("RGB", (r, g, b))


def apply_ops(img: Image.Image, ops: list[dict], default_f35: float = 28.0,
              photo_f35: float | None = None) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.mode == "L":
        img = img.convert("RGB")
    for op in ops or []:
        kind = op.get("op")
        if kind == "rotate90":
            n = int(op.get("n", 1)) % 4
            img = {0: img, 1: img.transpose(Image.ROTATE_270), 2: img.transpose(Image.ROTATE_180),
                   3: img.transpose(Image.ROTATE_90)}[n]
        elif kind == "straighten":
            img = straighten(img, float(op.get("deg", 0)))
        elif kind == "crop":
            img = crop(img, op["l"], op["t"], op["r"], op["b"])
        elif kind == "perspective":
            f35 = float(op.get("f35") or photo_f35 or default_f35)
            try:
                img = perspective.warp(img, f35, float(op.get("pitch", 0)), float(op.get("roll", 0)),
                                       float(op.get("strength", 1.0)))
            except ValueError:
                pass
        elif kind == "autoenhance":
            # Sans paramètres (repris du « J'ai de la chance » de Picasa) : analyse au moment du rendu.
            params = op if "wb" in op else enhance.analyze(img)
            img = enhance.apply(img, params, float(op.get("strength", 1.0)))
        elif kind == "bw":
            img = ImageOps.grayscale(img).convert("RGB")
        elif kind == "sepia":
            img = sepia(img)
        elif kind == "enhance":
            img = ImageOps.autocontrast(img, cutoff=0.5, preserve_tone=True)
            img = ImageEnhance.Color(img).enhance(1.1)
        elif kind == "autocontrast":
            img = ImageOps.autocontrast(img, cutoff=0.5, preserve_tone=True)
        elif kind == "autocolor":
            img = ImageOps.autocontrast(img, cutoff=0.5)
        elif kind == "sharpen":
            img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
        elif kind == "warm":
            img = warm(img)
    return img


def map_box(box, ops: list[dict], w: float, h: float):
    """Position d'un rectangle (coordonnées relatives à la photo d'origine, orientée) dans l'image
    rendue avec ses retouches. Renvoie (l, t, r, b) relatifs à l'image rendue, ou None si une
    opération ne permet pas ce calcul (perspective)."""
    pts = [(box[0] * w, box[1] * h), (box[2] * w, box[1] * h), (box[2] * w, box[3] * h), (box[0] * w, box[3] * h)]
    for op in ops or []:
        kind = op.get("op")
        if kind == "rotate90":
            n = int(op.get("n", 1)) % 4
            for _ in range(n):              # quart de tour horaire : (x, y) -> (h - y, x)
                pts = [(h - y, x) for x, y in pts]
                w, h = h, w
        elif kind == "straighten":
            deg = float(op.get("deg", 0))
            if abs(deg) < 1e-3:
                continue
            th = math.radians(deg)
            cx, cy = w / 2, h / 2
            c, sn = math.cos(th), math.sin(th)
            # image tournée de deg dans le sens horaire (rotate(-deg)), puis recadrée au centre
            pts = [(cx + (x - cx) * c - (y - cy) * sn, cy + (x - cx) * sn + (y - cy) * c) for x, y in pts]
            k = math.cos(math.radians(abs(deg))) + math.sin(math.radians(abs(deg))) * max(w, h) / min(w, h)
            cw, ch = w / k, h / k
            left, top = (w - cw) / 2, (h - ch) / 2
            pts = [(x - left, y - top) for x, y in pts]
            w, h = cw, ch
        elif kind == "crop":
            l, r = sorted((max(0.0, min(1.0, op["l"])), max(0.0, min(1.0, op["r"]))))
            t, b = sorted((max(0.0, min(1.0, op["t"])), max(0.0, min(1.0, op["b"]))))
            if r - l < 0.01 or b - t < 0.01:
                continue
            pts = [(x - l * w, y - t * h) for x, y in pts]
            w, h = (r - l) * w, (b - t) * h
        elif kind == "perspective":
            return None
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    return (min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h)


def render(path: Path, ops: list[dict], size: int, default_f35: float = 28.0,
           photo_f35: float | None = None, duration: float | None = None) -> Image.Image:
    # Ouverture adaptée au type de fichier (JPEG décodé en taille réduite, HEIC, RAW, image d'une vidéo).
    # La marge tient compte des opérations qui agrandissent une partie de l'image (recadrage...).
    from . import media
    want = int(size * _zoom_margin(ops)) if size else 0
    img = apply_ops(media.open_image(path, want, duration), ops, default_f35, photo_f35)
    if size:
        img.thumbnail((size, size), Image.LANCZOS, reducing_gap=2.0)
    return img


def _zoom_margin(ops: list[dict]) -> float:
    """Marge de résolution à décoder quand des opérations agrandissent une partie de l'image."""
    margin = 1.0
    for op in ops or []:
        kind = op.get("op")
        if kind == "crop":
            frac = min(abs(op["r"] - op["l"]), abs(op["b"] - op["t"])) or 1
            margin /= max(frac, 0.05)
        elif kind in ("straighten", "perspective"):
            margin *= 1.3
    return min(margin, 8.0)


def to_jpeg(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class RenderCache:
    """Cache disque des rendus, avec purge.

    - miniatures et aperçus : <cache>/<pid % 256>/<pid>_<hash>_<taille>.jpg
    - rendus pleine résolution (zoom) : <cache>/full/<pid>_<hash>.jpg, limités aux FULL_KEEP derniers
    - au-delà de la taille maximale, les fichiers les moins récemment vus sont supprimés :
      aperçus d'abord, miniatures ensuite (elles coûtent plus cher à régénérer en masse).
    """
    FULL_KEEP = 30
    TOUCH_AFTER = 86400  # la date d'accès n'est rafraîchie qu'une fois par jour (évite les écritures)

    def __init__(self, cache_dir: Path, max_bytes: int = 10 * 1024 ** 3):
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes
        self.full_dir = cache_dir / "full"
        self._lock = threading.Lock()
        self.last_purge: dict = {}

    def path(self, pid: int, ops_hash: str, size: int) -> Path:
        if size == 0:
            return self.full_dir / f"{pid}_{ops_hash}.jpg"
        return self.cache_dir / f"{pid % 256:02x}" / f"{pid}_{ops_hash}_{size}.jpg"

    _locks: dict = {}
    _locks_guard = threading.Lock()

    def _hit(self, p: Path) -> bool:
        try:
            st = p.stat()
        except FileNotFoundError:
            return False
        if time.time() - st.st_mtime > self.TOUCH_AFTER:
            os.utime(p)  # « vu récemment » pour la purge
        return True

    def get_or_render(self, pid: int, ops_hash: str, size: int, producer) -> Path:
        p = self.path(pid, ops_hash, size)
        if self._hit(p):
            return p
        # Un seul calcul par image : une demande identique (préchargement puis affichage, par
        # exemple) attend la première au lieu de refaire le travail.
        with self._locks_guard:
            lock = self._locks.setdefault(str(p), threading.Lock())
        with lock:
            try:
                if self._hit(p):
                    return p
                return self._produce(p, pid, size, producer)
            finally:
                with self._locks_guard:
                    self._locks.pop(str(p), None)

    def _produce(self, p: Path, pid: int, size: int, producer) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        pattern = f"{pid}_*.jpg" if size == 0 else f"{pid}_*_{size}.jpg"
        for old in p.parent.glob(pattern):
            old.unlink(missing_ok=True)
        data = to_jpeg(producer(), quality=82 if size and size <= 400 else 90)
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        if size == 0:
            self._prune_full()
        return p

    def _prune_full(self) -> None:
        files = sorted(self.full_dir.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[self.FULL_KEEP:]:
            f.unlink(missing_ok=True)

    def usage(self) -> dict:
        """Taille du cache par catégorie (octets, nombre de fichiers)."""
        out = {"thumb": [0, 0], "preview": [0, 0], "full": [0, 0], "faces": [0, 0], "video": [0, 0]}
        for kind, path, st in self._walk():
            out[kind][0] += st.st_size
            out[kind][1] += 1
        total = sum(v[0] for v in out.values())
        return {"total": total, "max": self.max_bytes,
                **{k: {"bytes": v[0], "files": v[1]} for k, v in out.items()}}

    def _walk(self):
        if not self.cache_dir.exists():
            return
        for sub in os.scandir(self.cache_dir):
            if not sub.is_dir():
                continue
            kind_dir = {"full": "full", "faces": "faces", "video": "video"}.get(sub.name)
            for e in os.scandir(sub.path):
                if not e.name.endswith((".jpg", ".mp4")) or e.name.endswith(".part.mp4"):
                    continue
                st = e.stat()
                kind = kind_dir or ("thumb" if e.name.endswith(f"_{SIZES['thumb']}.jpg") else "preview")
                yield kind, e.path, st

    def purge(self, max_bytes: int | None = None) -> dict:
        """Ramène le cache sous 90 % de la taille maximale (aperçus d'abord, puis miniatures)."""
        limit = self.max_bytes if max_bytes is None else max_bytes
        with self._lock:
            t = time.time()
            files = [(kind, path, st.st_size, st.st_mtime) for kind, path, st in self._walk()]
            total = sum(f[2] for f in files)
            removed, freed = 0, 0
            if total > limit:
                target = limit * 0.9
                order = {"full": 0, "video": 1, "preview": 2, "faces": 3, "thumb": 4}
                for kind, path, size, _ in sorted(files, key=lambda f: (order[f[0]], f[3])):
                    if total - freed <= target:
                        break
                    try:
                        os.unlink(path)
                        freed += size
                        removed += 1
                    except OSError:
                        pass
            self.last_purge = {"at": t, "before": total, "freed": freed, "removed": removed,
                               "seconds": round(time.time() - t, 2)}
            return self.last_purge

    def clear(self, what: str = "previews") -> int:
        """Vide une partie du cache : 'previews' (aperçus + zoom) ou 'all'."""
        n = 0
        for kind, path, _ in list(self._walk()):
            if what == "all" or kind in ("preview", "full", "video"):
                try:
                    os.unlink(path)
                    n += 1
                except OSError:
                    pass
        return n
