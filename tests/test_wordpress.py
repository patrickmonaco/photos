"""Publication WordPress : blog simulé localement (API REST minimale)."""
import base64
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_app import env, pid  # noqa: F401,E402


class FakeWP(BaseHTTPRequestHandler):
    media, posts, uploads, pretty, strip_auth, home, mu_plugin = {}, {}, [], True, False, "https://blog", False

    def log_message(self, *a):
        pass

    def _send(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        p = self.path
        if p.startswith("/?rest_route="):
            from urllib.parse import parse_qs, urlparse
            return parse_qs(urlparse(p).query)["rest_route"][0]
        if not FakeWP.pretty:
            return None
        return p.split("?")[0].removeprefix("/wp-json")

    def _authed(self):
        given = self.headers.get("Authorization")
        if FakeWP.strip_auth:                      # hébergeur qui supprime l'en-tête...
            given = self.headers.get("X-Phototheque-Auth") if FakeWP.mu_plugin else None  # ...et l'extension
        ok = given == "Basic " + base64.b64encode(b"patrick:abcd efgh ijkl").decode()
        if not ok:
            if given:
                self._send(401, {"code": "incorrect_password", "message": "Mot de passe incorrect."})
            else:   # en-tête supprimé par le serveur (cas fréquent en CGI/FastCGI)
                self._send(401, {"code": "rest_not_logged_in", "message": "Vous n'êtes pas connecté."})
            return False
        assert self.headers.get("User-Agent", "").startswith("Phototheque/")
        return ok

    def do_GET(self):
        r = self._route()
        if r is None:
            body = b"<html>page</html>"
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            return self.wfile.write(body)
        if r == "/":
            return self._send(200, {"home": FakeWP.home, "authentication": {"application-passwords": {}}})
        if not self._authed():
            return
        if r == "/wp/v2/users/me":
            return self._send(200, {"id": 1, "name": "Patrick", "capabilities": {"upload_files": True, "publish_posts": True}})
        self._send(404, {"code": "rest_no_route"})

    def do_POST(self):
        r = self._route()
        if not self._authed():
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if r == "/wp/v2/media":
            assert b"\xff\xd8" in body and b'name="file"' in body
            assert b"Exif" not in body[:2000]
            mid = 100 + len(FakeWP.media)
            cap = re.search(rb'name="caption"\r\n\r\n(.*?)\r\n', body, re.S).group(1).decode()
            FakeWP.media[mid] = cap
            FakeWP.uploads.append(mid)
            url = f"http://blog/wp-content/uploads/{mid}.jpg"
            return self._send(201, {"id": mid, "source_url": url,
                                    "media_details": {"sizes": {"large": {"source_url": url.replace(".jpg", "-1024.jpg")}}}})
        m = re.fullmatch(r"/wp/v2/media/(\d+)", r or "")
        if m:
            mid = int(m.group(1))
            if mid not in FakeWP.media:
                return self._send(404, {"code": "rest_post_invalid_id"})
            FakeWP.media[mid] = json.loads(body)["caption"]
            return self._send(200, {"id": mid, "source_url": f"http://blog/wp-content/uploads/{mid}.jpg"})
        if r == "/wp/v2/posts":
            data = json.loads(body)
            pid_ = 500 + len(FakeWP.posts)
            FakeWP.posts[pid_] = data
            return self._send(201, {"id": pid_, "link": f"http://blog/?p={pid_}", "status": data["status"]})
        m = re.fullmatch(r"/wp/v2/posts/(\d+)", r or "")
        if m and int(m.group(1)) in FakeWP.posts:
            FakeWP.posts[int(m.group(1))] = json.loads(body)
            return self._send(200, {"id": int(m.group(1)), "link": f"http://blog/?p={m.group(1)}", "status": "publish"})
        self._send(404, {"code": "rest_no_route"})


@pytest.fixture()
def wp():
    FakeWP.media, FakeWP.posts, FakeWP.uploads, FakeWP.pretty, FakeWP.strip_auth = {}, {}, [], True, False
    srv = HTTPServer(("127.0.0.1", 0), FakeWP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield FakeWP, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def wait(c):
    end = time.time() + 30
    while c.get("/api/wordpress/status").json()["running"] and time.time() < end:
        time.sleep(0.05)
    return c.get("/api/wordpress/status").json()


def test_wordpress_publish(env, wp):  # noqa: F811
    c, lib, library = env
    fake, url = wp
    items = [i for i in c.get("/api/photos").json()["items"] if i.get("media") != "video"][:3]
    aid = c.post("/api/albums", json={"name": "Été à Collioure"}).json()["id"]
    c.post(f"/api/albums/{aid}/add", json={"photo_ids": [i["id"] for i in items]})
    c.put(f"/api/photos/{items[0]['id']}/caption", json={"caption": "Le port <au matin>"})

    # pas configuré
    assert c.post(f"/api/albums/{aid}/wordpress", json={}).status_code == 400
    c.put("/api/settings", json={"wordpress_url": url + "/", "wordpress_user": "patrick",
                                 "wordpress_app_password": "wrong"})
    r = c.post("/api/wordpress/test").json()
    assert not r["ok"] and r["app_passwords"] and [x["key"] for x in r["checks"]] == ["wrong_password"]
    FakeWP.strip_auth, FakeWP.home = True, "http://blog"
    r = c.post("/api/wordpress/test").json()
    assert {x["key"] for x in r["checks"]} == {"no_credentials_received"}   # http:// vs adresse de test http
    c.put("/api/settings", json={"wordpress_app_password": "abcd efgh ijkl"})
    FakeWP.mu_plugin = True                     # extension installée : l'en-tête de secours passe
    assert c.post("/api/wordpress/test").json()["ok"]
    FakeWP.strip_auth, FakeWP.mu_plugin = False, False
    c.put("/api/settings", json={"wordpress_app_password": "abcd efgh ijkl", "wordpress_max_size": 800})
    assert c.post("/api/wordpress/test").json()["name"] == "Patrick"
    info = c.get(f"/api/albums/{aid}/wordpress").json()
    assert info["photos"] == 3 and info["post"] is None

    c.post(f"/api/albums/{aid}/wordpress", json={"intro": "Trois jours\n\nau soleil", "columns": 2})
    st = wait(c)
    assert st["message"] == "done" and st["uploaded"] == 3 and st["link"] == "http://blog/?p=500"
    post = fake.posts[500]
    assert post["title"] == "Été à Collioure" and post["status"] == "publish" and post["featured_media"] == 100
    assert "<!-- wp:gallery" in post["content"] and post["content"].count("<!-- wp:image") == 3
    assert "Le port &lt;au matin&gt;" in post["content"] and "-1024.jpg" in post["content"]
    assert '"columns":2' in post["content"] and "<p>Trois jours</p>" in post["content"]
    assert fake.media[100] == "Le port <au matin>"

    # republication : pas de nouvel envoi, article mis à jour, retouche => nouvel envoi de cette photo
    assert c.put(f"/api/photos/{items[1]['id']}/ops", json={"ops": [{"op": "bw"}]}).status_code == 200
    c.post(f"/api/albums/{aid}/wordpress", json={"title": "Collioure 2026"})
    st = wait(c)
    assert st["uploaded"] == 1 and st["reused"] == 2 and st["post_id"] == 500
    assert fake.posts[500]["title"] == "Collioure 2026" and len(fake.posts) == 1
    assert c.get(f"/api/albums/{aid}/wordpress").json()["post"]["link"] == "http://blog/?p=500"

    # permaliens simples : bascule sur ?rest_route=
    fake.pretty = False
    c.post(f"/api/albums/{aid}/wordpress", json={"status": "draft"})
    st = wait(c)
    assert st["message"] == "done" and fake.posts[500]["status"] == "draft"
