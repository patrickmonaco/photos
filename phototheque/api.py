"""API REST (FastAPI) + service de l'interface web."""
from __future__ import annotations

import json
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import __version__
from fastapi.staticfiles import StaticFiles
from .aiservice import AIService
from .plantnet import KeyRejected, PlantNetService
from .wordpress import WordPressService, WPError
from .config import Config
from .library import Library, ops_hash
from .metadata import read_camera
from .picasa import parse_folder_cached
from .ai import iou as ai_iou
from .render import SIZES, RenderCache, map_box, render, to_jpeg
from .watcher import FolderWatcher

STATIC = Path(__file__).parent / "static"


log = logging.getLogger("phototheque")


def setup_logging(config: Config) -> Path:
    """Journal dans <données>/phototheque.log : phases du démarrage, requêtes lentes, erreurs."""
    path = config.data_dir / "phototheque.log"
    root = logging.getLogger("phototheque")
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    return path


def create_app(config: Config, scan_on_start: bool = True) -> FastAPI:
    lib = Library(config)
    cache = RenderCache(config.cache_dir, int(config.settings.cache_max_gb * 1024 ** 3))

    def purge_loop():
        """Purge du cache au démarrage puis toutes les 15 minutes."""
        import time as _time
        while True:
            try:
                cache.max_bytes = int(config.settings.cache_max_gb * 1024 ** 3)
                r = cache.purge()
                log.info("cache vérifié en %.1f s : %.0f Mo, %s fichiers supprimés",
                         r["seconds"], r["before"] / 1e6, r["removed"])
            except Exception:  # pragma: no cover
                pass
            _time.sleep(900)
    app = FastAPI(title="Photothèque", docs_url="/api/docs")
    app.state.library = lib
    aisvc = AIService(lib)
    app.state.ai = aisvc
    plantnet = PlantNetService(lib)
    app.state.plantnet = plantnet
    wordpress = WordPressService(lib)
    app.state.wordpress = wordpress
    # Les miniatures passent par un nombre limité de rendus simultanés, pour que l'aperçu
    # de la visionneuse ne fasse pas la queue derrière une grille entière.
    thumb_slots = threading.Semaphore(3)
    prefetch_slots = threading.Semaphore(1)   # préchargement des voisines : jamais devant la photo demandée

    def scan_then_enqueue():
        t = time.time()
        st = lib.scan_all()
        log.info("indexation terminée en %.1f s : %s photos, %s dossiers inchangés sautés, erreur=%s",
                 time.time() - t, st.get("done"), st.get("skipped"), st.get("error"))
        t = time.time()
        n = aisvc.enqueue_missing()
        log.info("file IA mise à jour en %.1f s (%s nouveaux traitements)", time.time() - t, n)

    def start_scan():
        threading.Thread(target=scan_then_enqueue, daemon=True).start()

    watcher = FolderWatcher(lib, on_scanned=aisvc.enqueue_missing)
    app.state.watcher = watcher

    def restart_watcher():
        """En arrière-plan : poser la surveillance parcourt tous les dossiers, ce qui peut être long
        (NAS, disque USB) ; le serveur ne doit jamais attendre."""
        def run():
            t = time.time()
            if config.settings.watch_folders:
                ok = watcher.start()
                log.info("surveillance des dossiers %s en %.1f s", "active" if ok else "indisponible", time.time() - t)
            else:
                watcher.stop()
        threading.Thread(target=run, daemon=True, name="phototheque-watch-start").start()

    if scan_on_start:
        logfile = setup_logging(config)

        @app.middleware("http")
        async def slow_requests(request, call_next):
            t = time.time()
            response = await call_next(request)
            dt = time.time() - t
            if dt > 0.5 and not request.url.path.endswith(("/thumb", "/preview", "/full", "/render", "/export", "/video")):
                log.warning("requête lente : %s %s en %.1f s", request.method, request.url.path, dt)
            return response

        @app.on_event("startup")
        def _startup():
            log.info("démarrage (données : %s, journal : %s)", config.data_dir, logfile)
            start_scan()
            aisvc.ensure_models()
            aisvc.start()
            restart_watcher()
            threading.Thread(target=purge_loop, daemon=True, name="phototheque-cache").start()

        @app.on_event("shutdown")
        def _shutdown():
            aisvc.stop()
            watcher.stop()

    def get_photo(pid: int):
        row = lib.photo(pid)
        if not row:
            raise HTTPException(404, "photo inconnue")
        return row

    # ---------------------------------------------------------------- état, réglages
    @app.get("/api/status")
    def status():
        total = lib.db.one("SELECT COUNT(*) n FROM photo")["n"]
        return {"scan": lib.scan_state, "photos": total, "settings": config.settings.__dict__}

    @app.post("/api/scan")
    def scan():
        start_scan()
        return lib.scan_state

    @app.put("/api/settings")
    def update_settings(payload: dict = Body(...)):
        s = config.settings
        before = dict(s.__dict__)
        for key in ("language", "picasa_tilt_deg_per_unit", "picasa_rotate_clockwise", "default_f35",
                    "perspective_strength", "theme", "slideshow_interval", "ai_clip", "ai_faces",
                    "ai_tag_language", "ai_tag_threshold", "face_model", "face_cluster_threshold",
                    "enhance_strength", "watch_folders", "cache_max_gb", "plantnet_key",
                    "plantnet_daily_max", "plantnet_min_score", "plantnet_lang", "wordpress_url",
                    "wordpress_user", "wordpress_app_password", "wordpress_max_size", "wordpress_quality",
                    "wordpress_status"):
            if key in payload:
                kind = type(getattr(s, key))
                val = payload[key]
                if kind is bool:
                    val = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
                setattr(s, key, kind(val))
        if "plantnet_tags" in payload:
            tags = payload["plantnet_tags"]
            if isinstance(tags, str):
                tags = tags.split(",")
            s.plantnet_tags = sorted({t.strip() for t in tags if t.strip()}, key=str.lower)
        s.plantnet_key = s.plantnet_key.strip()
        s.wordpress_url = s.wordpress_url.strip().rstrip("/")
        s.wordpress_max_size = max(320, min(int(s.wordpress_max_size), 8000))
        s.wordpress_quality = max(50, min(int(s.wordpress_quality), 100))
        if s.wordpress_status not in ("publish", "draft", "private"):
            s.wordpress_status = "publish"
        s.plantnet_daily_max = max(0, min(int(s.plantnet_daily_max), 100000))
        if "perspective_auto_tags" in payload:
            tags = payload["perspective_auto_tags"]
            if isinstance(tags, str):
                tags = tags.split(",")
            s.perspective_auto_tags = sorted({t.strip() for t in tags if t.strip()}, key=str.lower)
        if "add_root" in payload:
            if not Path(payload["add_root"]).expanduser().is_dir():
                raise HTTPException(400, "dossier introuvable")
            config.add_root(payload["add_root"])
        if "remove_root" in payload:
            s.roots = [r for r in s.roots if r != payload["remove_root"]]
        if "include_folder" in payload:
            s.excluded_folders = [f for f in s.excluded_folders if f != payload["include_folder"]]
        config.save()
        changed = {k for k, v in s.__dict__.items() if before.get(k) != v}
        if changed & {"ai_clip", "ai_faces", "face_model"}:
            aisvc.ensure_models()
            aisvc.enqueue_missing()
        if changed & {"ai_tag_language", "ai_tag_threshold"}:
            threading.Thread(target=aisvc.refresh_auto_tags, daemon=True).start()
        if "perspective_auto_tags" in changed:
            aisvc.enqueue_perspective()
        if "cache_max_gb" in changed:
            cache.max_bytes = int(s.cache_max_gb * 1024 ** 3)
            threading.Thread(target=cache.purge, daemon=True).start()
        if changed & {"roots", "excluded_folders", "watch_folders"} and scan_on_start:
            restart_watcher()
        return s.__dict__

    @app.get("/api/cache")
    def cache_usage():
        return {**cache.usage(), "last_purge": cache.last_purge}

    @app.post("/api/cache/clear")
    def cache_clear(payload: dict = Body(default={})):
        n = cache.clear(payload.get("what", "previews"))
        return {"removed": n, **cache.usage()}

    @app.post("/api/folders/exclude")
    def exclude_folder(payload: dict = Body(...)):
        """Retire un dossier de la photothèque. Rien n'est supprimé sur le disque."""
        root, folder = payload["root"], payload.get("folder") or ""
        s = config.settings
        if root not in s.roots:
            raise HTTPException(404, "racine inconnue")
        if not folder:
            s.roots = [r for r in s.roots if r != root]
        else:
            path = str(Path(root) / folder)
            if path not in s.excluded_folders:
                s.excluded_folders.append(path)
        config.save()
        if scan_on_start:
            restart_watcher()
        start_scan()
        return s.__dict__

    @app.post("/api/calibrate")
    def calibrate_tilt():
        from .calibrate import calibrate
        return calibrate(config.settings.roots)

    # ---------------------------------------------------------------- navigation
    @app.get("/api/folders")
    def folders():
        rows = lib.db.query("SELECT root, folder, COUNT(*) n FROM photo GROUP BY root, folder ORDER BY root, folder")
        return [dict(r) for r in rows]

    @app.get("/api/dates")
    def dates():
        rows = lib.db.query("SELECT year, month, day, COUNT(*) n FROM photo WHERE year IS NOT NULL "
                            "GROUP BY year, month, day ORDER BY year DESC, month DESC, day DESC")
        return [dict(r) for r in rows]

    @app.get("/api/tags")
    def tags():
        rows = lib.db.query("SELECT t.name, t.kind, COUNT(*) n FROM tag t JOIN photo_tag pt ON pt.tag_id=t.id "
                            "GROUP BY t.id ORDER BY t.kind, lower(t.name)")
        return [dict(r) for r in rows]

    @app.get("/api/albums")
    def albums():
        rows = lib.db.query(
            "SELECT a.id, a.name, a.description, a.date, a.source, COUNT(ap.photo_id) n, "
            "(SELECT ap2.photo_id FROM album_photo ap2 WHERE ap2.album_id=a.id ORDER BY ap2.position LIMIT 1) cover "
            "FROM album a LEFT JOIN album_photo ap ON ap.album_id=a.id GROUP BY a.id ORDER BY lower(a.name)")
        return [dict(r) for r in rows]

    COLUMNS = ("p.id, p.filename, p.folder, p.root, p.taken_at, p.width, p.height, p.star, p.ops_hash, "
               "p.ops_source, p.caption, p.media, p.duration")

    def build_filter(f: dict):
        """Filtres d'une vue -> (clause WHERE, paramètres, ORDER BY, paramètres d'ordre)."""
        where, params, order, order_params = [], [], "p.taken_at, p.filename", []
        if f.get("cluster") not in (None, ""):
            where.append("p.id IN (SELECT photo_id FROM face WHERE cluster_id=?)"); params.append(int(f["cluster"]))
            order = "p.taken_at DESC, p.filename"     # les photos d'une personne : les plus récentes d'abord
        if f.get("root") is not None:
            where.append("p.root=?"); params.append(f["root"])
        folder = f.get("folder")
        recursive = str(f.get("recursive", "")).lower() in ("1", "true")
        if folder is not None:
            if recursive and folder:
                where.append("(p.folder=? OR p.folder LIKE ?)"); params += [folder, folder + "/%"]
            elif not recursive:
                where.append("p.folder=?"); params.append(folder)
            # Vue récursive : photos regroupées par sous-dossier, puis par nom
            order = "p.folder, p.filename" if recursive else "p.filename"
        for col in ("year", "month", "day"):
            if f.get(col) not in (None, ""):
                where.append(f"p.{col}=?"); params.append(int(f[col]))
        if f.get("tag"):
            where.append("p.id IN (SELECT pt.photo_id FROM photo_tag pt JOIN tag t ON t.id=pt.tag_id "
                         "WHERE t.name=? AND t.kind=?)"); params += [f["tag"], f.get("kind") or "keyword"]
        if f.get("album"):
            where.append("p.id IN (SELECT photo_id FROM album_photo WHERE album_id=?)"); params.append(f["album"])
            order = "(SELECT position FROM album_photo WHERE album_id=? AND photo_id=p.id), p.taken_at"
            order_params = [f["album"]]
        # Tri choisi dans la barre d'outils (sauf pour un album, qui garde son ordre)
        if f.get("sort") in ("date_desc", "date_asc") and not f.get("album"):
            order = "p.taken_at DESC, p.filename" if f["sort"] == "date_desc" else "p.taken_at, p.filename"
            order_params = []
        if str(f.get("starred", "")).lower() in ("1", "true"):
            where.append("p.star=1")
        if f.get("q"):
            like = f"%{f['q']}%"
            where.append("(p.filename LIKE ? OR p.caption LIKE ? OR p.id IN (SELECT pt.photo_id FROM photo_tag pt "
                         "JOIN tag t ON t.id=pt.tag_id WHERE t.name LIKE ?))"); params += [like, like, like]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, params, order, order_params

    def ranked_ids(f: dict) -> list[int] | None:
        if f.get("semantic"):
            return aisvc.search(f["semantic"])
        if f.get("similar") not in (None, ""):
            return aisvc.similar(int(f["similar"]))
        return None

    def resolve_ids(payload: dict) -> list[int]:
        """Photos visées par une action : liste explicite, ou « toute la vue » (filtres) moins des exclusions.
        La sélection « tout » est ainsi résolue par le serveur, sans charger la vue dans le navigateur."""
        if "selection" not in payload:
            return [int(p) for p in payload.get("photo_ids", [])]
        sel = payload["selection"]
        f = sel.get("query", {})
        exclude = {int(i) for i in sel.get("exclude", [])}
        ids = ranked_ids(f)
        if ids is None:
            clause, params, _, _ = build_filter(f)
            ids = [r["id"] for r in lib.db.query(f"SELECT p.id FROM photo p{clause}", tuple(params))]
        return [i for i in ids if i not in exclude]

    @app.get("/api/photos")
    def photos(request: Request, offset: int = 0, limit: int = Query(300, le=2000)):
        f = dict(request.query_params)
        ranked = ranked_ids(f)
        if ranked is not None:
            # Résultats classés par l'IA (similarité), puis paginés.
            page = ranked[offset:offset + limit]
            rows = {r["id"]: dict(r) for r in lib.db.query(
                f"SELECT {COLUMNS} FROM photo p WHERE p.id IN ({','.join('?' * len(page))})", tuple(page))} \
                if page else {}
            return {"total": len(ranked), "items": [rows[i] for i in page if i in rows]}
        clause, params, order, order_params = build_filter(f)
        count = lib.db.one(f"SELECT COUNT(*) n FROM photo p{clause}", tuple(params))["n"]
        rows = lib.db.query(f"SELECT {COLUMNS} FROM photo p{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                            tuple(params + order_params + [limit, offset]))
        return {"total": count, "items": [dict(r) for r in rows]}

    @app.get("/api/photos/index")
    def photo_index(request: Request, pid: int):
        """Position d'une photo dans une vue (pour la retrouver dans la grille)."""
        f = dict(request.query_params)
        ranked = ranked_ids(f)
        if ranked is not None:
            return {"index": ranked.index(pid) if pid in ranked else None}
        clause, params, order, order_params = build_filter(f)
        r = lib.db.one(f"SELECT rn FROM (SELECT p.id, ROW_NUMBER() OVER (ORDER BY {order}) - 1 rn "
                       f"FROM photo p{clause}) WHERE id=?", tuple(order_params + params + [pid]))
        return {"index": r["rn"] if r else None}

    @app.get("/api/photos/{pid}")
    def photo_detail(pid: int):
        aisvc.user_active()
        row = get_photo(pid)
        d = dict(row)
        d["ops"] = json.loads(row["ops"] or "[]")
        d["picasa_unsupported"] = json.loads(row["picasa_unsupported"] or "[]")
        d.pop("file_keywords", None)
        d["picasa_ops"] = lib.picasa_ops(pid)
        d["tags"] = [dict(r) for r in lib.db.query(
            "SELECT t.name, t.kind, pt.source FROM photo_tag pt JOIN tag t ON t.id=pt.tag_id "
            "WHERE pt.photo_id=? ORDER BY t.kind, lower(t.name)", (pid,))]
        d["albums"] = [dict(r) for r in lib.db.query(
            "SELECT a.id, a.name FROM album_photo ap JOIN album a ON a.id=ap.album_id WHERE ap.photo_id=?", (pid,))]
        ops_now = json.loads(row["ops"] or "[]")
        faces = [dict(r) for r in lib.db.query(
            "SELECT id, l, t, r, b, person, source, cluster_id, coalesce(manual, 0) manual, "
            "coalesce(ignored, 0) ignored FROM face WHERE photo_id=? ORDER BY l", (pid,))]
        ai_boxes = [f for f in faces if f["source"] == "ai"]
        for f in faces:
            # position du visage dans l'image affichée (retouches comprises) ; None : perspective
            v = map_box((f["l"], f["t"], f["r"], f["b"]), ops_now, row["width"] or 1, row["height"] or 1)
            f["view"] = [round(x, 4) for x in v] if v else None
            # visage Picasa déjà retrouvé par l'IA : affiché une seule fois
            f["duplicate"] = f["source"] == "picasa" and any(
                ai_iou((f["l"], f["t"], f["r"], f["b"]), (a["l"], a["t"], a["r"], a["b"])) > 0.3 for a in ai_boxes)
        d["faces"] = faces
        d["has_embedding"] = bool(lib.db.one("SELECT 1 FROM embedding WHERE photo_id=?", (pid,)))
        if row["camera"] is None:  # photo indexée avant la v0.5 : lecture à la demande
            cam, orient = read_camera(lib.abs_path(row))
            # Mémorisation facultative : jamais d'attente si une écriture est en cours
            lib.db.try_execute("UPDATE photo SET camera=?, exif_orientation=? WHERE id=?", (cam, orient, pid))
            d["camera"], d["exif_orientation"] = cam, orient
        # Diagnostic Picasa : section trouvée ou non, valeurs brutes
        s = config.settings
        pic = parse_folder_cached(lib.folder_path(row), s.picasa_tilt_deg_per_unit, s.picasa_rotate_clockwise)
        entry = pic.entry(row["filename"]) if pic else None
        d["plant"] = plantnet.info(pid)
        if row["media"] == "video":
            from . import media
            info = media.probe(lib.abs_path(row))
            d["video"] = {"codec": info["codec"], "audio": info["audio"], "container": info["container"],
                          "playable": media.browser_playable(info)}
        kinds = []
        if entry is not None:
            for o in entry.ops:
                k = {"crop": "crop", "straighten": "straighten", "rotate90": "rotate"}.get(o.get("op"), "filter")
                if k not in kinds:
                    kinds.append(k)
            if entry.star:
                kinds.append("star")
            if entry.caption:
                kinds.append("caption")
            if entry.keywords:
                kinds.append(f"keywords:{len(entry.keywords)}")
            if entry.faces:
                kinds.append(f"faces:{len(entry.faces)}")
            if entry.albums:
                kinds.append(f"albums:{len(entry.albums)}")
        d["picasa"] = {"ini": pic is not None, "found": entry is not None, "directives": kinds,
                       "section": next((k for k, v in pic.photos.items() if v is entry), None) if entry else None}
        return d

    # ---------------------------------------------------------------- images
    def _rendered(pid: int, size_name: str):
        row = get_photo(pid)
        size = SIZES[size_name]
        ops = json.loads(row["ops"] or "[]")
        def produce():
            t0 = time.time()
            img = render(lib.abs_path(row), ops, size, config.settings.default_f35, row["f35"], row["duration"])
            dt = time.time() - t0
            if dt > 1.5:
                log.warning("rendu lent (%s, %.1f s) : %s [%s]", size_name, dt, row["filename"],
                            ", ".join(o.get("op", "?") for o in ops) or "sans retouche")
            return img
        path = cache.get_or_render(pid, row["ops_hash"] or ops_hash(ops), size, produce)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/photos/{pid}/thumb")
    def thumb(pid: int):
        row = get_photo(pid)
        if cache.path(pid, row["ops_hash"] or "", SIZES["thumb"]).exists():
            return _rendered(pid, "thumb")
        with thumb_slots:
            return _rendered(pid, "thumb")

    @app.get("/api/photos/{pid}/preview")
    def preview(pid: int, prefetch: int = 0):
        if prefetch:
            with prefetch_slots:
                return _rendered(pid, "preview")
        aisvc.user_active()
        return _rendered(pid, "preview")

    @app.get("/api/photos/{pid}/full")
    def full(pid: int):
        """Rendu pleine résolution avec les retouches (zoom de la visionneuse)."""
        aisvc.user_active()
        return _rendered(pid, "full")

    @app.get("/api/photos/{pid}/original")
    def original(pid: int):
        return FileResponse(lib.abs_path(get_photo(pid)))

    video_locks: dict[int, threading.Lock] = {}
    video_locks_guard = threading.Lock()

    # ---------------- intensité d'amélioration rétroactive ----------------
    enhance_job = {"running": False, "done": 0, "total": 0, "result": None, "error": None}

    @app.get("/api/enhance/strength")
    def enhance_strength_info():
        n = lib.db.one("SELECT COUNT(*) n FROM photo WHERE ops LIKE '%\"autoenhance\"%'")["n"]
        return {**enhance_job, "photos": n, "strength": config.settings.enhance_strength}

    @app.post("/api/enhance/strength")
    def enhance_strength_apply(payload: dict = Body(...)):
        if enhance_job["running"]:
            raise HTTPException(409, "déjà en cours")
        strength = float(payload["strength"])

        def run():
            def prog(done, total):
                enhance_job.update(done=done, total=total)
            try:
                enhance_job["result"] = lib.apply_enhance_strength(strength, prog)
            except Exception as e:
                log.exception("intensité d'amélioration : échec")
                enhance_job["error"] = str(e)
            finally:
                enhance_job["running"] = False

        enhance_job.update(running=True, done=0, total=0, result=None, error=None)
        threading.Thread(target=run, name="enhance-strength", daemon=True).start()
        return enhance_job

    # ---------------- WordPress ----------------
    def wp_fail(e: WPError):
        raise HTTPException(400 if e.code != "auth" else 401, {"code": e.code, "detail": e.detail})

    @app.post("/api/wordpress/test")
    def wordpress_test():
        try:
            return wordpress.test()
        except WPError as e:
            wp_fail(e)

    @app.get("/api/wordpress/status")
    def wordpress_status():
        return wordpress.status()

    @app.get("/api/albums/{aid}/wordpress")
    def wordpress_album(aid: str):
        return wordpress.album_info(aid)

    @app.post("/api/albums/{aid}/wordpress")
    def wordpress_publish(aid: str, payload: dict = Body(default={})):
        try:
            return wordpress.start(aid, payload)
        except WPError as e:
            wp_fail(e)

    @app.post("/api/wordpress/stop")
    def wordpress_stop():
        wordpress.stop()
        return wordpress.status()

    # ---------------- Pl@ntNet ----------------
    @app.get("/api/plantnet")
    def plantnet_status():
        return plantnet.status()

    @app.post("/api/plantnet/start")
    def plantnet_start():
        try:
            return plantnet.start()
        except KeyRejected:
            raise HTTPException(400, "clé Pl@ntNet manquante")

    @app.post("/api/plantnet/stop")
    def plantnet_stop():
        plantnet.stop()
        return plantnet.status()

    @app.post("/api/plantnet/reset")
    def plantnet_reset(payload: dict = Body(default={})):
        return {"reset": plantnet.reset(payload.get("which", "failed"))}

    @app.get("/api/photos/{pid}/video")
    def video(pid: int):
        """Flux vidéo lisible par le navigateur : l'original (MP4 H.264...) ou une copie convertie
        en MP4 (vieux AVI, MJPEG, DivX...), mise en cache. Le fichier d'origine n'est jamais modifié."""
        from . import media
        row = get_photo(pid)
        if row["media"] != "video":
            raise HTTPException(404, "not a video")
        src = lib.abs_path(row)
        info = media.probe(src)
        if media.browser_playable(info):
            return FileResponse(src, media_type="video/mp4" if src.suffix.lower() != ".webm" else "video/webm")
        dst = cache.cache_dir / "video" / f"{pid}_{int(src.stat().st_mtime)}.mp4"
        with video_locks_guard:
            lock = video_locks.setdefault(pid, threading.Lock())
        with lock:
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                for old in dst.parent.glob(f"{pid}_*.mp4"):
                    old.unlink(missing_ok=True)
                try:
                    media.transcode(src, dst)
                except Exception as e:
                    log.exception("conversion vidéo impossible : %s", src)
                    raise HTTPException(500, f"conversion impossible : {e}")
        return FileResponse(dst, media_type="video/mp4")

    @app.get("/api/photos/{pid}/export")
    def export(pid: int):
        row = get_photo(pid)
        if row["media"] == "video":
            return FileResponse(lib.abs_path(row), filename=row["filename"])
        path = _rendered(pid, "full").path
        stem = Path(row["filename"]).stem
        return FileResponse(path, media_type="image/jpeg", filename=f"{stem}_retouche.jpg")

    @app.post("/api/photos/{pid}/render")
    def render_ops(pid: int, payload: dict = Body(...)):
        """Aperçu d'une liste d'opérations non enregistrée (outils de retouche)."""
        aisvc.user_active()
        row = get_photo(pid)
        size = int(payload.get("size", 1200))
        img = render(lib.abs_path(row), payload.get("ops", []), size, config.settings.default_f35, row["f35"],
                     row["duration"])
        return Response(to_jpeg(img, 85), media_type="image/jpeg")

    # ---------------------------------------------------------------- directives
    @app.put("/api/photos/{pid}/ops")
    def set_ops(pid: int, payload: dict = Body(...)):
        get_photo(pid)
        lib.set_ops(pid, payload.get("ops"))
        return photo_detail(pid)

    @app.put("/api/photos/{pid}/star")
    def set_star(pid: int, payload: dict = Body(...)):
        get_photo(pid)
        lib.set_star(pid, bool(payload.get("star")))
        return {"ok": True}

    @app.put("/api/photos/{pid}/caption")
    def set_caption(pid: int, payload: dict = Body(...)):
        get_photo(pid)
        lib.set_caption(pid, payload.get("caption"))
        return {"ok": True}

    @app.post("/api/photos/{pid}/perspective")
    def detect_perspective(pid: int):
        """Détecte les lignes fuyantes et propose une nouvelle liste d'opérations."""
        get_photo(pid)
        return lib.perspective_suggestion(pid)

    @app.post("/api/photos/{pid}/enhance")
    def suggest_enhance(pid: int):
        """Amélioration automatique : analyse la photo et propose une nouvelle liste d'opérations."""
        get_photo(pid)
        return lib.enhance_suggestion(pid)

    @app.post("/api/tags/assign")
    def assign_tag(payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "nom de tag vide")
        ids = resolve_ids(payload)
        lib.add_tag_bulk(ids, name, payload.get("kind", "keyword"))
        if name.lower() in {t.lower() for t in config.settings.perspective_auto_tags}:
            aisvc.enqueue_perspective(ids)
        return {"ok": True, "count": len(ids)}

    @app.post("/api/tags/unassign")
    def unassign_tag(payload: dict = Body(...)):
        ids = resolve_ids(payload)
        lib.remove_tag_bulk(ids, payload["name"])
        return {"ok": True, "count": len(ids)}

    # ---------------------------------------------------------------- albums
    @app.post("/api/albums")
    def create_album(payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "nom d'album vide")
        aid = lib.create_album(name, payload.get("description"))
        ids = resolve_ids(payload)
        if ids:
            lib.album_add(aid, ids)
        return {"id": aid}

    @app.put("/api/albums/{aid}")
    def update_album(aid: str, payload: dict = Body(...)):
        lib.update_album(aid, payload.get("name"), payload.get("description"))
        return {"ok": True}

    @app.delete("/api/albums/{aid}")
    def delete_album(aid: str):
        lib.delete_album(aid)
        return {"ok": True}

    @app.post("/api/albums/{aid}/add")
    def album_add(aid: str, payload: dict = Body(...)):
        lib.album_add(aid, resolve_ids(payload))
        return {"ok": True}

    @app.post("/api/albums/{aid}/remove")
    def album_remove(aid: str, payload: dict = Body(...)):
        lib.album_remove(aid, resolve_ids(payload))
        return {"ok": True}

    # ---------------------------------------------------------------- IA
    @app.get("/api/ai/status")
    def ai_status():
        return aisvc.status()

    @app.post("/api/ai/download")
    def ai_download(payload: dict = Body(...)):
        aisvc.models.download_async(payload["model"])
        return aisvc.models.status()

    @app.post("/api/ai/retry")
    def ai_retry():
        lib.db.execute("UPDATE job SET status='pending', error=NULL WHERE status='error'")
        aisvc.enqueue_missing()
        return aisvc.status()

    @app.get("/api/faces/clusters")
    def face_clusters(min_faces: int = 2):
        return aisvc.clusters(min_faces)

    @app.post("/api/faces/clusters/{cid}/name")
    def name_cluster(cid: int, payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "nom vide")
        return {"id": aisvc.name_cluster(cid, name)}

    @app.post("/api/faces/update")
    def faces_update(payload: dict = Body(...)):
        try:
            return aisvc.update_faces([int(i) for i in payload.get("face_ids", [])], payload.get("action", ""),
                                      payload.get("name"), payload.get("cluster"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/faces/names")
    def face_names():
        rows = lib.db.query("SELECT DISTINCT name FROM face_cluster WHERE name IS NOT NULL UNION "
                            "SELECT name FROM tag WHERE kind='person'")
        return sorted({r["name"] for r in rows}, key=str.lower)

    @app.get("/api/faces/clusters/{cid}/faces")
    def cluster_faces(cid: int, offset: int = 0, limit: int = Query(200, le=1000), sort: str = "date_desc"):
        return aisvc.cluster_faces(cid, offset, limit, sort)

    @app.get("/api/faces/clusters/{cid}/suggestions")
    def cluster_suggestions(cid: int):
        return aisvc.cluster_suggestions(cid)

    @app.post("/api/faces/clusters/{cid}/merge")
    def cluster_merge(cid: int, payload: dict = Body(...)):
        return {"id": aisvc.merge_clusters(cid, [int(o) for o in payload.get("others", [])])}

    @app.post("/api/faces/clusters/{cid}/reject")
    def cluster_reject(cid: int, payload: dict = Body(...)):
        aisvc.reject_suggestion(cid, int(payload["other"]))
        return {"ok": True}

    @app.delete("/api/faces/clusters/{cid}")
    def delete_cluster(cid: int):
        return aisvc.delete_cluster(cid)

    @app.get("/api/faces/{face_id}/thumb")
    def face_thumb(face_id: int):
        path = config.cache_dir / "faces" / f"{face_id}.jpg"
        if not path.exists():
            img = aisvc.face_thumb(face_id)
            if img is None:
                raise HTTPException(404, "visage inconnu")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(to_jpeg(img, 85))
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    # ---------------------------------------------------------------- interface
    @app.get("/")
    def index():
        # Version dans l'adresse des fichiers de l'interface : une mise à jour est prise
        # immédiatement, sans vider le cache du navigateur.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace('/static/app.css"', f'/static/app.css?v={__version__}"') \
                   .replace('/static/app.js"', f'/static/app.js?v={__version__}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.middleware("http")
    async def static_revalidate(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/") and "/vendor/" not in request.url.path:
            response.headers["Cache-Control"] = "no-cache"   # revalidation (304 si inchangé)
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
