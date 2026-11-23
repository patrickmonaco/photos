"""Ouverture unifiée des fichiers : images (Pillow), HEIC (pillow-heif), RAW (rawpy) et vidéos (ffmpeg).

- open_image(path, size)  : une image PIL orientée (pour les vidéos : une image extraite du film)
- probe(path)             : informations d'une vidéo (durée, date, codec, dimensions, rotation)
- browser_playable(info)  : la vidéo peut-elle être lue telle quelle par un navigateur ?
- transcode(src, dst)     : conversion en MP4 H.264 lisible partout (vieux AVI, HEVC...)
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("phototheque.media")

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm"}
RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw"}
HEIF_EXTENSIONS = {".heic", ".heif"}

try:  # HEIC : Pillow sait alors ouvrir ces fichiers directement (EXIF compris)
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False


def kind_of(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in RAW_EXTENSIONS:
        return "raw"
    return "image"


# --------------------------------------------------------------------------- ffmpeg
@lru_cache(maxsize=1)
def ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # binaire fourni par le paquet imageio-ffmpeg (pratique sous Windows)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@lru_cache(maxsize=1)
def ffprobe_exe() -> str | None:
    return shutil.which("ffprobe")


def _run(args: list[str], timeout: float = 120, binary: bool = False):
    flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0  # pas de console sous Windows
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags,
                          text=not binary)


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    v = " ".join(v.split())
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%a %b %d %H:%M:%S %Y",    # IDIT des vieux AVI : "SAT AUG 05 16:30:00 2006"
                "%Y:%m:%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(v, fmt)
            if d.tzinfo:  # heure locale de la machine
                d = d.astimezone().replace(tzinfo=None)
            return d.isoformat(timespec="seconds") if d.year > 1970 else None
        except ValueError:
            continue
    return None


def probe(path: Path) -> dict:
    """Durée (s), date de création, codecs, dimensions affichées et rotation d'une vidéo."""
    info = {"duration": None, "created": None, "codec": None, "audio": None, "width": None, "height": None,
            "rotation": 0, "container": Path(path).suffix.lower().lstrip(".")}
    fp = ffprobe_exe()
    if fp:
        r = _run([fp, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        fmt = data.get("format", {})
        info["duration"] = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        info["created"] = next((d for d in (_parse_date(tags.get(k)) for k in
                                ("com.apple.quicktime.creationdate", "creation_time", "idit", "date", "icrd"))
                                if d), None)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and info["codec"] is None and s.get("disposition", {}).get("attached_pic") != 1:
                info["codec"] = s.get("codec_name")
                info["width"], info["height"] = s.get("width"), s.get("height")
                rot = (s.get("tags") or {}).get("rotate")
                if rot is None:
                    for sd in s.get("side_data_list", []) or []:
                        if "rotation" in sd:
                            rot = -float(sd["rotation"])  # matrice d'affichage : sens trigonométrique
                info["rotation"] = int(round(float(rot or 0))) % 360
                info["created"] = info["created"] or _parse_date((s.get("tags") or {}).get("creation_time"))
            elif s.get("codec_type") == "audio" and info["audio"] is None:
                info["audio"] = s.get("codec_name")
    elif ffmpeg_exe():  # sans ffprobe : lecture de la sortie de « ffmpeg -i »
        err = _run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], 60).stderr or ""
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        info["created"] = None
        for key in ("com.apple.quicktime.creationdate", "creation_time", "IDIT", "date", "ICRD"):
            m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", err, re.M | re.I)
            if m and (d := _parse_date(m.group(1))):
                info["created"] = d
                break
        m = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", err)
        if m:
            info["codec"], info["width"], info["height"] = m.group(1), int(m.group(2)), int(m.group(3))
        m = re.search(r"Audio: (\w+)", err)
        info["audio"] = m.group(1) if m else None
        m = re.search(r"rotate\s*:\s*(-?\d+)", err) or re.search(r"rotation of (-?[\d.]+) degrees", err)
        if m:
            v = float(m.group(1))
            info["rotation"] = int(round(v if "rotate" in m.group(0) else -v)) % 360
    if info["rotation"] in (90, 270) and info["width"]:
        info["width"], info["height"] = info["height"], info["width"]
    return info


def video_frame(path: Path, at: float | None = None, duration: float | None = None) -> Image.Image:
    """Une image du film (vers 10 % de la durée, au plus 1 s), déjà tournée selon la vidéo."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg introuvable")
    if at is None:
        at = min(1.0, (duration or 0) * 0.1)
    r = _run([exe, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
              "-f", "image2pipe", "-vcodec", "png", "-"], 60, binary=True)
    if not r.stdout and at:
        r = _run([exe, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
                  "-f", "image2pipe", "-vcodec", "png", "-"], 60, binary=True)
    if not r.stdout:
        raise RuntimeError(f"aucune image extraite de {path}")
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


PLAYABLE_CONTAINERS = {"mp4", "m4v", "mov", "webm"}
PLAYABLE_VIDEO = {"h264", "vp8", "vp9", "av1"}
PLAYABLE_AUDIO = {None, "aac", "mp3", "opus", "vorbis"}


def browser_playable(info: dict) -> bool:
    """Les navigateurs lisent le H.264, le VP8/VP9 et l'AV1 dans du MP4/MOV/WebM.
    Les vieux AVI (MJPEG, DivX, Xvid) et le HEVC (souvent illisible sous Linux) sont convertis."""
    return (info.get("container") in PLAYABLE_CONTAINERS and info.get("codec") in PLAYABLE_VIDEO
            and info.get("audio") in PLAYABLE_AUDIO)


def transcode(src: Path, dst: Path, max_width: int = 1280) -> None:
    """Conversion en MP4 H.264/AAC, lisible par tous les navigateurs, avec avance rapide."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg introuvable")
    tmp = dst.with_suffix(".part.mp4")
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = _run([exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
              "-vf", f"scale='min({max_width},iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)],
             timeout=3600)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"conversion impossible : {(r.stderr or '')[-300:]}")
    tmp.replace(dst)


# --------------------------------------------------------------------------- RAW
RAW_FLIP = {3: Image.ROTATE_180, 5: Image.ROTATE_90, 6: Image.ROTATE_270}


def open_raw(path: Path, size: int = 0) -> Image.Image:
    """Miniatures et aperçus : la vignette JPEG intégrée au RAW (rapide).
    Pleine résolution : développement complet avec la balance des blancs de l'appareil."""
    import rawpy
    with rawpy.imread(str(path)) as raw:
        if size and size <= 1600:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = ImageOps.exif_transpose(Image.open(io.BytesIO(thumb.data)))
                else:
                    img = Image.fromarray(thumb.data)
                flip = raw.sizes.flip
                if flip in RAW_FLIP and img.width > img.height:  # vignette non tournée
                    img = img.transpose(RAW_FLIP[flip])
                if max(img.size) >= min(size, 1200) * 0.6:
                    return img.convert("RGB")
            except Exception:
                pass
        rgb = raw.postprocess(use_camera_wb=True, half_size=bool(size and size <= 2000), output_bps=8)
    return Image.fromarray(rgb)


def raw_metadata(path: Path) -> dict:
    """Date, appareil, focale et orientation d'un RAW (lecture des balises EXIF/TIFF avec exifread)."""
    out = {"taken_at": None, "camera": "", "orientation": 1, "f35": None, "width": None, "height": None}
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        dt = str(tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime") or "")
        try:
            out["taken_at"] = datetime.strptime(dt[:19], "%Y:%m:%d %H:%M:%S").isoformat(timespec="seconds")
        except ValueError:
            pass
        make, model = str(tags.get("Image Make") or "").strip(), str(tags.get("Image Model") or "").strip()
        out["camera"] = model if make and model.lower().startswith(make.split()[0].lower()) else f"{make} {model}".strip()
        if tags.get("EXIF FocalLengthIn35mmFilm"):
            out["f35"] = float(str(tags["EXIF FocalLengthIn35mmFilm"]).split("/")[0]) or None
    except Exception:
        pass
    try:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            w, h = raw.sizes.width, raw.sizes.height
            if raw.sizes.flip in (5, 6):
                w, h = h, w
            out["width"], out["height"] = w, h
            out["orientation"] = {0: 1, 3: 3, 5: 8, 6: 6}.get(raw.sizes.flip, 1)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- point d'entrée
def open_image(path: Path, size: int = 0, duration: float | None = None) -> Image.Image:
    """Image PIL orientée, quel que soit le type de fichier. size = côté visé (0 = pleine résolution)."""
    kind = kind_of(path)
    if kind == "video":
        return video_frame(path, duration=duration)
    if kind == "raw":
        return open_raw(path, size)
    with Image.open(path) as src:
        if size and src.format == "JPEG":
            w, h = src.size
            s = size / max(w, h)
            if s < 1:
                src.draft("RGB", (max(1, int(w * s)), max(1, int(h * s))))
        src.load()
        return ImageOps.exif_transpose(src).convert("RGB")
