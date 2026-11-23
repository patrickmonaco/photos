"""Identification des plantes avec le service Pl@ntNet (https://my.plantnet.org).

Seules les photos portant un des tags choisis (fleur, plante, arbre par défaut) et sans légende
sont envoyées. Le nom trouvé devient la légende de la photo, enregistrée dans les directives
(.phototheque.json) : la photo n'est jamais modifiée.

Quota : le service compte les identifications par jour UTC (remise à zéro à 00:00 UTC, soit 1 h
ou 2 h du matin en France). Un compteur local, plafonné par le réglage « appels par jour », est
recalé sur le nombre restant que renvoie Pl@ntNet après chaque appel.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .net import open_url as _open, ssl_context  # noqa: F401  (ssl_context : compatibilité)

log = logging.getLogger("phototheque")

API_URL = os.environ.get("PHOTOTHEQUE_PLANTNET_URL", "https://my-api.plantnet.org").rstrip("/")


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def next_reset_local() -> str:
    """Prochaine remise à zéro du quota (minuit UTC), en heure locale."""
    now = datetime.now(timezone.utc)
    reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return reset.astimezone().strftime("%H:%M")


def _multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data, ctype in files:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class QuotaExceeded(Exception):
    pass


class KeyRejected(Exception):
    pass


class PlantNetService:
    def __init__(self, lib, cache=None):
        self.lib = lib
        self.db = lib.db
        self.config = lib.config
        self.quota_path = self.config.data_dir / "plantnet.json"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "identified": 0, "low": 0, "notfound": 0,
                      "errors": 0, "message": None, "last": None}

    # ---------------- quota ----------------
    def _load_quota(self) -> dict:
        try:
            q = json.loads(self.quota_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = {}
        if q.get("day") != utc_day():
            q = {"day": utc_day(), "count": 0, "remaining": None}
        return q

    def _save_quota(self, q: dict) -> None:
        tmp = self.quota_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(q), encoding="utf-8")
        tmp.replace(self.quota_path)

    def quota(self) -> dict:
        q = self._load_quota()
        cap = int(self.config.settings.plantnet_daily_max)
        left = max(0, cap - q["count"])
        if q.get("remaining") is not None:
            left = min(left, max(0, int(q["remaining"])))
        return {"day": q["day"], "used": q["count"], "max": cap, "left": left,
                "remaining_server": q.get("remaining"), "reset": next_reset_local()}

    def _sync_quota(self, key: str) -> None:
        """Recale le compteur sur l'usage du jour connu de Pl@ntNet (ne consomme pas d'identification)."""
        try:
            url = f"{API_URL}/v2/quota/daily?" + urllib.parse.urlencode({"api-key": key})
            with _open(url, 20) as r:
                data = json.loads(r.read())
            ident = (data.get("quota") or {}).get("identify") or {}
            q = self._load_quota()
            if "count" in ident:
                q["count"] = max(q["count"], int(ident["count"]))
            if "remaining" in ident:
                q["remaining"] = int(ident["remaining"])
            self._save_quota(q)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise KeyRejected() from e
            log.info("Pl@ntNet : quota du jour non lu (%s)", e)
        except Exception as e:  # réseau indisponible... on garde le compteur local
            log.info("Pl@ntNet : quota du jour non lu (%s)", e)

    # ---------------- photos à traiter ----------------
    def _tags(self) -> list[str]:
        return [t.strip().lower() for t in self.config.settings.plantnet_tags if t.strip()]

    def candidates(self, limit: int | None = None) -> list[int]:
        tags = self._tags()
        if not tags:
            return []
        marks = ",".join("?" * len(tags))
        sql = (f"SELECT DISTINCT p.id FROM photo p JOIN photo_tag pt ON pt.photo_id = p.id "
               f"JOIN tag t ON t.id = pt.tag_id "
               f"WHERE lower(t.name) IN ({marks}) AND coalesce(p.media, 'image') != 'video' "
               f"AND coalesce(trim(p.caption), '') = '' "
               f"AND p.id NOT IN (SELECT photo_id FROM plant_id) "
               f"ORDER BY p.taken_at DESC, p.id DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["id"] for r in self.db.query(sql, tags)]

    def status(self) -> dict:
        s = self.config.settings
        return {**self.state, "quota": self.quota(), "pending": len(self.candidates()),
                "key": bool(s.plantnet_key.strip()), "tags": s.plantnet_tags}

    # ---------------- traitement ----------------
    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            if not self.config.settings.plantnet_key.strip():
                raise KeyRejected()
            self._stop.clear()
            self.state.update(running=True, done=0, total=0, identified=0, low=0, notfound=0, errors=0,
                              message=None, last=None)
            self._thread = threading.Thread(target=self._run, name="plantnet", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        key = self.config.settings.plantnet_key.strip()
        try:
            self._sync_quota(key)
            left = self.quota()["left"]
            ids = self.candidates(limit=left) if left else []
            self.state["total"] = len(ids)
            if not left:
                self.state["message"] = "quota"
            for pid in ids:
                if self._stop.is_set():
                    self.state["message"] = "stopped"
                    break
                if self.quota()["left"] <= 0:
                    self.state["message"] = "quota"
                    break
                try:
                    self._identify(pid, key)
                except (QuotaExceeded, KeyRejected):
                    raise
                except Exception as e:
                    log.exception("Pl@ntNet : échec sur la photo %s", pid)
                    self.state["errors"] += 1
                    self.state["last"] = str(e)
                    if self.state["errors"] >= 5 and self.state["identified"] + self.state["notfound"] == 0:
                        self.state["message"] = "network"
                        break
                self.state["done"] += 1
            else:
                if self.state["message"] is None:
                    # Plafond atteint alors qu'il reste des photos : on le dit, la suite se fera demain.
                    rest = self.quota()["left"] == 0 and self.candidates(limit=1)
                    self.state["message"] = "quota" if rest else "done"
        except QuotaExceeded:
            self.state["message"] = "quota"
        except KeyRejected:
            self.state["message"] = "key"
        except Exception as e:
            log.exception("Pl@ntNet : traitement interrompu")
            self.state.update(message="network", last=str(e))
        finally:
            self.state["running"] = False
            log.info("Pl@ntNet : %s", {k: self.state[k] for k in ("done", "identified", "low", "notfound",
                                                                   "errors", "message")})

    def _image_bytes(self, row) -> bytes:
        """Photo telle qu'affichée (recadrage, redressement...), petit côté 1280 px, JPEG 90 %."""
        from .render import render
        w, h = row["width"] or 1280, row["height"] or 1280
        long_side = int(1280 * max(w, h) / max(1, min(w, h)))
        ops = [o for o in json.loads(row["ops"] or "[]") if o.get("op") in ("rotate90", "straighten", "crop",
                                                                            "perspective")]
        img = render(self.lib.abs_path(row), ops, min(long_side, 4096), self.config.settings.default_f35,
                     row["f35"])
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()

    def _identify(self, pid: int, key: str) -> None:
        s = self.config.settings
        row = self.lib.photo(pid)
        if row is None:
            return
        body, ctype = _multipart([("organs", "auto")],
                                 [("images", "photo.jpg", self._image_bytes(row), "image/jpeg")])
        params = {"api-key": key, "lang": s.plantnet_lang or s.language, "nb-results": "3",
                  "include-related-images": "false"}
        url = f"{API_URL}/v2/identify/all?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        q = self._load_quota()
        try:
            with _open(req, 60) as r:
                data = json.loads(r.read())
            code = 200
        except urllib.error.HTTPError as e:
            code, data = e.code, {}
            if code == 429:
                q["remaining"] = 0
                self._save_quota(q)
                raise QuotaExceeded() from e
            if code in (401, 403):
                raise KeyRejected() from e
            if code != 404:
                raise RuntimeError(f"HTTP {code}") from e
        # Appel abouti (200 ou 404 « espèce non trouvée ») : il compte dans le quota.
        q["count"] += 1
        if "remainingIdentificationRequests" in data:
            q["remaining"] = int(data["remainingIdentificationRequests"])
        self._save_quota(q)

        now = datetime.now().isoformat(timespec="seconds")
        results = data.get("results") or []
        if code == 404 or not results:
            self.db.execute("INSERT OR REPLACE INTO plant_id (photo_id, status, at) VALUES (?, 'notfound', ?)",
                            (pid, now))
            self.state["notfound"] += 1
            return
        best = results[0]
        sp = best.get("species") or {}
        sci = sp.get("scientificNameWithoutAuthor") or data.get("bestMatch") or ""
        common = (sp.get("commonNames") or [None])[0]
        family = ((sp.get("family") or {}).get("scientificNameWithoutAuthor")
                  or (sp.get("family") or {}).get("scientificName"))
        score = float(best.get("score") or 0)
        caption = None
        status = "ok" if score >= s.plantnet_min_score else "low"
        if status == "ok":
            caption = f"{common[0].upper() + common[1:]} ({sci})" if common else sci
            # La légende n'est posée que si la photo n'en a toujours pas.
            fresh = self.lib.photo(pid)
            if fresh is not None and not (fresh["caption"] or "").strip():
                self.lib.set_caption(pid, caption)
            else:
                caption = None
            self.state["identified"] += 1
        else:
            self.state["low"] += 1
        self.db.execute("INSERT OR REPLACE INTO plant_id (photo_id, status, species, common, family, score, "
                        "caption, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (pid, status, sci, common, family, score, caption, now))

    def info(self, pid: int) -> dict | None:
        r = self.db.one("SELECT status, species, common, family, score FROM plant_id WHERE photo_id=?", (pid,))
        return dict(r) if r else None

    def reset(self, which: str = "failed") -> int:
        """Permet de retenter les photos non reconnues ('failed') ou toutes ('all')."""
        if which == "all":
            return self.db.execute("DELETE FROM plant_id").rowcount
        return self.db.execute("DELETE FROM plant_id WHERE status IN ('notfound', 'low', 'error')").rowcount
