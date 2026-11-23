"""Publication d'un album vers un blog WordPress, par l'API REST.

Authentification : un « mot de passe d'application » WordPress (Profil › Mots de passe
d'application, WordPress 5.6 et plus), envoyé en HTTP Basic sur HTTPS. Le mot de passe du compte
n'est jamais utilisé.

Déroulement :
1. chaque photo de l'album (les vidéos sont ignorées) est rendue avec ses retouches, réduite à la
   taille choisie et envoyée dans la médiathèque (/wp/v2/media), avec sa légende ;
2. un article est créé (/wp/v2/posts) avec un bloc Galerie qui reprend les photos dans l'ordre de
   l'album, la première servant d'image mise en avant.

Une photo déjà envoyée et non retouchée depuis n'est pas renvoyée ; republier un album met à jour
son article au lieu d'en créer un second. Le JPEG envoyé est recalculé sans les métadonnées EXIF :
ni la position GPS ni l'appareil ne sont publiés.
"""
from __future__ import annotations

import base64
import html
import io
import json
import logging
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import __version__
from .net import multipart, open_url

log = logging.getLogger("phototheque")
USER_AGENT = f"Phototheque/{__version__} (+WordPress REST)"


class WPError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "photo"


class WordPressClient:
    def __init__(self, url: str, user: str, app_password: str):
        url = url.strip().rstrip("/")
        if url and not re.match(r"^https?://", url):
            url = "https://" + url
        self.url = url
        token = base64.b64encode(f"{user.strip()}:{app_password.strip()}".encode()).decode()
        self.auth = f"Basic {token}"
        self.pretty = True          # /wp-json/… ; sinon ?rest_route=/… (permaliens simples)

    def _endpoint(self, path: str, query: dict | None = None) -> str:
        if self.pretty:
            u = f"{self.url}/wp-json{path}"
            return u + ("?" + urllib.parse.urlencode(query) if query else "")
        return f"{self.url}/?" + urllib.parse.urlencode({"rest_route": path, **(query or {})})

    def call(self, method: str, path: str, *, query: dict | None = None, json_body=None,
             body: bytes | None = None, ctype: str | None = None, timeout: int = 60, auth: bool = True):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth:
            headers["Authorization"] = self.auth
            # Copie lue par l'extension wordpress/phototheque-auth.php quand l'hébergeur supprime
            # l'en-tête Authorization (PHP en CGI/FastCGI) ; sans l'extension, WordPress l'ignore.
            headers["X-Phototheque-Auth"] = self.auth
        if json_body is not None:
            body, ctype = json.dumps(json_body).encode(), "application/json"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self._endpoint(path, query), data=body, method=method, headers=headers)
        try:
            with open_url(req, timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            code = err.get("code") or f"http_{e.code}"
            if e.code in (401, 403):
                # Le code WordPress dit pourquoi : identifiants faux, aucun identifiant reçu...
                raise WPError("auth", f"{code}|{err.get('message') or ''}") from e
            if e.code == 404 and code in ("http_404", "rest_no_route"):
                raise WPError("not_found", err.get("message") or "") from e
            raise WPError(code, err.get("message") or str(e)) from e
        except urllib.error.URLError as e:
            raise WPError("network", str(e.reason)) from e
        try:
            return json.loads(raw)
        except ValueError as e:
            # Une page HTML à la place du JSON : souvent l'API REST désactivée ou un pare-feu.
            raise WPError("not_json", raw[:200].decode("utf-8", "replace")) from e

    def me(self) -> dict:
        """Vérifie l'adresse et les identifiants ; bascule sur ?rest_route= si /wp-json n'existe pas."""
        if not self.url:
            raise WPError("config", "adresse du blog manquante")
        try:
            return self.call("GET", "/wp/v2/users/me", query={"context": "edit"}, timeout=20)
        except WPError as e:
            if e.code not in ("not_found", "not_json"):
                raise
            self.pretty = False
            return self.call("GET", "/wp/v2/users/me", query={"context": "edit"}, timeout=20)


class WordPressService:
    def __init__(self, lib):
        self.lib = lib
        self.db = lib.db
        self.config = lib.config
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = {"running": False, "album": None, "done": 0, "total": 0, "uploaded": 0, "reused": 0,
                      "skipped_videos": 0, "message": None, "detail": None, "link": None, "post_id": None}

    # ---------------- configuration ----------------
    def client(self) -> WordPressClient:
        s = self.config.settings
        if not (s.wordpress_url.strip() and s.wordpress_user.strip() and s.wordpress_app_password.strip()):
            raise WPError("config", "adresse, utilisateur ou mot de passe d'application manquant")
        return WordPressClient(s.wordpress_url, s.wordpress_user, s.wordpress_app_password)

    def site(self) -> str:
        return self.config.settings.wordpress_url.strip().rstrip("/").lower()

    def test(self) -> dict:
        """Teste la connexion et explique un refus : la cause la plus fréquente n'est pas le mot de passe."""
        client = self.client()
        out = {"ok": False, "checks": []}
        # 1. Index public de l'API (sans identifiants) : adresse du site et mots de passe d'application disponibles ?
        index = None
        for pretty in (True, False):
            client.pretty = pretty
            try:
                index = client.call("GET", "/", timeout=20, auth=False)
                break
            except WPError as e:
                if e.code not in ("not_found", "not_json") or not pretty:
                    out["checks"].append({"key": "index_error", "code": e.code, "detail": e.detail[:200]})
        if index is not None:
            home = str(index.get("home") or index.get("url") or "")
            out["home"] = home
            if home.startswith("http://") and client.url.startswith("https://"):
                out["checks"].append({"key": "home_http", "home": home})
            auth = index.get("authentication")
            out["app_passwords"] = isinstance(auth, dict) and "application-passwords" in auth
            if not out["app_passwords"]:
                out["checks"].append({"key": "app_passwords_unavailable"})
        # 2. Appel authentifié
        try:
            me = client.call("GET", "/wp/v2/users/me", query={"context": "edit"}, timeout=20)
        except WPError as e:
            code, _, message = e.detail.partition("|")
            if e.code == "auth":
                key = {"rest_not_logged_in": "no_credentials_received", "incorrect_password": "wrong_password",
                       "invalid_username": "wrong_user", "invalid_email": "wrong_user",
                       "application_passwords_disabled": "app_passwords_disabled",
                       "application_passwords_disabled_for_user": "app_passwords_disabled"}.get(code, "auth_other")
                out["checks"].append({"key": key, "code": code, "detail": message[:200]})
                if key == "no_credentials_received":
                    out["checks"] = [c for c in out["checks"] if c["key"] != key] + self._diag(client)
            else:
                out["checks"].append({"key": "error", "code": e.code, "detail": e.detail[:200]})
            log.warning("WordPress : test refusé : %s", out["checks"])
            return out
        caps = me.get("capabilities") or {}
        out.update(ok=True, name=me.get("name"), can_upload=bool(caps.get("upload_files")),
                   can_publish=bool(caps.get("publish_posts")))
        return out

    @staticmethod
    def _diag(client: WordPressClient) -> list[dict]:
        """Interroge l'extension phototheque-auth.php : qu'est-ce que le serveur a transmis à PHP ?"""
        try:
            d = client.call("GET", "/phototheque/v1/diag", timeout=20)
        except WPError as e:
            if e.code == "not_found":
                return [{"key": "plugin_missing"}]
            return [{"key": "no_credentials_received", "code": "rest_not_logged_in"}]
        log.warning("WordPress : diagnostic de l'extension : %s", d)
        if not d.get("fallback_header") and not d.get("authorization_header"):
            return [{"key": "headers_stripped", "detail": json.dumps(d)}]
        if d.get("password_check") and d["password_check"] != "ok":
            return [{"key": "auth_other", "code": d["password_check"], "detail": d["password_check"]}]
        return [{"key": "diag", "detail": json.dumps(d)}]

    def album_info(self, album_id: str) -> dict:
        n = self.db.one("SELECT COUNT(*) n FROM album_photo ap JOIN photo p ON p.id=ap.photo_id "
                        "WHERE ap.album_id=? AND coalesce(p.media, 'image') != 'video'", (album_id,))["n"]
        v = self.db.one("SELECT COUNT(*) n FROM album_photo ap JOIN photo p ON p.id=ap.photo_id "
                        "WHERE ap.album_id=? AND p.media = 'video'", (album_id,))["n"]
        post = self.db.one("SELECT post_id, link, status, at FROM wp_post WHERE album_id=? AND site=?",
                           (album_id, self.site()))
        return {"photos": n, "videos": v, "post": dict(post) if post else None,
                "configured": bool(self.config.settings.wordpress_url.strip()
                                   and self.config.settings.wordpress_app_password.strip())}

    def status(self) -> dict:
        return dict(self.state)

    # ---------------- publication ----------------
    def start(self, album_id: str, options: dict) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise WPError("busy", "une publication est déjà en cours")
            if not self.db.one("SELECT 1 FROM album WHERE id=?", (album_id,)):
                raise WPError("album", "album introuvable")
            client = self.client()
            self._stop.clear()
            self.state.update(running=True, album=album_id, done=0, total=0, uploaded=0, reused=0,
                              skipped_videos=0, message=None, detail=None, link=None, post_id=None)
            self._thread = threading.Thread(target=self._run, args=(client, album_id, options),
                                            name="wordpress", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, client: WordPressClient, album_id: str, opt: dict) -> None:
        try:
            client.me()
            self._publish(client, album_id, opt)
        except WPError as e:
            log.warning("WordPress : %s", e)
            self.state.update(message=e.code, detail=e.detail)
        except Exception as e:
            log.exception("WordPress : publication interrompue")
            self.state.update(message="error", detail=str(e))
        finally:
            self.state["running"] = False

    def _image(self, row, max_size: int, quality: int) -> bytes:
        """Photo telle qu'affichée (toutes ses retouches), côté le plus long ≤ max_size, sans EXIF."""
        from .render import render
        ops = json.loads(row["ops"] or "[]")
        img = render(self.lib.abs_path(row), ops, max_size, self.config.settings.default_f35, row["f35"],
                     row["duration"])
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()

    def _publish(self, client: WordPressClient, album_id: str, opt: dict) -> None:
        s = self.config.settings
        site = self.site()
        album = self.db.one("SELECT name, description FROM album WHERE id=?", (album_id,))
        rows = self.db.query("SELECT p.* FROM album_photo ap JOIN photo p ON p.id=ap.photo_id "
                             "WHERE ap.album_id=? ORDER BY ap.position, p.taken_at", (album_id,))
        photos = [r for r in rows if r["media"] != "video"]
        self.state["skipped_videos"] = len(rows) - len(photos)
        self.state["total"] = len(photos)
        if not photos:
            raise WPError("empty", "aucune photo à publier dans cet album")
        max_size = int(opt.get("max_size") or s.wordpress_max_size)
        quality = int(opt.get("quality") or s.wordpress_quality)
        captions = bool(opt.get("captions", True))
        base = slug(album["name"])
        items = []
        for i, row in enumerate(photos, 1):
            if self._stop.is_set():
                raise WPError("stopped")
            caption = (row["caption"] or "").strip() if captions else ""
            known = self.db.one("SELECT media_id, url FROM wp_media WHERE photo_id=? AND site=? AND ops_hash=? "
                                "AND size=?", (row["id"], site, row["ops_hash"] or "", max_size))
            media = None
            if known:
                try:   # toujours présente dans la médiathèque ? on remet la légende à jour au passage
                    media = client.call("POST", f"/wp/v2/media/{known['media_id']}",
                                        json_body={"caption": caption, "alt_text": caption})
                    self.state["reused"] += 1
                except WPError as e:
                    if e.code not in ("not_found", "rest_post_invalid_id"):
                        raise
                    media = None
            if media is None:
                data = self._image(row, max_size, quality)
                fname = f"{base}-{i:03d}.jpg"
                body, ctype = multipart(
                    [("title", caption or row["filename"].rsplit(".", 1)[0]), ("caption", caption),
                     ("alt_text", caption)],
                    [("file", fname, data, "image/jpeg")])
                media = client.call("POST", "/wp/v2/media", body=body, ctype=ctype, timeout=180)
                self.state["uploaded"] += 1
            sizes = (media.get("media_details") or {}).get("sizes") or {}
            url = (sizes.get("large") or {}).get("source_url") or media.get("source_url")
            self.db.execute("INSERT OR REPLACE INTO wp_media (photo_id, site, ops_hash, size, media_id, url, at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (row["id"], site, row["ops_hash"] or "", max_size, media["id"], url,
                             datetime.now().isoformat(timespec="seconds")))
            items.append({"id": media["id"], "url": url, "full": media.get("source_url") or url, "caption": caption})
            self.state["done"] = i

        content = gallery_content(opt.get("intro") or album["description"] or "", items,
                                  int(opt.get("columns") or 3))
        post = {"title": opt.get("title") or album["name"], "content": content,
                "status": opt.get("status") or s.wordpress_status, "featured_media": items[0]["id"]}
        existing = self.db.one("SELECT post_id FROM wp_post WHERE album_id=? AND site=?", (album_id, site))
        result = None
        if existing and opt.get("update", True):
            try:
                result = client.call("POST", f"/wp/v2/posts/{existing['post_id']}", json_body=post)
            except WPError as e:
                if e.code not in ("not_found", "rest_post_invalid_id"):
                    raise
        if result is None:
            result = client.call("POST", "/wp/v2/posts", json_body=post)
        self.db.execute("INSERT OR REPLACE INTO wp_post (album_id, site, post_id, link, status, at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (album_id, site, result["id"], result.get("link"), result.get("status"),
                         datetime.now().isoformat(timespec="seconds")))
        self.state.update(message="done", link=result.get("link"), post_id=result["id"])
        log.info("WordPress : album %s publié (%s photos, %s envoyées) : %s", album_id, len(items),
                 self.state["uploaded"], result.get("link"))


def gallery_content(intro: str, items: list[dict], columns: int = 3) -> str:
    """Contenu Gutenberg : un paragraphe d'introduction puis un bloc Galerie (images imbriquées)."""
    out = []
    for para in [p.strip() for p in intro.split("\n\n") if p.strip()]:
        out.append(f"<!-- wp:paragraph -->\n<p>{html.escape(para).replace(chr(10), '<br>')}</p>\n<!-- /wp:paragraph -->")
    columns = max(1, min(columns, 8))
    out.append(f'<!-- wp:gallery {{"columns":{columns},"linkTo":"media"}} -->\n'
               f'<figure class="wp-block-gallery has-nested-images columns-{columns} is-cropped">')
    for it in items:
        cap = html.escape(it["caption"])
        out.append(f'<!-- wp:image {{"id":{it["id"]},"sizeSlug":"large","linkDestination":"media"}} -->\n'
                   f'<figure class="wp-block-image size-large"><a href="{html.escape(it.get("full") or it["url"])}">'
                   f'<img src="{html.escape(it["url"])}" alt="{cap}" class="wp-image-{it["id"]}"/></a>'
                   + (f'<figcaption class="wp-element-caption">{cap}</figcaption>' if cap else "")
                   + "</figure>\n<!-- /wp:image -->")
    out.append("</figure>\n<!-- /wp:gallery -->")
    return "\n\n".join(out)
