"""Pl@ntNet : service simulé localement (aucun appel réel, aucun quota consommé)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phototheque import plantnet  # noqa: E402
from tests.test_app import env, pid  # noqa: F401,E402  (fixture partagée)


class FakePlantNet(BaseHTTPRequestHandler):
    calls = []
    remaining = 500
    mode = "ok"

    def log_message(self, *a):
        pass

    def _send(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "api-key=BAD" in self.path:
            return self._send(401, {"message": "Invalid API key"})
        self._send(200, {"day": "x", "quota": {"identify": {"count": 500 - FakePlantNet.remaining,
                                                             "total": 500, "remaining": FakePlantNet.remaining}}})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        FakePlantNet.calls.append((self.path, body))
        assert b'name="images"' in body and b"\xff\xd8" in body and b'name="organs"' in body
        if FakePlantNet.remaining <= 0:
            return self._send(429, {"message": "Too Many Requests"})
        FakePlantNet.remaining -= 1
        if len(FakePlantNet.calls) == 2:
            return self._send(404, {"message": "Species not found"})
        self._send(200, {"bestMatch": "Rosa canina L.", "remainingIdentificationRequests": FakePlantNet.remaining,
                         "results": [{"score": 0.83, "species": {
                             "scientificNameWithoutAuthor": "Rosa canina", "commonNames": ["églantier", "rosier des chiens"],
                             "family": {"scientificNameWithoutAuthor": "Rosaceae"}}}]})


@pytest.fixture()
def fake():
    srv = HTTPServer(("127.0.0.1", 0), FakePlantNet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = plantnet.API_URL
    plantnet.API_URL = f"http://127.0.0.1:{srv.server_port}"
    FakePlantNet.calls, FakePlantNet.remaining = [], 500
    yield FakePlantNet
    plantnet.API_URL = old
    srv.shutdown()


def wait(c):
    end = time.time() + 30
    while c.get("/api/plantnet").json()["running"] and time.time() < end:
        time.sleep(0.05)
    return c.get("/api/plantnet").json()


def test_plantnet(env, fake):  # noqa: F811
    c, lib, library = env
    items = c.get("/api/photos").json()["items"]
    plain = [i for i in items if not i["caption"]]
    a, b, d = plain[0]["id"], plain[1]["id"], plain[2]["id"]
    with_caption = plain[3]["id"]
    c.put(f"/api/photos/{with_caption}/caption", json={"caption": "Mon jardin"})
    c.post("/api/tags/assign", json={"photo_ids": [a, b, with_caption], "name": "Fleur"})
    c.post("/api/tags/assign", json={"photo_ids": [d], "name": "arbre"})

    # sans clé : refus
    assert c.post("/api/plantnet/start").status_code == 400
    c.put("/api/settings", json={"plantnet_key": " KEY ", "plantnet_daily_max": 2})
    st = c.get("/api/plantnet").json()
    assert st["key"] and st["pending"] == 3          # la photo déjà légendée est ignorée
    assert st["quota"]["left"] == 2

    st = wait(c) if c.post("/api/plantnet/start").status_code == 200 else None
    assert len(fake.calls) == 2                        # plafond journalier local respecté
    assert st["identified"] == 1 and st["notfound"] == 1 and st["message"] == "quota"
    assert "api-key=KEY" in fake.calls[0][0] and "lang=fr" in fake.calls[0][0]
    first = c.get(f"/api/photos/{a if True else b}").json()
    caps = {c.get(f"/api/photos/{x}").json()["caption"] for x in (a, b, d)}
    assert "Églantier (Rosa canina)" in caps
    assert c.get(f"/api/photos/{with_caption}").json()["caption"] != "Églantier (Rosa canina)"
    # légende enregistrée dans les directives, pas dans la photo
    sidecars = list(lib.rglob(".phototheque.json"))
    assert any("Rosa canina" in p.read_text(encoding="utf-8") for p in sidecars)
    assert first["plant"] is None or first["plant"]["species"] in ("Rosa canina", None)

    # le lendemain (nouveau jour UTC) : le compteur repart, il reste une photo
    q = json.loads((library.config.data_dir / "plantnet.json").read_text())
    q["day"] = "2000-01-01"
    (library.config.data_dir / "plantnet.json").write_text(json.dumps(q))
    c.put("/api/settings", json={"plantnet_daily_max": 500})
    st = c.get("/api/plantnet").json()
    assert st["quota"]["used"] == 0 and st["pending"] == 1
    st = (c.post("/api/plantnet/start"), wait(c))[1]
    assert st["identified"] == 1 and st["pending"] == 0

    # quota épuisé côté Pl@ntNet (429) : arrêt propre
    c.post("/api/plantnet/reset", json={"which": "all"})
    for x in (a, b, d):
        c.put(f"/api/photos/{x}/caption", json={"caption": ""})
    fake.remaining = 0
    st = (c.post("/api/plantnet/start"), wait(c))[1]
    assert st["message"] == "quota" and st["quota"]["left"] == 0

    # clé refusée
    c.put("/api/settings", json={"plantnet_key": "BAD"})
    st = (c.post("/api/plantnet/start"), wait(c))[1]
    assert st["message"] == "key"
