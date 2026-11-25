"""Service principal : indexation des racines, fusion Picasa + directives, tags, albums."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict
from pathlib import Path

from .albums import AlbumStore
from .config import MEDIA_EXTENSIONS, SIDECAR_NAME, Config
from .db import Database
from .metadata import date_parts, read_metadata
from .picasa import find_ini, parse_folder_cached as parse_folder
from .sidecar import SidecarStore

log = logging.getLogger("phototheque")

# À incrémenter quand l'interprétation des .picasa.ini change (force la refusion au scan).
PICASA_CONVENTION = 6

SKIP_DIRS = {".picasaoriginals", ".git", "@eadir", "$recycle.bin", ".trash", ".thumbnails"}


def ops_hash(ops: list | None) -> str:
    return hashlib.sha1(json.dumps(ops or [], sort_keys=True).encode()).hexdigest()[:12]


class Library:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.sidecars = SidecarStore(config.sidecar_fallback_dir)
        self.albums = AlbumStore(config.albums_path)
        self.scan_state = {"running": False, "done": 0, "folder": None, "error": None}
        self._scan_lock = threading.Lock()

    # ------------------------------------------------------------------ chemins
    def abs_path(self, row) -> Path:
        return Path(row["root"]) / row["relpath"]

    def folder_path(self, row) -> Path:
        return Path(row["root"]) / row["folder"] if row["folder"] else Path(row["root"])

    def photo(self, photo_id: int):
        return self.db.one("SELECT * FROM photo WHERE id=?", (photo_id,))

    # ------------------------------------------------------------------ scan
    def scan_all(self, deep: bool = False) -> dict:
        """Indexation de toutes les racines.

        Par défaut, un dossier dont la date de modification (ainsi que celles de son .picasa.ini et
        de ses directives) n'a pas changé n'est pas relu fichier par fichier : l'ajout, la
        suppression ou le renommage d'une photo modifient la date du dossier. deep=True vérifie
        chaque fichier (utile si des photos ont été réécrites sur place par un autre logiciel)."""
        if not self._scan_lock.acquire(blocking=False):
            return self.scan_state
        try:
            self.scan_state.update(running=True, done=0, error=None, skipped=0)
            self._deep = deep
            picasa_albums: dict[str, dict] = {}
            members: dict[str, list[tuple[str, str]]] = {}
            roots = self.config.settings.roots
            for root in roots:
                if Path(root).is_dir():
                    self._scan_root(Path(root), picasa_albums, members)
            # Racines retirées de la configuration
            if roots:
                marks = ",".join("?" * len(roots))
                self.db.execute(f"DELETE FROM photo WHERE root NOT IN ({marks})", tuple(roots))
            else:
                self.db.execute("DELETE FROM photo")
            self._sync_albums(picasa_albums, members)
            self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")
        except Exception as exc:  # pragma: no cover
            log.exception("scan")
            self.scan_state["error"] = str(exc)
        finally:
            self.scan_state.update(running=False, folder=None)
            self._scan_lock.release()
        return self.scan_state

    def _excluded(self) -> set[str]:
        return {os.path.normcase(os.path.abspath(p)) for p in self.config.settings.excluded_folders}

    def is_excluded(self, path: Path, excluded: set[str] | None = None) -> bool:
        excluded = self._excluded() if excluded is None else excluded
        p = os.path.normcase(os.path.abspath(path))
        return any(p == e or p.startswith(e + os.sep) for e in excluded)

    def _scan_root(self, root: Path, picasa_albums: dict, members: dict) -> None:
        seen: set[str] = set()
        excluded = self._excluded()
        states = {r["folder"]: r["sig"] for r in self.db.query("SELECT folder, sig FROM folder_state WHERE root=?",
                                                               (str(root),))}
        visited: set[str] = set()
        settings_sig = self._settings_sig()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d.lower() not in SKIP_DIRS and not d.startswith(".")
                                 and not self.is_excluded(Path(dirpath) / d, excluded))
            folder = Path(dirpath)
            rel = folder.relative_to(root).as_posix()
            rel = "" if rel == "." else rel
            visited.add(rel)
            sig = self._folder_sig(folder, settings_sig)
            if not getattr(self, "_deep", False) and states.get(rel) == sig:
                self._skip_dir(root, folder, rel, picasa_albums, members, seen)
                continue
            self._scan_dir(root, folder, filenames, picasa_albums, members, seen)
            self.db.execute("INSERT OR REPLACE INTO folder_state(root, folder, sig) VALUES (?,?,?)",
                            (str(root), rel, sig))
        gone_dirs = [f for f in states if f not in visited]
        for i in range(0, len(gone_dirs), 500):
            chunk = gone_dirs[i:i + 500]
            self.db.execute(f"DELETE FROM folder_state WHERE root=? AND folder IN ({','.join('?' * len(chunk))})",
                            (str(root), *chunk))
        # Fichiers disparus (ou dans un dossier exclu)
        rows = self.db.query("SELECT id, relpath FROM photo WHERE root=?", (str(root),))
        gone = [r["id"] for r in rows if r["relpath"] not in seen]
        self._delete_ids(gone)

    def _settings_sig(self) -> str:
        s = self.config.settings
        # Les types de fichiers reconnus en font partie : après l'ajout des vidéos, HEIC et RAW (v0.8),
        # chaque dossier est relu une fois pour y trouver ces fichiers jusque-là ignorés.
        exts = ",".join(sorted(MEDIA_EXTENSIONS))
        return f"{s.picasa_tilt_deg_per_unit}|{s.picasa_rotate_clockwise}|{s.enhance_strength}|{PICASA_CONVENTION}|{exts}"

    @staticmethod
    def _folder_sig(folder: Path, settings_sig: str) -> str:
        def mt(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        ini = find_ini(folder)
        return f"{mt(folder)}|{mt(ini) if ini else 0}|{mt(folder / SIDECAR_NAME)}|{settings_sig}"

    def _skip_dir(self, root: Path, folder: Path, rel: str, picasa_albums: dict, members: dict,
                  seen: set[str]) -> None:
        """Dossier inchangé : on reprend ses photos depuis l'index, et ses albums Picasa depuis le .ini."""
        rows = self.db.query("SELECT relpath, filename FROM photo WHERE root=? AND folder=?", (str(root), rel))
        self.scan_state["done"] += len(rows)
        self.scan_state["skipped"] = self.scan_state.get("skipped", 0) + 1
        for r in rows:
            seen.add(r["relpath"])
        if not rows or not find_ini(folder):
            return
        s = self.config.settings
        pic = parse_folder(folder, s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        if not pic or not pic.albums:
            return
        for token, alb in pic.albums.items():
            picasa_albums.setdefault(token, {"id": token, "name": alb.name,
                                             "description": alb.description, "date": alb.date})
        for r in rows:
            pphoto = pic.entry(r["filename"])
            if pphoto:
                for token in pphoto.albums:
                    members.setdefault(token, []).append((str(root), r["relpath"]))

    def _delete_ids(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self.db.execute(f"DELETE FROM photo WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))

    def _scan_dir(self, root: Path, folder: Path, filenames: list[str], picasa_albums: dict,
                  members: dict, seen: set[str]) -> None:
        rel_folder = folder.relative_to(root).as_posix()
        rel_folder = "" if rel_folder == "." else rel_folder
        self.scan_state["folder"] = rel_folder or str(root)
        images = sorted(f for f in filenames if Path(f).suffix.lower() in MEDIA_EXTENSIONS)
        if not images:
            return
        s = self.config.settings
        pic = parse_folder(folder, s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        side = self.sidecars.load(folder)
        if pic:
            for token, alb in pic.albums.items():
                picasa_albums.setdefault(token, {"id": token, "name": alb.name,
                                                 "description": alb.description, "date": alb.date})
        # Lecture des fichiers (stat, EXIF) HORS verrou, puis écritures groupées en transactions courtes :
        # sur un disque lent ou un NAS, la base n'est jamais bloquée pendant les accès disque.
        existing = {r["relpath"]: r for r in self.db.query(
            "SELECT id, relpath, size, mtime, merge_sig, file_keywords, file_caption FROM photo "
            "WHERE root=? AND folder=?", (str(root), rel_folder))}
        for start in range(0, len(images), 100):
            batch = []
            for filename in images[start:start + 100]:
                relpath = f"{rel_folder}/{filename}" if rel_folder else filename
                seen.add(relpath)
                try:
                    st = (folder / filename).stat()
                except OSError:
                    continue
                row = existing.get(relpath)
                self.scan_state["done"] += 1
                pphoto = pic.entry(filename) if pic else None
                if pphoto:
                    for token in pphoto.albums:
                        members.setdefault(token, []).append((str(root), relpath))
                unchanged = row and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1e-3
                sentry = side["photos"].get(filename, {})
                if unchanged and row["merge_sig"] == self._merge_sig(pphoto, sentry, row["file_keywords"],
                                                                      row["file_caption"]):
                    continue  # rien n'a changé : ni lecture ni écriture
                meta = None if unchanged else read_metadata(folder / filename)
                batch.append((filename, relpath, st, row, pphoto, sentry, meta))
            if batch:
                with self.db.transaction():
                    for filename, relpath, st, row, pphoto, sentry, meta in batch:
                        self._write_file(root, rel_folder, filename, relpath, st, row, pphoto, sentry, meta)

    def root_of(self, path: Path) -> Path | None:
        p = os.path.normcase(os.path.abspath(path))
        for r in self.config.settings.roots:
            rn = os.path.normcase(os.path.abspath(r))
            if p == rn or p.startswith(rn + os.sep):
                return Path(r)
        return None

    def scan_folders(self, dirs) -> bool:
        """Rescan ciblé de quelques dossiers (surveillance des dossiers). False si un scan est en cours."""
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            data = self.albums.load()
            blocked = set(data["deleted_picasa"]) | {a["id"] for a in data["albums"]}
            for d in {Path(x) for x in dirs}:
                root = self.root_of(d)
                if root is None or self.is_excluded(d):
                    continue
                rel = d.relative_to(root).as_posix()
                rel = "" if rel == "." else rel
                if not d.is_dir():  # dossier supprimé ou renommé
                    rows = self.db.query("SELECT id FROM photo WHERE root=? AND (folder=? OR folder LIKE ?)",
                                         (str(root), rel, rel + "/%"))
                    self._delete_ids([r["id"] for r in rows])
                    continue
                picasa_albums, members, seen = {}, {}, set()
                names = [f for f in os.listdir(d) if (d / f).is_file()]
                self._scan_dir(root, d, names, picasa_albums, members, seen)
                self.db.execute("INSERT OR REPLACE INTO folder_state(root, folder, sig) VALUES (?,?,?)",
                                (str(root), rel, self._folder_sig(d, self._settings_sig())))
                rows = self.db.query("SELECT id, relpath FROM photo WHERE root=? AND folder=?", (str(root), rel))
                self._delete_ids([r["id"] for r in rows if r["relpath"] not in seen])
                # Albums Picasa de ce dossier
                with self.db.transaction():
                    for token, refs in members.items():
                        if token in blocked:
                            continue
                        if not self.db.one("SELECT 1 FROM album WHERE id=?", (token,)) and token in picasa_albums:
                            a = picasa_albums[token]
                            self.db.execute("INSERT INTO album(id,name,description,date,source) VALUES (?,?,?,?,'picasa')",
                                            (token, a["name"], a.get("description"), a.get("date")))
                        for r, p in refs:
                            row = self.db.one("SELECT id FROM photo WHERE root=? AND relpath=?", (r, p))
                            if row and self.db.one("SELECT 1 FROM album WHERE id=?", (token,)):
                                self.db.execute("INSERT OR IGNORE INTO album_photo(album_id, photo_id, position) "
                                                "VALUES (?,?,(SELECT COUNT(*) FROM album_photo WHERE album_id=?))",
                                                (token, row["id"], token))
            self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")
            return True
        finally:
            self._scan_lock.release()

    def _write_file(self, root: Path, rel_folder: str, filename: str, relpath: str, st, row,
                    pphoto, sentry: dict, meta: dict | None) -> int:
        """Écrit une photo dans l'index (métadonnées déjà lues si meta est fourni)."""
        if meta is None and row is not None:
            pid = row["id"]
        else:
            meta = meta or read_metadata(root / relpath)
            y, m, d = date_parts(meta["taken_at"])
            values = dict(root=str(root), relpath=relpath, folder=rel_folder, filename=filename,
                          size=st.st_size, mtime=st.st_mtime, width=meta["width"], height=meta["height"],
                          taken_at=meta["taken_at"], date_source=meta["date_source"],
                          year=y, month=m, day=d, f35=meta["f35"],
                          file_keywords=json.dumps(meta["keywords"], ensure_ascii=False),
                          file_caption=meta["caption"], camera=meta["camera"],
                          exif_orientation=meta["orientation"], media=meta["media"],
                          duration=meta["duration"], codec=meta["codec"])
            if row:
                pid = row["id"]
                sets = ",".join(f"{k}=:{k}" for k in values)
                self.db.execute(f"UPDATE photo SET {sets} WHERE id=:id", {**values, "id": pid})
            else:
                cols = ",".join(values)
                pid = self.db.execute(f"INSERT INTO photo({cols}) VALUES ({','.join(':' + k for k in values)})",
                                      values).lastrowid
        self._merge(pid, pphoto, sentry)
        return pid

    def _merge_sig(self, pphoto, sentry: dict, file_keywords, file_caption) -> str:
        s = self.config.settings
        payload = [asdict(pphoto) if pphoto else None, sentry, file_keywords, file_caption,
                   s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise, s.enhance_strength, PICASA_CONVENTION]
        return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    # Filtres de correction Picasa remplacés par « Améliorer » : J'ai de la chance, Contraste auto,
    # Couleur auto, Réchauffer. Netteté, Noir et blanc et Sépia sont conservés tels quels.
    PICASA_TO_AUTOENHANCE = {"enhance", "autocontrast", "autocolor", "warm"}

    def convert_picasa_ops(self, ops: list[dict]) -> list[dict]:
        """Remplace les corrections automatiques de Picasa par une seule opération « Améliorer »,
        à l'intensité par défaut des réglages (paramètres calculés au rendu : Picasa ne les fournit pas)."""
        out, auto = [], None
        for op in ops:
            if op.get("op") in self.PICASA_TO_AUTOENHANCE:
                if auto is None:
                    auto = {"op": "autoenhance", "strength": self.config.settings.enhance_strength,
                            "from_picasa": []}
                    out.append(auto)
                auto["from_picasa"].append(op["op"])
            else:
                out.append(op)
        return out

    def _merge(self, pid: int, pphoto, sentry: dict) -> None:
        """Fusion Picasa + directives de l'application -> état effectif dans l'index."""
        row = self.db.one("SELECT file_keywords, file_caption FROM photo WHERE id=?", (pid,))
        self.db.execute("UPDATE photo SET merge_sig=? WHERE id=?",
                        (self._merge_sig(pphoto, sentry, row["file_keywords"], row["file_caption"]), pid))
        if "ops" in sentry:
            ops, source = sentry["ops"], "user"
        elif pphoto and pphoto.ops:
            ops, source = self.convert_picasa_ops(pphoto.ops), "picasa"
        else:
            ops, source = [], None
        star = sentry["star"] if "star" in sentry else bool(pphoto and pphoto.star)
        caption = sentry.get("caption") or (pphoto.caption if pphoto else None) or row["file_caption"]
        unsupported = json.dumps(pphoto.unsupported) if pphoto and pphoto.unsupported else None
        self.db.execute(
            "UPDATE photo SET ops=?, ops_source=?, ops_hash=?, star=?, caption=?, picasa_unsupported=? WHERE id=?",
            (json.dumps(ops), source, ops_hash(ops), int(star), caption, unsupported, pid))

        removed = {t.lower() for t in sentry.get("removed_tags", [])}
        tags: dict[tuple[str, str], str] = {}
        for kw in json.loads(row["file_keywords"] or "[]"):
            tags[(kw, "keyword")] = "iptc"
        if pphoto:
            for kw in pphoto.keywords:
                tags[(kw, "keyword")] = "picasa"
            for f in pphoto.faces:
                if f.get("person"):
                    tags[(f["person"], "person")] = "picasa"
        for kw in sentry.get("tags", []):
            tags[(kw, "keyword")] = "user"
        for kw in sentry.get("people", []):
            tags[(kw, "person")] = "user"
        # Les tags issus de l'IA (auto, visages) sont conservés ; une source explicite les remplace.
        self.db.execute("DELETE FROM photo_tag WHERE photo_id=? AND source NOT IN ('auto', 'face')", (pid,))
        for (name, kind), src in tags.items():
            if name.lower() in removed:
                continue
            self.db.execute("INSERT OR REPLACE INTO photo_tag(photo_id, tag_id, source) VALUES (?,?,?)",
                            (pid, self.db.tag_id(name, kind), src))
        if removed:
            marks = ",".join("?" * len(removed))
            self.db.execute(f"DELETE FROM photo_tag WHERE photo_id=? AND source IN ('auto', 'face') AND tag_id IN "
                            f"(SELECT id FROM tag WHERE lower(name) IN ({marks}))", (pid, *removed))
        self.db.execute("DELETE FROM face WHERE photo_id=? AND source='picasa'", (pid,))
        if pphoto:
            for f in pphoto.faces:
                self.db.execute("INSERT INTO face(photo_id,l,t,r,b,person,source) VALUES (?,?,?,?,?,?,'picasa')",
                                (pid, f["l"], f["t"], f["r"], f["b"], f.get("person")))

    def refresh_photo(self, pid: int) -> None:
        """Recalcule l'état effectif d'une photo après une modification de ses directives."""
        row = self.photo(pid)
        folder = self.folder_path(row)
        s = self.config.settings
        pic = parse_folder(folder, s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        pphoto = pic.entry(row["filename"]) if pic else None
        sentry = self.sidecars.load(folder)["photos"].get(row["filename"], {})
        with self.db.transaction():
            self._merge(pid, pphoto, sentry)
            self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")

    def picasa_ops(self, pid: int) -> list[dict]:
        row = self.photo(pid)
        s = self.config.settings
        pic = parse_folder(self.folder_path(row), s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        p = pic.entry(row["filename"]) if pic else None
        return self.convert_picasa_ops(p.ops) if p else []

    def perspective_suggestion(self, pid: int) -> dict:
        """Détecte les lignes fuyantes et propose une nouvelle liste d'opérations."""
        from . import media, perspective
        from .render import apply_ops
        row = self.photo(pid)
        if row["media"] == "video":
            return {"found": False}
        ops = json.loads(row["ops"] or "[]")
        head = [o for o in ops if o.get("op") == "rotate90"]
        rest = [o for o in ops if o.get("op") not in ("rotate90", "perspective", "straighten")]
        s = self.config.settings
        f35 = row["f35"] or s.default_f35
        img = apply_ops(media.open_image(self.abs_path(row), 1400), head)
        est = perspective.estimate(img, f35)
        if not est:
            return {"found": False}
        op = {"op": "perspective", "pitch": est["pitch"], "roll": est["roll"], "strength": s.perspective_strength}
        if not row["f35"]:
            op["f35"] = f35
        return {"found": True, "estimate": est, "f35_from_exif": bool(row["f35"]), "ops": head + [op] + rest}

    def enhance_suggestion(self, pid: int) -> dict:
        """Analyse la photo (avec ses autres retouches) et propose l'opération « autoenhance »."""
        from . import enhance
        from .render import render
        row = self.photo(pid)
        base = [o for o in json.loads(row["ops"] or "[]") if o.get("op") != "autoenhance"]
        img = render(self.abs_path(row), base, 800, self.config.settings.default_f35, row["f35"], row["duration"])
        op = {"op": "autoenhance", **enhance.analyze(img), "strength": self.config.settings.enhance_strength}
        return {"params": op, "ops": base + [op]}

    # ------------------------------------------------------------------ directives
    def set_ops(self, pid: int, ops: list[dict] | None) -> None:
        """ops=None : revenir aux retouches Picasa (ou aucune)."""
        row = self.photo(pid)
        self.sidecars.update_photo(self.folder_path(row), row["filename"], ops=ops)
        self.refresh_photo(pid)

    def set_star(self, pid: int, star: bool) -> None:
        row = self.photo(pid)
        self.sidecars.update_photo(self.folder_path(row), row["filename"], star=star)
        self.refresh_photo(pid)

    def set_caption(self, pid: int, caption: str | None) -> None:
        row = self.photo(pid)
        self.sidecars.update_photo(self.folder_path(row), row["filename"], caption=caption or None)
        self.refresh_photo(pid)

    def rows_for(self, ids: list[int]) -> list:
        out = []
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            out += self.db.query(f"SELECT * FROM photo WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
        return out

    def apply_enhance_strength(self, strength: float, progress=None) -> dict:
        """Nouvelle intensité d'amélioration, appliquée aussi aux photos déjà améliorées : celles
        améliorées à la main (directive modifiée dans .phototheque.json) et celles dont l'amélioration
        vient d'un filtre Picasa (recalculée avec le nouveau réglage). Les photos ne sont pas touchées ;
        leurs vignettes sont recalculées à l'affichage."""
        strength = round(max(0.0, min(1.5, float(strength))), 3)
        self.config.settings.enhance_strength = strength
        self.config.save()
        ids = [r["id"] for r in self.db.query("SELECT id FROM photo WHERE ops LIKE '%\"autoenhance\"%'")]
        groups = self._by_folder(ids)
        done = 0
        for (root, folder), rows in groups.items():
            def update(photos, rows=rows):
                for row in rows:
                    entry = photos.get(row["filename"])
                    if not entry or not isinstance(entry.get("ops"), list):
                        continue
                    for op in entry["ops"]:
                        if isinstance(op, dict) and op.get("op") == "autoenhance":
                            op["strength"] = strength
            data = self.sidecars.update_folder(self.folder_path(rows[0]), update)
            self._refresh_rows(rows, data["photos"])
            done += len(rows)
            if progress:
                progress(done, len(ids))
        return {"photos": len(ids), "strength": strength}

    def _by_folder(self, ids: list[int]) -> dict:
        groups: dict = {}
        for row in self.rows_for(ids):
            groups.setdefault((row["root"], row["folder"]), []).append(row)
        return groups

    def _refresh_rows(self, rows: list, photos: dict) -> None:
        """Refusion de plusieurs photos d'un même dossier (une lecture du .picasa.ini)."""
        folder = self.folder_path(rows[0])
        s = self.config.settings
        pic = parse_folder(folder, s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        with self.db.transaction():
            for row in rows:
                self._merge(row["id"], pic.entry(row["filename"]) if pic else None,
                            photos.get(row["filename"], {}))

    def add_tag_bulk(self, ids: list[int], name: str, kind: str = "keyword") -> None:
        key = "people" if kind == "person" else "tags"
        low = name.lower()
        for (root, folder), rows in self._by_folder(ids).items():
            def update(photos, rows=rows):
                for row in rows:
                    entry = photos.setdefault(row["filename"], {})
                    values = list(entry.get(key, []))
                    if low not in {v.lower() for v in values}:
                        values.append(name)
                    entry[key] = values
                    removed = [t for t in entry.get("removed_tags", []) if t.lower() != low]
                    if removed:
                        entry["removed_tags"] = removed
                    else:
                        entry.pop("removed_tags", None)
            data = self.sidecars.update_folder(self.folder_path(rows[0]), update)
            self._refresh_rows(rows, data["photos"])
        self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")

    def remove_tag_bulk(self, ids: list[int], name: str) -> None:
        low = name.lower()
        for (root, folder), rows in self._by_folder(ids).items():
            pids = [r["id"] for r in rows]
            inherited = {r["photo_id"] for r in self.db.query(
                "SELECT pt.photo_id FROM photo_tag pt JOIN tag t ON t.id=pt.tag_id "
                f"WHERE lower(t.name)=? AND pt.source!='user' AND pt.photo_id IN ({','.join('?' * len(pids))})",
                (low, *pids))}

            def update(photos, rows=rows, inherited=inherited):
                for row in rows:
                    entry = photos.setdefault(row["filename"], {})
                    for key in ("tags", "people"):
                        vals = [t for t in entry.get(key, []) if t.lower() != low]
                        if vals:
                            entry[key] = vals
                        else:
                            entry.pop(key, None)
                    removed = list(entry.get("removed_tags", []))
                    if row["id"] in inherited and low not in {r.lower() for r in removed}:
                        removed.append(name)
                    if removed:
                        entry["removed_tags"] = removed
            data = self.sidecars.update_folder(self.folder_path(rows[0]), update)
            self._refresh_rows(rows, data["photos"])
        self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")

    def add_tag(self, pid: int, name: str, kind: str = "keyword") -> None:
        self.add_tag_bulk([pid], name, kind)

    def remove_tag(self, pid: int, name: str) -> None:
        self.remove_tag_bulk([pid], name)

    # ------------------------------------------------------------------ albums
    def _sync_albums(self, picasa_albums: dict, members: dict) -> None:
        data = self.albums.load()
        deleted = set(data["deleted_picasa"])
        overrides = {a["id"]: a for a in data["albums"]}
        with self.db.transaction():
            self.db.execute("DELETE FROM album")
            for token, alb in picasa_albums.items():
                if token in deleted or token in overrides:
                    continue
                self._insert_album(alb, "picasa", [{"root": r, "relpath": p} for r, p in members.get(token, [])])
            for alb in data["albums"]:
                self._insert_album(alb, alb.get("source", "user"), alb.get("photos", []))

    def _insert_album(self, alb: dict, source: str, photos: list[dict]) -> None:
        self.db.execute("INSERT OR REPLACE INTO album(id,name,description,date,source) VALUES (?,?,?,?,?)",
                        (alb["id"], alb["name"], alb.get("description"), alb.get("date"), source))
        for pos, ref in enumerate(photos):
            row = self.db.one("SELECT id FROM photo WHERE root=? AND relpath=?", (ref["root"], ref["relpath"]))
            if row:
                self.db.execute("INSERT OR IGNORE INTO album_photo(album_id, photo_id, position) VALUES (?,?,?)",
                                (alb["id"], row["id"], pos))

    def _album_snapshot(self, album_id: str) -> dict | None:
        a = self.db.one("SELECT * FROM album WHERE id=?", (album_id,))
        if not a:
            return None
        photos = self.db.query(
            "SELECT p.root, p.relpath FROM album_photo ap JOIN photo p ON p.id=ap.photo_id "
            "WHERE ap.album_id=? ORDER BY ap.position, p.taken_at", (album_id,))
        return {"id": a["id"], "name": a["name"], "description": a["description"], "date": a["date"],
                "source": a["source"], "photos": [{"root": p["root"], "relpath": p["relpath"]} for p in photos]}

    def _save_album(self, snap: dict) -> None:
        self.albums.upsert(snap)
        with self.db.transaction():
            self.db.execute("DELETE FROM album WHERE id=?", (snap["id"],))
            self._insert_album(snap, snap["source"], snap["photos"])

    def create_album(self, name: str, description: str | None = None) -> str:
        snap = {"id": self.albums.new_id(), "name": name, "description": description,
                "date": None, "source": "user", "photos": []}
        self._save_album(snap)
        return snap["id"]

    def update_album(self, album_id: str, name: str | None = None, description: str | None = None) -> None:
        snap = self._album_snapshot(album_id)
        if name:
            snap["name"] = name
        if description is not None:
            snap["description"] = description
        self._save_album(snap)

    def album_add(self, album_id: str, photo_ids: list[int]) -> None:
        snap = self._album_snapshot(album_id)
        existing = {(p["root"], p["relpath"]) for p in snap["photos"]}
        for row in self.rows_for(photo_ids):
            if (row["root"], row["relpath"]) not in existing:
                snap["photos"].append({"root": row["root"], "relpath": row["relpath"]})
                existing.add((row["root"], row["relpath"]))
        self._save_album(snap)

    def album_remove(self, album_id: str, photo_ids: list[int]) -> None:
        snap = self._album_snapshot(album_id)
        drop = {(row["root"], row["relpath"]) for row in self.rows_for(photo_ids)}
        snap["photos"] = [p for p in snap["photos"] if (p["root"], p["relpath"]) not in drop]
        self._save_album(snap)

    def delete_album(self, album_id: str) -> None:
        a = self.db.one("SELECT source FROM album WHERE id=?", (album_id,))
        if not a:
            return
        self.albums.delete(album_id, is_picasa=a["source"] == "picasa")
        self.db.execute("DELETE FROM album WHERE id=?", (album_id,))
