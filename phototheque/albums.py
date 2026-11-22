"""Albums (dossiers virtuels), stockés dans albums.json du dossier de données.

Les albums Picasa sont lus dans les .picasa.ini. Dès qu'on modifie un album Picasa
(renommage, ajout ou retrait de photos), une copie complète est écrite ici et prend
le dessus sur la version Picasa. Un album Picasa supprimé est mémorisé dans
"deleted_picasa" pour ne pas réapparaître au scan suivant.
"""
from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path

_lock = threading.RLock()


class AlbumStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            data = {}
        data.setdefault("version", 1)
        data.setdefault("albums", [])
        data.setdefault("deleted_picasa", [])
        return data

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def new_id() -> str:
        return "u" + secrets.token_hex(8)

    def upsert(self, album: dict) -> dict:
        """album = {id, name, description, date, source, photos: [{root, relpath}]}"""
        with _lock:
            data = self.load()
            data["albums"] = [a for a in data["albums"] if a["id"] != album["id"]]
            data["albums"].append(album)
            self.save(data)
            return album

    def delete(self, album_id: str, is_picasa: bool) -> None:
        with _lock:
            data = self.load()
            data["albums"] = [a for a in data["albums"] if a["id"] != album_id]
            if is_picasa and album_id not in data["deleted_picasa"]:
                data["deleted_picasa"].append(album_id)
            self.save(data)
