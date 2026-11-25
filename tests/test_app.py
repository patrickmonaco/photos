import hashlib
import shutil
import io
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phototheque.api import create_app  # noqa: E402
from phototheque.config import Config  # noqa: E402
from tests.make_sample import build  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    lib = tmp_path / "Photos"
    build(lib)
    cfg = Config(tmp_path / "data")
    cfg.add_root(str(lib))
    app = create_app(cfg, scan_on_start=False)
    app.state.library.scan_all()
    return TestClient(app), lib, app.state.library


def pid(client, name):
    items = client.get("/api/photos", params={"q": name}).json()["items"]
    return next(i["id"] for i in items if i["filename"] == name)


def wait_scan(library, timeout=30):
    """Attend la fin d'une indexation lancée en arrière-plan par l'API."""
    import time
    time.sleep(0.05)
    end = time.time() + timeout
    while library.scan_state.get("running") and time.time() < end:
        time.sleep(0.05)


def md5(p: Path):
    return hashlib.md5(p.read_bytes()).hexdigest()


def test_navigation(env):
    c, lib, _ = env
    assert c.get("/api/status").json()["photos"] == 10
    dates = c.get("/api/dates").json()
    assert {"year": 2019, "month": 7, "day": 14, "n": 2} in dates
    assert sum(d["n"] for d in c.get("/api/folders").json()) == 10
    r = c.get("/api/photos", params={"year": 2021, "month": 12}).json()
    assert r["total"] == 2
    r = c.get("/api/photos", params={"root": str(lib), "folder": "2021", "recursive": True}).json()
    assert r["total"] == 2


def test_picasa_import(env):
    c, _, _ = env
    tags = {(t["name"], t["kind"]): t["n"] for t in c.get("/api/tags").json()}
    assert tags[("mer", "keyword")] == 1 and tags[("Marie Dupont", "person")] == 1
    albums = {a["name"]: a for a in c.get("/api/albums").json()}
    assert albums["Meilleures photos"]["n"] == 3 and albums["Meilleures photos"]["source"] == "picasa"
    assert albums["Famille"]["n"] == 2
    d1 = c.get(f"/api/photos/{pid(c, 'IMG_0001.jpg')}").json()
    assert d1["star"] == 1 and d1["ops_source"] == "picasa"
    assert [o["op"] for o in d1["ops"]] == ["straighten", "crop"]
    assert d1["ops"][1]["l"] == pytest.approx(0x1000 / 65536)
    assert len(d1["faces"]) == 1 and d1["faces"][0]["person"] == "Marie Dupont"
    d2 = c.get(f"/api/photos/{pid(c, 'IMG_0002.jpg')}").json()
    assert d2["ops"] == [{"op": "rotate90", "n": 3}, {"op": "bw"}]  # rotate(1) = quart de tour anti-horaire
    d4 = c.get(f"/api/photos/{pid(c, 'IMG_0004.jpg')}").json()
    assert d4["picasa_unsupported"] == ["redeye", "finetune2"]
    # « J'ai de la chance » -> « Améliorer » à l'intensité par défaut
    assert d4["ops"] == [{"op": "autoenhance", "strength": 1.0, "from_picasa": ["enhance"]}]


def test_rendering(env):
    c, _, _ = env
    r = c.get(f"/api/photos/{pid(c, 'IMG_0002.jpg')}/preview")
    img = Image.open(io.BytesIO(r.content))
    assert img.height > img.width  # rotate(1) -> portrait
    r = c.get(f"/api/photos/{pid(c, 'IMG_0001.jpg')}/thumb")
    assert Image.open(io.BytesIO(r.content)).size[0] <= 360


def test_edits_never_touch_photos(env):
    c, lib, _ = env
    p = lib / "2019 Vacances Bretagne" / "IMG_0001.jpg"
    ini = lib / "2019 Vacances Bretagne" / ".picasa.ini"
    before, ini_before = md5(p), md5(ini)
    i = pid(c, "IMG_0001.jpg")
    d = c.put(f"/api/photos/{i}/ops", json={"ops": [{"op": "straighten", "deg": 3}]}).json()
    assert d["ops_source"] == "user"
    side = json.loads((p.parent / ".phototheque.json").read_text(encoding="utf-8"))
    assert side["photos"]["IMG_0001.jpg"]["ops"] == [{"op": "straighten", "deg": 3}]
    assert md5(p) == before and md5(ini) == ini_before
    # retour aux retouches Picasa
    d = c.put(f"/api/photos/{i}/ops", json={"ops": None}).json()
    assert d["ops_source"] == "picasa"


def test_tags(env):
    c, _, _ = env
    i = pid(c, "IMG_0001.jpg")
    c.post("/api/tags/assign", json={"photo_ids": [i], "name": "Bretagne"})
    c.post("/api/tags/unassign", json={"photo_ids": [i], "name": "plage"})  # tag hérité de Picasa
    names = {t["name"] for t in c.get(f"/api/photos/{i}").json()["tags"]}
    assert "Bretagne" in names and "plage" not in names and "mer" in names
    c.post("/api/scan")
    env[2].scan_all()
    names = {t["name"] for t in c.get(f"/api/photos/{i}").json()["tags"]}
    assert "plage" not in names  # le masquage survit au rescan
    assert c.get("/api/photos", params={"tag": "Bretagne"}).json()["total"] == 1


def test_albums(env):
    c, _, library = env
    ids = [pid(c, "IMG_0002.jpg"), pid(c, "IMG_0004.jpg")]
    aid = c.post("/api/albums", json={"name": "Sélection", "photo_ids": ids}).json()["id"]
    assert c.get("/api/photos", params={"album": aid}).json()["total"] == 2
    picasa = next(a for a in c.get("/api/albums").json() if a["name"] == "Famille")
    c.post(f"/api/albums/{picasa['id']}/add", json={"photo_ids": [ids[0]]})
    c.put(f"/api/albums/{picasa['id']}", json={"name": "Famille élargie"})
    meilleures = next(a for a in c.get("/api/albums").json() if a["name"] == "Meilleures photos")
    c.delete(f"/api/albums/{meilleures['id']}")
    library.scan_all()
    albums = {a["name"]: a["n"] for a in c.get("/api/albums").json()}
    assert albums == {"Sélection": 2, "Famille élargie": 3}


def test_perspective(env):
    c, _, _ = env
    i = pid(c, "IMG_0003.jpg")
    r = c.post(f"/api/photos/{i}/perspective").json()
    assert r["found"] and r["estimate"]["pitch"] == pytest.approx(-14, abs=0.3)
    assert r["f35_from_exif"]
    c.put(f"/api/photos/{i}/ops", json={"ops": r["ops"]})
    assert c.get(f"/api/photos/{i}/preview").status_code == 200


def test_picasa_tilt_sign(env):
    c, _, _ = env
    d = c.get(f"/api/photos/{pid(c, 'IMG_0001.jpg')}").json()
    st = next(o for o in d["ops"] if o["op"] == "straighten")
    assert st["picasa_tilt"] == 0.2 and st["deg"] == -2.0  # tilt Picasa positif -> redressement négatif


def test_calibration_sign(env):
    from phototheque.calibrate import calibrate
    _, lib, _ = env
    r = calibrate([str(lib)])
    assert r["count"] == 3 and r["factor"] == pytest.approx(7.5, abs=0.15)


def test_settings_migration_v3(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"roots": [], "picasa_tilt_deg_per_unit": -7.5, "picasa_rotate_clockwise": True, "settings_version": 2}))
    cfg = Config(tmp_path)
    assert cfg.settings.picasa_tilt_deg_per_unit == 7.5 and cfg.settings.picasa_rotate_clockwise is True
    assert json.loads((tmp_path / "settings.json").read_text())["settings_version"] == 3


def test_autoenhance(env):
    import numpy as np
    from phototheque import enhance
    from skimage import data
    # Une photo correcte n'est presque pas modifiée
    good = Image.fromarray(data.astronaut())
    p = enhance.analyze(good)
    assert all(abs(g - 1) < 0.12 for g in p["wb"]) and abs(p["gamma"] - 1) < 0.12
    # Une photo sombre à dominante jaune est éclaircie et neutralisée
    a = np.asarray(good, dtype=np.float32) / 255
    bad = Image.fromarray((np.clip((a ** 1.6) * [1.08, 1.0, 0.78], 0, 1) * 255).astype(np.uint8))
    p = enhance.analyze(bad)
    assert p["wb"][2] > 1.05 > 0.95 > p["wb"][0] and p["gamma"] < 0.9
    fixed = np.asarray(enhance.apply(bad, p), dtype=np.float32)
    assert np.median(fixed @ [0.2126, 0.7152, 0.0722]) > np.median(np.asarray(bad, np.float32) @ [0.2126, 0.7152, 0.0722]) + 20
    # Via l'API : proposition, enregistrement, rendu
    c, lib, _ = env
    i = pid(c, "IMG_0004.jpg")
    r = c.post(f"/api/photos/{i}/enhance").json()
    assert [o["op"] for o in r["ops"]] == ["autoenhance"] and "wb" in r["ops"][0]  # remplace la version Picasa
    r["ops"][-1]["strength"] = 0.6
    d = c.put(f"/api/photos/{i}/ops", json={"ops": r["ops"]}).json()
    assert d["ops"][-1]["strength"] == 0.6
    assert c.get(f"/api/photos/{i}/preview").status_code == 200
    # Une 2e analyse remplace l'opération au lieu de l'empiler
    r2 = c.post(f"/api/photos/{i}/enhance").json()
    assert sum(o["op"] == "autoenhance" for o in r2["ops"]) == 1


def test_picasa_name_matching(env):
    c, lib, library = env
    d = lib / "Casse"
    d.mkdir()
    Image.new("RGB", (60, 40), (90, 120, 150)).save(d / "IMG_X.JPG")
    (d / ".picasa.ini").write_text("[img_x.jpg]\nrotate=rotate(2)\nfilters=tilt=1,-0.300000,0.000000;\n",
                                   encoding="utf-8")
    library.scan_all()
    det = c.get(f"/api/photos/{pid(c, 'IMG_X.JPG')}").json()
    assert det["ops"][0] == {"op": "rotate90", "n": 2}
    assert det["ops"][1]["deg"] == 3.0          # tilt Picasa -0,3 -> +3°
    assert det["picasa"] == {"ini": True, "found": True, "section": "img_x.jpg", "directives": ["rotate", "straighten"]}


def test_exclude_and_include_folder(env):
    c, lib, library = env
    root = str(lib)
    c.app.state.library.config.settings  # noqa: B018
    c.post("/api/folders/exclude", json={"root": root, "folder": "2021"})
    wait_scan(library)
    library.scan_all()
    assert c.get("/api/photos", params={"year": 2021}).json()["total"] == 0
    assert (lib / "2021" / "Noël" / "IMG_2001.jpg").exists()  # rien n'est supprimé sur le disque
    excluded = c.get("/api/status").json()["settings"]["excluded_folders"]
    assert excluded == [str(lib / "2021")]
    c.put("/api/settings", json={"include_folder": excluded[0]})
    wait_scan(library)
    library.scan_all()
    assert c.get("/api/photos", params={"year": 2021}).json()["total"] == 2


def test_targeted_rescan_and_camera(env):
    from PIL import Image as PImage
    c, lib, library = env
    folder = lib / "Divers"
    img = PImage.new("RGB", (80, 60), (200, 100, 50))
    exif = PImage.Exif()
    exif[271], exif[272] = "Canon", "Canon EOS 5D Mark IV"
    img.save(folder / "nouvelle.jpg", exif=exif)
    assert library.scan_folders([str(folder)])
    det = c.get(f"/api/photos/{pid(c, 'nouvelle.jpg')}").json()
    assert det["camera"] == "Canon EOS 5D Mark IV"
    (folder / "nouvelle.jpg").unlink()
    library.scan_folders([str(folder)])
    assert c.get("/api/photos", params={"q": "nouvelle"}).json()["total"] == 0
    shutil.rmtree(lib / "2020 Montagne")
    library.scan_folders([str(lib / "2020 Montagne")])
    assert c.get("/api/photos", params={"year": 2020}).json()["total"] == 0


def test_folder_watcher(env):
    import time
    from phototheque.watcher import FolderWatcher
    c, lib, library = env
    scanned = []
    w = FolderWatcher(library, on_scanned=lambda: scanned.append(1))
    assert w.start()
    try:
        time.sleep(0.5)
        Image.new("RGB", (64, 48), (10, 200, 10)).save(lib / "Divers" / "ajout_auto.jpg")
        for _ in range(40):
            time.sleep(0.25)
            if c.get("/api/photos", params={"q": "ajout_auto"}).json()["total"]:
                break
        assert c.get("/api/photos", params={"q": "ajout_auto"}).json()["total"] == 1 and scanned
    finally:
        w.stop()


def test_picasa_lucky_follows_default_strength(env):
    c, _, library = env
    i = pid(c, "IMG_0004.jpg")
    assert c.get(f"/api/photos/{i}/preview").status_code == 200   # rendu avec analyse à la volée
    c.put("/api/settings", json={"enhance_strength": 0.6})
    library.scan_all()
    d = c.get(f"/api/photos/{i}").json()
    assert d["ops"][0]["strength"] == 0.6 and d["ops_source"] == "picasa"
    assert d["picasa_ops"][0]["op"] == "autoenhance"
    assert c.get(f"/api/photos/{i}/thumb").status_code == 200


def test_picasa_filters_conversion(env):
    c, lib, library = env
    d = lib / "Filtres"
    d.mkdir()
    for n in ("a.jpg", "b.jpg"):
        Image.new("RGB", (80, 60), (120, 90, 60)).save(d / n)
    (d / ".picasa.ini").write_text(
        "[a.jpg]\nfilters=autolight=1;unsharp2=1;warm=1;autocolor=1;\n"
        "[b.jpg]\nfilters=bw=1;sepia=1;\n", encoding="utf-8")
    library.scan_all()
    a = c.get(f"/api/photos/{pid(c, 'a.jpg')}").json()["ops"]
    assert a == [{"op": "autoenhance", "strength": 1.0, "from_picasa": ["autocontrast", "warm", "autocolor"]},
                 {"op": "sharpen"}]  # une seule amélioration, la netteté est conservée
    b = c.get(f"/api/photos/{pid(c, 'b.jpg')}").json()["ops"]
    assert b == [{"op": "bw"}, {"op": "sepia"}]


def test_cache_purge(tmp_path):
    import os
    import time
    from phototheque.render import RenderCache
    cache = RenderCache(tmp_path, max_bytes=10_000)
    blob = Image.new("RGB", (8, 8))
    now = time.time()
    for pid in range(1, 21):  # 20 miniatures + 20 aperçus d'environ 600 octets
        for size in (360, 1600):
            p = cache.get_or_render(pid, "h", size, lambda: blob)
            os.utime(p, (now - pid, now - pid))
    before = cache.usage()
    assert before["thumb"]["files"] == 20 and before["preview"]["files"] == 20
    r = cache.purge()
    after = cache.usage()
    assert after["total"] <= 10_000 and r["removed"] > 0
    assert after["thumb"]["files"] == 20 or after["preview"]["files"] == 0   # aperçus supprimés en premier
    for pid in range(1, 41):  # rendus pleine résolution : seuls les 30 derniers sont gardés
        cache.get_or_render(pid, "h", 0, lambda: blob)
    assert cache.usage()["full"]["files"] == RenderCache.FULL_KEEP
    assert cache.clear("previews") >= RenderCache.FULL_KEEP


def test_quick_rescan_skips_unchanged_folders(env):
    c, lib, library = env
    library.scan_all()
    st = library.scan_state
    assert st["skipped"] >= 5 and st["done"] == 10      # tous les dossiers sautés, photos conservées
    albums = {a["name"]: a["n"] for a in c.get("/api/albums").json()}
    assert albums == {"Meilleures photos": 3, "Famille": 2}   # albums Picasa des dossiers sautés conservés
    Image.new("RGB", (40, 30)).save(lib / "Divers" / "nouveau.jpg")   # modifie la date du dossier
    library.scan_all()
    assert c.get("/api/photos", params={"q": "nouveau"}).json()["total"] == 1
    library.scan_all(deep=True)
    assert library.scan_state["skipped"] == 0


def test_select_all_resolved_by_server(env):
    c, lib, _ = env
    excl = pid(c, "IMG_0002.jpg")
    sel = {"selection": {"query": {"year": "2019"}, "exclude": [excl]}}
    r = c.post("/api/tags/assign", json={**sel, "name": "Bretagne 2019"}).json()
    assert r["count"] == 3                                   # 4 photos de 2019 moins 1 exclue
    assert c.get("/api/photos", params={"tag": "Bretagne 2019"}).json()["total"] == 3
    aid = c.post("/api/albums", json={"name": "Tout 2019", **sel}).json()["id"]
    assert c.get("/api/photos", params={"album": aid}).json()["total"] == 3
    c.post("/api/tags/unassign", json={"selection": {"query": {"tag": "Bretagne 2019"}}, "name": "Bretagne 2019"})
    assert c.get("/api/photos", params={"tag": "Bretagne 2019"}).json()["total"] == 0


def test_recursive_folder_view(env):
    c, lib, _ = env
    root = str(lib)
    flat = c.get("/api/photos", params={"root": root, "folder": ""}).json()["total"]
    rec = c.get("/api/photos", params={"root": root, "folder": "", "recursive": "true"}).json()
    assert flat == 0 and rec["total"] == 10          # racine sans photo directe : tout vient des sous-dossiers
    folders = [i["folder"] for i in rec["items"]]
    assert folders == sorted(folders)                 # regroupées par sous-dossier
    sub = c.get("/api/photos", params={"root": root, "folder": "2021", "recursive": "true"}).json()
    assert sub["total"] == 2 and {i["folder"] for i in sub["items"]} == {"2021/Noël"}


MEDIA = Path("/tmp/mediatest")


@pytest.mark.skipif(not (MEDIA / "clip.mp4").exists(), reason="fichiers vidéo de test absents")
def test_videos_and_heic(env):
    c, lib, library = env
    d = lib / "Vidéos"
    d.mkdir()
    for f in ("clip.mp4", "old.avi", "rotated90.mp4", "photo.heic"):
        shutil.copy(MEDIA / f, d / f)
    before = {f: md5(d / f) for f in ("clip.mp4", "old.avi")}
    library.scan_all()
    items = c.get("/api/photos", params={"root": str(lib), "folder": "Vidéos"}).json()["items"]
    by = {i["filename"]: i for i in items}
    assert by["clip.mp4"]["media"] == "video" and by["clip.mp4"]["duration"] > 2
    assert by["clip.mp4"]["taken_at"].startswith("2018-08-05")
    assert by["photo.heic"]["media"] == "image"
    rot = by["rotated90.mp4"]
    assert (rot["width"], rot["height"]) == (640, 480)   # codée 480x640 + rotation : dimensions affichées
    im = Image.open(io.BytesIO(c.get(f"/api/photos/{rot['id']}/thumb").content))
    assert im.width > im.height                   # vignette tournée comme à la lecture
    # Vignettes (une image de la vidéo) et aperçu HEIC
    for name in ("clip.mp4", "old.avi", "photo.heic"):
        r = c.get(f"/api/photos/{by[name]['id']}/thumb")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    # MP4 servi tel quel (avec Range), AVI converti en MP4 en cache
    vid = by["clip.mp4"]["id"]
    r = c.get(f"/api/photos/{vid}/video", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    det = c.get(f"/api/photos/{by['old.avi']['id']}").json()
    assert det["video"]["playable"] is False
    r = c.get(f"/api/photos/{by['old.avi']['id']}/video")
    assert r.status_code == 200 and r.content[4:8] == b"ftyp"
    assert c.get("/api/cache").json()["video"]["files"] == 1
    # Export d'une vidéo = l'original
    assert c.get(f"/api/photos/{vid}/export").content == (d / "clip.mp4").read_bytes()
    assert {f: md5(d / f) for f in before} == before


def test_enhance_strength_retroactive(env):
    c, lib, library = env
    items = [i for i in c.get("/api/photos").json()["items"] if i.get("media") != "video"]
    target = items[0]
    det = c.get(f"/api/photos/{target['id']}").json()
    ops = [o for o in det["ops"] if o["op"] != "autoenhance"] + [{"op": "autoenhance", "strength": 1.0, "wb": [1, 1, 1],
                                                                 "black": 0, "white": 255, "gamma": 1, "clahe": 1,
                                                                 "vibrance": 0}]
    assert c.put(f"/api/photos/{target['id']}/ops", json={"ops": ops}).status_code == 200
    info = c.get("/api/enhance/strength").json()
    assert info["photos"] >= 1
    old_hash = c.get(f"/api/photos/{target['id']}").json()["ops_hash"]
    c.post("/api/enhance/strength", json={"strength": 0.5})
    for _ in range(200):
        st = c.get("/api/enhance/strength").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["result"]["photos"] == info["photos"] and st["strength"] == 0.5
    rows = library.db.query("SELECT ops FROM photo WHERE ops LIKE '%autoenhance%'")
    strengths = {o["strength"] for r in rows for o in json.loads(r["ops"]) if o["op"] == "autoenhance"}
    assert strengths == {0.5}                        # main et Picasa
    det = c.get(f"/api/photos/{target['id']}").json()
    assert det["ops_hash"] != old_hash               # vignette recalculée
    side = json.loads((lib / det["folder"] / ".phototheque.json").read_text(encoding="utf-8"))
    assert [o for o in side["photos"][det["filename"]]["ops"] if o["op"] == "autoenhance"][0]["strength"] == 0.5
