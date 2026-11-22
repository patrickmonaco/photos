"""Traitements en arrière-plan (file `job`) : IA et correction automatique des lignes fuyantes."""
from __future__ import annotations

import json
import logging
import threading
import time

import numpy as np
from PIL import Image

from . import ai
from .library import Library

log = logging.getLogger("phototheque.jobs")

PRIORITY = "CASE type WHEN 'perspective_auto' THEN 0 WHEN 'clip' THEN 1 ELSE 2 END"


class AIService:
    def __init__(self, lib: Library):
        self.lib = lib
        self.db = lib.db
        self.config = lib.config
        self.models = ai.ModelManager(self.config.data_dir / "models")
        self._clip = None
        self._faces = None
        self._faces_key = None
        self._load_lock = threading.Lock()
        self._vectors = None          # (ids, matrice) des vecteurs CLIP
        self._vectors_dirty = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.current = None
        self._last_type = None
        self._clu = None
        self._clu_lock = threading.RLock()
        self.db.execute("UPDATE job SET status='pending' WHERE status='running'")

    # ------------------------------------------------------------------ modèles
    def clip(self):
        if self._clip is None and self.models.ready("clip"):
            with self._load_lock:
                if self._clip is None:
                    self._clip = ai.Clip(self.models.path("clip"))
        return self._clip

    def faces(self):
        key = self.config.settings.face_model
        if (self._faces is None or self._faces_key != key) and self.models.ready(key):
            with self._load_lock:
                self._faces = ai.Faces(self.models.path(key))
                self._faces_key = key
        return self._faces

    def ensure_models(self) -> None:
        s = self.config.settings
        if s.ai_clip:
            self.models.download_async("clip")
        if s.ai_faces:
            self.models.download_async(s.face_model)

    # ------------------------------------------------------------------ file de jobs
    def enqueue_missing(self) -> int:
        s = self.config.settings
        before = self.db.one("SELECT COUNT(*) n FROM job WHERE status='pending'")["n"]
        # Requêtes ensemblistes (EXCEPT) : une passe sur chaque table, au lieu d'une recherche par photo.
        if s.ai_clip:
            self.db.execute(
                "INSERT OR IGNORE INTO job(photo_id, type, model_version) SELECT id, 'clip', ? FROM ("
                " SELECT id FROM photo"
                " EXCEPT SELECT photo_id FROM embedding WHERE model=?"
                " EXCEPT SELECT photo_id FROM job WHERE type='clip' AND status IN ('error', 'pending', 'running'))",
                (ai.CLIP_MODEL_ID, ai.CLIP_MODEL_ID))
        if s.ai_faces:
            self.db.execute(
                "INSERT OR IGNORE INTO job(photo_id, type, model_version) SELECT id, 'faces', ? FROM ("
                " SELECT id FROM photo"
                " EXCEPT SELECT photo_id FROM job WHERE type='faces' AND ("
                "   status IN ('pending', 'running') OR (status IN ('done', 'error') AND model_version=?)))",
                (s.face_model, s.face_model))
        self.enqueue_perspective()
        self._wake.set()
        return self.db.one("SELECT COUNT(*) n FROM job WHERE status='pending'")["n"] - before

    def enqueue_perspective(self, photo_ids: list[int] | None = None) -> None:
        tags = [t.strip().lower() for t in self.config.settings.perspective_auto_tags if t.strip()]
        if not tags:
            return
        marks = ",".join("?" * len(tags))
        sql = ("INSERT OR IGNORE INTO job(photo_id, type) SELECT DISTINCT p.id, 'perspective_auto' FROM photo p "
               "JOIN photo_tag pt ON pt.photo_id=p.id JOIN tag t ON t.id=pt.tag_id "
               f"WHERE lower(t.name) IN ({marks}) AND (p.ops IS NULL OR p.ops NOT LIKE '%\"perspective\"%') "
               "AND NOT EXISTS (SELECT 1 FROM job j WHERE j.photo_id=p.id AND j.type='perspective_auto' "
               "AND j.status IN ('done', 'error'))")
        params: list = list(tags)
        if photo_ids:
            sql += f" AND p.id IN ({','.join('?' * len(photo_ids))})"
            params += photo_ids
        self.db.execute(sql, tuple(params))
        self._wake.set()

    def _claim(self, limit: int = 1) -> list:
        """Choisit les prochains jobs : perspective automatique d'abord, puis CLIP et visages en
        alternance (l'un ne bloque pas l'autre), en commençant par les jobs les plus récents
        pour que les photos ajoutées soient traitées tout de suite."""
        with self.db.transaction():
            types = [r["type"] for r in self.db.query(
                f"SELECT DISTINCT type FROM job WHERE status='pending' ORDER BY {PRIORITY}")]
            if not types:
                return []
            if "perspective_auto" in types:
                kind = "perspective_auto"
            else:
                others = [t for t in types if t != self._last_type] or types
                kind = others[0]
            self._last_type = kind
            rows = self.db.query("SELECT id, photo_id, type FROM job WHERE status='pending' AND type=? "
                                 "ORDER BY id DESC LIMIT ?", (kind, limit if kind == "clip" else 4))
            self.db.execute(f"UPDATE job SET status='running', updated_at=CURRENT_TIMESTAMP "
                            f"WHERE id IN ({','.join('?' * len(rows))})", tuple(r["id"] for r in rows))
            return rows

    def _finish(self, job_id: int, status: str, result=None, error: str | None = None) -> None:
        self.db.execute("UPDATE job SET status=?, result=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (status, json.dumps(result) if result is not None else None, error, job_id))

    def process_pending(self, max_jobs: int | None = None) -> int:
        """Traite des jobs ; renvoie le nombre traité (utilisé par le worker et les tests)."""
        done = 0
        while max_jobs is None or done < max_jobs:
            rows = self._claim(16)
            if not rows:
                break
            kind = rows[0]["type"]
            self.current = kind
            try:
                if kind == "clip":
                    self._run_clip(rows)
                else:
                    for r in rows:
                        try:
                            res = self._run_faces(r["photo_id"]) if kind == "faces" \
                                else self._run_perspective(r["photo_id"])
                            self._finish(r["id"], "done", res)
                        except _NotReady:
                            self.db.execute("UPDATE job SET status='pending' WHERE id=?", (r["id"],))
                            return done
                        except Exception as exc:
                            log.exception("job %s", kind)
                            self._finish(r["id"], "error", error=str(exc))
            except _NotReady:
                self.db.execute(f"UPDATE job SET status='pending' WHERE id IN ({','.join('?' * len(rows))})",
                                tuple(r["id"] for r in rows))
                return done
            finally:
                self.current = None
            done += len(rows)
            time.sleep(0.01)  # laisse passer les écritures de l'interface entre deux lots
        return done

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="phototheque-worker").start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.process_pending(max_jobs=64)
            except Exception:  # pragma: no cover
                log.exception("worker")
                n = 0
            if not n:
                self._wake.wait(5)
                self._wake.clear()

    # ------------------------------------------------------------------ perspective auto
    def _run_perspective(self, pid: int) -> dict:
        row = self.lib.photo(pid)
        if not row:
            return {"skipped": "missing"}
        folder = self.lib.folder_path(row)
        entry = self.lib.sidecars.load(folder)["photos"].get(row["filename"], {})
        if "auto_perspective" in entry:
            return {"skipped": "already"}
        sugg = self.lib.perspective_suggestion(pid)
        if sugg["found"]:
            self.lib.sidecars.update_photo(folder, row["filename"], ops=sugg["ops"],
                                           auto_perspective={"pitch": sugg["estimate"]["pitch"],
                                                             "roll": sugg["estimate"]["roll"]})
        else:
            self.lib.sidecars.update_photo(folder, row["filename"], auto_perspective={"found": False})
        self.lib.refresh_photo(pid)
        return {"found": sugg["found"]}

    # ------------------------------------------------------------------ CLIP
    def _run_clip(self, rows) -> None:
        clip = self.clip()
        if clip is None:
            raise _NotReady
        imgs, ok = [], []
        for r in rows:
            row = self.lib.photo(r["photo_id"])
            try:
                imgs.append(ai.load_rgb(self.lib.abs_path(row), 336))
                ok.append(r)
            except Exception as exc:
                self._finish(r["id"], "error", error=str(exc))
        if not ok:
            return
        vecs = clip.embed_images(imgs)
        s = self.config.settings
        for r, v in zip(ok, vecs):
            pid = r["photo_id"]
            tags = clip.auto_tags(v, threshold=0.05)
            removed = self._removed_tags(pid)
            with self.db.transaction():
                self.db.execute("INSERT OR REPLACE INTO embedding(photo_id, model, vector) VALUES (?,?,?)",
                                (pid, ai.CLIP_MODEL_ID, v.astype(np.float16).tobytes()))
                self.db.execute("DELETE FROM auto_tag WHERE photo_id=?", (pid,))
                for label, score in tags:
                    self.db.execute("INSERT INTO auto_tag(photo_id, label, score, model) VALUES (?,?,?,?)",
                                    (pid, label, score, ai.CLIP_MODEL_ID))
                self._apply_auto_tags(pid, s, removed)
            self._finish(r["id"], "done", {"tags": [t for t, _ in tags]})
        self._vectors_dirty = True

    def _removed_tags(self, pid: int) -> set[str]:
        row = self.lib.photo(pid)
        entry = self.lib.sidecars.load(self.lib.folder_path(row))["photos"].get(row["filename"], {})
        return {t.lower() for t in entry.get("removed_tags", [])}

    def _apply_auto_tags(self, pid: int, s, removed: set[str] | None = None) -> None:
        removed = self._removed_tags(pid) if removed is None else removed
        self.db.execute("DELETE FROM photo_tag WHERE photo_id=? AND source='auto'", (pid,))
        for r in self.db.query("SELECT label, score FROM auto_tag WHERE photo_id=? AND score>=?",
                               (pid, s.ai_tag_threshold)):
            name = ai.tag_label(r["label"], s.ai_tag_language)
            if name.lower() in removed:
                continue
            self.db.execute("INSERT OR IGNORE INTO photo_tag(photo_id, tag_id, source) VALUES (?,?,'auto')",
                            (pid, self.db.tag_id(name, "keyword")))

    def refresh_auto_tags(self) -> None:
        """Après un changement de langue ou de seuil des tags automatiques."""
        s = self.config.settings
        ids = [r["photo_id"] for r in self.db.query("SELECT DISTINCT photo_id FROM auto_tag")]
        for i in range(0, len(ids), 200):
            with self.db.transaction():
                for pid in ids[i:i + 200]:
                    self._apply_auto_tags(pid, s)
        self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")

    def _matrix(self):
        """(ids, matrice float16) des vecteurs CLIP : moitié moins de mémoire qu'en float32."""
        if self._vectors_dirty or self._vectors is None:
            rows = self.db.query("SELECT photo_id, vector FROM embedding WHERE model=?", (ai.CLIP_MODEL_ID,))
            if rows:
                ids = np.array([r["photo_id"] for r in rows])
                mat = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float16).reshape(len(rows), -1)
                self._vectors = (ids, mat.copy())
            else:
                self._vectors = (np.array([], dtype=int), np.zeros((0, 512), np.float16))
            self._vectors_dirty = False
        return self._vectors

    def _scores(self, q: np.ndarray, chunk: int = 32768) -> np.ndarray:
        """Similarité de q avec toutes les photos, calculée par blocs en float32."""
        _, mat = self._matrix()
        q = q.astype(np.float32)
        out = np.empty(len(mat), dtype=np.float32)
        for i in range(0, len(mat), chunk):
            out[i:i + chunk] = mat[i:i + chunk].astype(np.float32) @ q
        return out

    def search(self, query: str, limit: int = 500, min_score: float = 0.18) -> list[int]:
        clip = self.clip()
        if clip is None:
            return []
        q = query.strip()
        for en, fr in ai.VOCABULARY:  # un libellé français connu -> prompt anglais
            if q.lower() == fr.lower():
                q = en
        text = clip.embed_texts([f"a photo of {q}"])[0]
        ids, mat = self._matrix()
        if not len(ids):
            return []
        sims = self._scores(text)
        order = np.argsort(-sims)[:limit]
        return [int(ids[i]) for i in order if sims[i] >= min_score]

    def similar(self, pid: int, limit: int = 200, min_score: float = 0.72) -> list[int]:
        ids, mat = self._matrix()
        where = np.nonzero(ids == pid)[0]
        if not len(where):
            return []
        sims = self._scores(mat[where[0]])
        order = np.argsort(-sims)[:limit + 1]
        return [int(ids[i]) for i in order if ids[i] != pid and sims[i] >= min_score]

    # ------------------------------------------------------------------ visages
    def _run_faces(self, pid: int) -> dict:
        model = self.faces()
        if model is None:
            raise _NotReady
        row = self.lib.photo(pid)
        img = ai.load_rgb(self.lib.abs_path(row), 1280)
        w, h = img.size
        dets = [d for d in model.detect(img) if d["score"] >= 0.6
                and min(d["box"][2] - d["box"][0], d["box"][3] - d["box"][1]) >= 32]
        picasa = self.db.query("SELECT l, t, r, b, person FROM face WHERE photo_id=? AND source='picasa' "
                               "AND person IS NOT NULL", (pid,))
        s = self.config.settings
        removed = self._removed_tags(pid)
        named_clusters: list[tuple[int, str]] = []
        # Calcul des signatures AVANT la transaction : le verrou d'écriture n'est tenu que le temps
        # des écritures, jamais pendant l'inférence.
        embs = [model.embed(img, d["kps"]) for d in dets]
        with self.db.transaction():
            self.db.execute("DELETE FROM face WHERE photo_id=? AND source='ai'", (pid,))
            self.db.execute("DELETE FROM photo_tag WHERE photo_id=? AND source='face'", (pid,))
            for d, emb in zip(dets, embs):
                x1, y1, x2, y2 = d["box"]
                rel = (max(0.0, float(x1) / w), max(0.0, float(y1) / h),
                       min(1.0, float(x2) / w), min(1.0, float(y2) / h))
                person = next((p["person"] for p in picasa
                               if ai.iou(rel, (p["l"], p["t"], p["r"], p["b"])) > 0.3), None)
                cid, cname, newly_named = self._assign_cluster(emb, person, s.face_cluster_threshold)
                person = person or cname
                if newly_named:
                    named_clusters.append((cid, cname))
                self.db.execute("INSERT INTO face(photo_id, l, t, r, b, person, source, score, embedding, cluster_id) "
                                "VALUES (?,?,?,?,?,?,'ai',?,?,?)",
                                (pid, *rel, person, d["score"], emb.tobytes(), cid))
                if person and person.lower() not in removed:
                    self.db.execute("INSERT OR IGNORE INTO photo_tag(photo_id, tag_id, source) VALUES (?,?,'face')",
                                    (pid, self.db.tag_id(person, "person")))
        # Un groupe qui vient de recevoir un nom (venu de Picasa) : le nom s'étend aux photos déjà analysées.
        for cid, name in named_clusters:
            self._propagate_name(cid, name)
        return {"faces": len(dets)}

    def _propagate_name(self, cid: int, name: str) -> None:
        pids = [r["photo_id"] for r in self.db.query(
            "SELECT DISTINCT photo_id FROM face WHERE cluster_id=? AND (person IS NULL OR person!=?)", (cid, name))]
        self.db.execute("UPDATE face SET person=? WHERE cluster_id=? AND person IS NULL", (name, cid))
        for pid in pids:
            if name.lower() in self._removed_tags(pid):
                continue
            self.db.execute("INSERT OR IGNORE INTO photo_tag(photo_id, tag_id, source) VALUES (?,?,'face')",
                            (pid, self.db.tag_id(name, "person")))

    # -- centres des groupes de visages, gardés en mémoire (évite de tout relire pour chaque visage)
    def _clusters_mem(self) -> dict:
        if self._clu is None:
            rows = self.db.query("SELECT id, name, centroid, n FROM face_cluster ORDER BY id")
            k = len(rows)
            cap = max(1024, k * 2)
            cents = np.zeros((cap, 512), dtype=np.float32)
            if k:
                cents[:k] = np.frombuffer(b"".join(r["centroid"] for r in rows), dtype=np.float32).reshape(k, -1)
            self._clu = {"k": k, "cents": cents, "ids": [r["id"] for r in rows],
                         "n": [r["n"] for r in rows], "names": [r["name"] for r in rows],
                         "by_name": {r["name"].lower(): i for i, r in enumerate(rows) if r["name"]}}
        return self._clu

    def invalidate_clusters(self) -> None:
        with self._clu_lock:
            self._clu = None

    def _assign_cluster(self, emb: np.ndarray, person: str | None, threshold: float):
        with self._clu_lock:
            m = self._clusters_mem()
            k = m["k"]
            best, best_sim = None, -1.0
            if k:
                sims = m["cents"][:k] @ emb
                best = int(np.argmax(sims))
                best_sim = float(sims[best])
            if person:  # un nom venant de Picasa rattache au groupe de ce nom
                named = m["by_name"].get(person.lower())
                if named is not None:
                    best, best_sim = named, 1.0
            if best is not None and best_sim >= threshold:
                old_name = m["names"][best]
                c = m["cents"][best] * m["n"][best] + emb
                c /= np.linalg.norm(c)
                m["cents"][best] = c
                m["n"][best] += 1
                name = old_name or person
                if name and not old_name:
                    m["names"][best] = name
                    m["by_name"][name.lower()] = best
                self.db.execute("UPDATE face_cluster SET centroid=?, n=n+1, name=? WHERE id=?",
                                (c.astype(np.float32).tobytes(), name, m["ids"][best]))
                return m["ids"][best], name, bool(name and not old_name)
            cid = self.db.execute("INSERT INTO face_cluster(name, centroid, n) VALUES (?,?,1)",
                                  (person, emb.tobytes())).lastrowid
            if k == len(m["cents"]):
                m["cents"] = np.concatenate([m["cents"], np.zeros_like(m["cents"])])
            m["cents"][k] = emb
            m["ids"].append(cid)
            m["n"].append(1)
            m["names"].append(person)
            if person:
                m["by_name"][person.lower()] = k
            m["k"] = k + 1
            return cid, person, False

    def clusters(self, min_faces: int = 2) -> list[dict]:
        # Une seule passe sur les visages (agrégats + meilleur visage par fenêtre), au lieu d'une
        # sous-requête par groupe qui relisait toute la table.
        rows = self.db.query(
            "WITH ranked AS (SELECT id, cluster_id, photo_id, "
            "  ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY score DESC) rn FROM face WHERE cluster_id > 0), "
            "stats AS (SELECT cluster_id, COUNT(*) n, COUNT(DISTINCT photo_id) photos, "
            "  MAX(CASE WHEN rn = 1 THEN id END) cover FROM ranked GROUP BY cluster_id) "
            "SELECT c.id, c.name, s.n, s.photos, s.cover FROM face_cluster c JOIN stats s ON s.cluster_id = c.id "
            "WHERE s.n >= ? OR c.name IS NOT NULL ORDER BY c.name IS NULL, lower(c.name), s.n DESC", (min_faces,))
        return [dict(r) for r in rows]

    def name_cluster(self, cid: int, name: str) -> int:
        """Nomme un groupe (ou le fusionne avec le groupe portant déjà ce nom)."""
        name = name.strip()
        target = self.db.one("SELECT id FROM face_cluster WHERE lower(name)=lower(?) AND id!=?", (name, cid))
        with self.db.transaction():
            if target:
                self.db.execute("UPDATE face SET cluster_id=? WHERE cluster_id=?", (target["id"], cid))
                self.db.execute("DELETE FROM face_cluster WHERE id=?", (cid,))
                cid = target["id"]
                embs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in
                        self.db.query("SELECT embedding FROM face WHERE cluster_id=? AND embedding IS NOT NULL", (cid,))]
                if embs:
                    c = np.mean(embs, axis=0)
                    self.db.execute("UPDATE face_cluster SET centroid=?, n=? WHERE id=?",
                                    ((c / np.linalg.norm(c)).astype(np.float32).tobytes(), len(embs), cid))
            self.db.execute("UPDATE face_cluster SET name=? WHERE id=?", (name, cid))
            self.db.execute("UPDATE face SET person=? WHERE cluster_id=?", (name, cid))
        self.invalidate_clusters()
        # Le nom est enregistré dans les directives des photos (tag « personne »), donc durable.
        pids = [r["photo_id"] for r in self.db.query("SELECT DISTINCT photo_id FROM face WHERE cluster_id=?", (cid,))]
        for pid in pids:
            row = self.lib.photo(pid)
            folder = self.lib.folder_path(row)
            entry = self.lib.sidecars.load(folder)["photos"].get(row["filename"], {})
            people = list(entry.get("people", []))
            if name.lower() not in {p.lower() for p in people}:
                people.append(name)
                self.lib.sidecars.update_photo(folder, row["filename"], people=people)
            self.lib.refresh_photo(pid)
        return cid

    def delete_cluster(self, cid: int) -> dict:
        """Supprime un groupe : ses visages ne sont plus regroupés (marqués ignorés, cluster_id = -1).
        Si le groupe portait un nom, ce nom est retiré des photos concernées."""
        c = self.db.one("SELECT name FROM face_cluster WHERE id=?", (cid,))
        if not c:
            return {"deleted": False}
        self.invalidate_clusters()
        pids = [r["photo_id"] for r in self.db.query("SELECT DISTINCT photo_id FROM face WHERE cluster_id=?", (cid,))]
        name = c["name"]
        with self.db.transaction():
            self.db.execute("UPDATE face SET cluster_id=-1, person=NULL WHERE cluster_id=?", (cid,))
            self.db.execute("DELETE FROM face_cluster WHERE id=?", (cid,))
            if name:
                for pid in pids:
                    self.db.execute("DELETE FROM photo_tag WHERE photo_id=? AND source='face' AND tag_id IN "
                                    "(SELECT id FROM tag WHERE lower(name)=lower(?) AND kind='person')", (pid, name))
        if name:
            # Nom enregistré dans les directives par « Nommer ce groupe » : on le retire aussi.
            for pid in pids:
                row = self.lib.photo(pid)
                folder = self.lib.folder_path(row)
                entry = self.lib.sidecars.load(folder)["photos"].get(row["filename"], {})
                people = [p for p in entry.get("people", []) if p.lower() != name.lower()]
                if len(people) != len(entry.get("people", [])):
                    self.lib.sidecars.update_photo(folder, row["filename"], people=people or None)
                    self.lib.refresh_photo(pid)
        self.db.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM photo_tag)")
        return {"deleted": True, "photos": len(pids), "name": name}

    def face_thumb(self, face_id: int, size: int = 160) -> Image.Image | None:
        f = self.db.one("SELECT * FROM face WHERE id=?", (face_id,))
        if not f:
            return None
        img = ai.load_rgb(self.lib.abs_path(self.lib.photo(f["photo_id"])), 1600)
        w, h = img.size
        cx, cy = (f["l"] + f["r"]) / 2 * w, (f["t"] + f["b"]) / 2 * h
        half = max((f["r"] - f["l"]) * w, (f["b"] - f["t"]) * h) * 0.8
        crop = img.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half)))
        crop.thumbnail((size, size))
        return crop

    # ------------------------------------------------------------------ état
    def status(self) -> dict:
        counts = {}
        for r in self.db.query("SELECT type, status, COUNT(*) n FROM job GROUP BY type, status"):
            counts.setdefault(r["type"], {})[r["status"]] = r["n"]
        return {"models": self.models.status(), "jobs": counts, "current": self.current,
                "embeddings": self.db.one("SELECT COUNT(*) n FROM embedding")["n"],
                "faces": self.db.one("SELECT COUNT(*) n FROM face WHERE source='ai'")["n"]}


class _NotReady(Exception):
    """Modèle pas encore disponible : le job reste en attente."""

