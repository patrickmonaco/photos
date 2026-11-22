"""Fichiers de directives propres à l'application (.phototheque.json, un par dossier).

Structure :
{
  "version": 1,
  "photos": {
    "IMG_0001.JPG": {
      "ops": [...],          # présent = l'utilisateur a pris la main sur les retouches
      "tags": ["mer", ...],  # tags ajoutés par l'utilisateur
      "removed_tags": [...], # tags hérités (Picasa/IPTC) masqués par l'utilisateur
      "star": true,
      "caption": "..."
    }
  }
}
Les photos ne sont jamais modifiées.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .config import SIDECAR_NAME

_lock = threading.RLock()


class SidecarStore:
    def __init__(self, fallback_dir: Path):
        self.fallback_dir = fallback_dir

    def _fallback_path(self, folder: Path) -> Path:
        digest = hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()[:16]
        return self.fallback_dir / digest / SIDECAR_NAME

    def path_for(self, folder: Path) -> Path:
        primary = folder / SIDECAR_NAME
        if primary.exists() or os.access(folder, os.W_OK):
            return primary
        return self._fallback_path(folder)

    def load(self, folder: Path) -> dict:
        for p in (folder / SIDECAR_NAME, self._fallback_path(folder)):
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    data.setdefault("photos", {})
                    return data
                except (OSError, json.JSONDecodeError):
                    continue
        return {"version": 1, "photos": {}}

    def update_photo(self, folder: Path, filename: str, **changes) -> dict:
        """Met à jour l'entrée d'une photo. Une valeur None supprime la clé."""
        with _lock:
            data = self.load(folder)
            entry = data["photos"].setdefault(filename, {})
            for key, value in changes.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            if not entry:
                data["photos"].pop(filename, None)
            self._write(folder, data)
            return entry

    def update_folder(self, folder: Path, updater) -> dict:
        """Modifie plusieurs photos d'un dossier en une seule écriture.
        updater(photos: dict) modifie le dictionnaire {fichier: entrée} en place."""
        with _lock:
            data = self.load(folder)
            updater(data["photos"])
            data["photos"] = {k: v for k, v in data["photos"].items() if v}
            self._write(folder, data)
            return data

    def _write(self, folder: Path, data: dict) -> None:
        target = self.path_for(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        if os.name == "nt" and target.parent == folder:
            # Fichier caché sous Windows, comme .picasa.ini
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x02)
            except Exception:
                pass
