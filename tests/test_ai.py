"""Tests de la reconnaissance d'image et des traitements automatiques.

- Visages : vrais modèles InsightFace si présents dans PHOTOTHEQUE_TEST_MODELS (sinon test ignoré).
- CLIP : modèle ONNX factice de même interface (vérifie la chaîne complète, pas la qualité).
"""
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phototheque.api import create_app  # noqa: E402
from phototheque.config import Config  # noqa: E402
from tests.make_sample import build  # noqa: E402

FACE_MODELS = Path(os.environ.get("PHOTOTHEQUE_TEST_MODELS", "/tmp/models/faces"))


def make_fake_clip(target: Path) -> None:
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers
    target.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    wv = numpy_helper.from_array(rng.standard_normal((3, 512)).astype(np.float32), "W")
    g = helper.make_graph(
        [helper.make_node("ReduceMean", ["pixel_values"], ["m"], axes=[2, 3], keepdims=0),
         helper.make_node("MatMul", ["m", "W"], ["image_embeds"])], "vision",
        [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, ["n", 3, 224, 224])],
        [helper.make_tensor_value_info("image_embeds", TensorProto.FLOAT, ["n", 512])], [wv])
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), target / "vision_model.onnx")
    wt = numpy_helper.from_array(rng.standard_normal((77, 512)).astype(np.float32), "W")
    g = helper.make_graph(
        [helper.make_node("Cast", ["input_ids"], ["f"], to=TensorProto.FLOAT),
         helper.make_node("MatMul", ["f", "W"], ["text_embeds"])], "text",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["n", 77])],
        [helper.make_tensor_value_info("text_embeds", TensorProto.FLOAT, ["n", 512])], [wt])
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), target / "text_model.onnx")
    words = ["<|endoftext|>", "[UNK]", "a", "photo", "of"] + [f"w{i}" for i in range(50)]
    tok = Tokenizer(models.WordLevel({w: i for i, w in enumerate(words)}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(target / "tokenizer.json"))


def rect64(l, t, r, b):
    return "".join(f"{min(65535, int(v * 65536)):04x}" for v in (l, t, r, b))


@pytest.fixture()
def env(tmp_path):
    from skimage import data
    lib = tmp_path / "Photos"
    build(lib)
    portraits = lib / "Portraits"
    portraits.mkdir()
    astro = Image.fromarray(data.astronaut())
    astro.save(portraits / "astro1.jpg")
    astro.resize((400, 400)).save(portraits / "astro2.jpg")
    (portraits / ".picasa.ini").write_text(
        "[Contacts2]\nabc123=Eileen Collins;;\n"
        f"[astro1.jpg]\nfaces=rect64({rect64(0.35, 0.11, 0.53, 0.35)}),abc123\n", encoding="utf-8")
    cfg = Config(tmp_path / "data")
    cfg.add_root(str(lib))
    app = create_app(cfg, scan_on_start=False)
    app.state.library.scan_all()
    return TestClient(app), lib, app.state.ai, cfg


def pid(client, name):
    items = client.get("/api/photos", params={"q": name}).json()["items"]
    return next(i["id"] for i in items if i["filename"] == name)


def test_perspective_auto_by_tag(env):
    c, lib, svc, _ = env
    c.put("/api/settings", json={"perspective_auto_tags": "Architecture, façade"})
    svc.process_pending()
    i = pid(c, "IMG_0003.jpg")
    d = c.get(f"/api/photos/{i}").json()
    persp = next(o for o in d["ops"] if o["op"] == "perspective")
    assert persp["pitch"] == pytest.approx(-14, abs=0.3) and d["ops_source"] == "user"
    side = json.loads((lib / "2019 Vacances Bretagne" / ".phototheque.json").read_text(encoding="utf-8"))
    assert "auto_perspective" in side["photos"]["IMG_0003.jpg"]
    # L'utilisateur retire la correction : elle n'est pas réappliquée.
    c.put(f"/api/photos/{i}/ops", json={"ops": []})
    svc.enqueue_missing(); svc.process_pending()
    assert c.get(f"/api/photos/{i}").json()["ops"] == []
    # Nouveau tag sur une autre photo -> correction automatique demandée
    j = pid(c, "IMG_0004.jpg")
    c.post("/api/tags/assign", json={"photo_ids": [j], "name": "façade"})
    svc.process_pending()
    assert c.get("/api/ai/status").json()["jobs"]["perspective_auto"]["done"] == 2


def test_clip_pipeline(env):
    c, _, svc, cfg = env
    make_fake_clip(cfg.data_dir / "models" / "clip-vit-b32")
    c.put("/api/settings", json={"ai_clip": True})
    svc.process_pending()
    st = c.get("/api/ai/status").json()
    assert st["embeddings"] == 12 and st["models"]["clip"]["ready"]
    r = c.get("/api/photos", params={"semantic": "plage"}).json()
    assert isinstance(r["items"], list)
    i = pid(c, "astro1.jpg")
    sim = c.get("/api/photos", params={"similar": i}).json()
    assert pid(c, "astro2.jpg") in [x["id"] for x in sim["items"]]  # même image redimensionnée
    c.put("/api/settings", json={"ai_tag_language": "en", "ai_tag_threshold": 0.0})
    svc.refresh_auto_tags()


@pytest.mark.skipif(not (FACE_MODELS / "det.onnx").exists(), reason="modèles InsightFace absents")
def test_faces(env):
    c, lib, svc, cfg = env
    d = cfg.data_dir / "models" / "buffalo_l"
    d.mkdir(parents=True)
    for f in ("det.onnx", "rec.onnx"):
        shutil.copy(FACE_MODELS / f, d / f)
    c.put("/api/settings", json={"ai_faces": True})
    svc.process_pending()
    clusters = c.get("/api/faces/clusters").json()
    eileen = next(cl for cl in clusters if cl["name"] == "Eileen Collins")
    assert eileen["photos"] == 2  # le nom Picasa s'étend à la 2e photo
    a2 = pid(c, "astro2.jpg")
    tags = {(t["name"], t["source"]) for t in c.get(f"/api/photos/{a2}").json()["tags"]}
    assert ("Eileen Collins", "face") in tags
    assert c.get(f"/api/faces/{eileen['cover']}/thumb").status_code == 200
    # Renommer le groupe : écrit le nom dans les directives des deux photos
    c.post(f"/api/faces/clusters/{eileen['id']}/name", json={"name": "Eileen M. Collins"})
    side = json.loads((lib / "Portraits" / ".phototheque.json").read_text(encoding="utf-8"))
    assert side["photos"]["astro2.jpg"]["people"] == ["Eileen M. Collins"]
    assert c.get("/api/photos", params={"cluster": eileen["id"]}).json()["total"] == 2
    # Les photos sans visage n'ont pas de visage IA
    assert c.get("/api/ai/status").json()["faces"] == 2


@pytest.mark.skipif(not (FACE_MODELS / "det.onnx").exists(), reason="modèles InsightFace absents")
def test_delete_face_cluster(env):
    c, lib, svc, cfg = env
    d = cfg.data_dir / "models" / "buffalo_l"
    d.mkdir(parents=True)
    for f in ("det.onnx", "rec.onnx"):
        shutil.copy(FACE_MODELS / f, d / f)
    c.put("/api/settings", json={"ai_faces": True})
    svc.process_pending()
    cl = c.get("/api/faces/clusters").json()[0]
    c.post(f"/api/faces/clusters/{cl['id']}/name", json={"name": "Astronaute"})
    r = c.delete(f"/api/faces/clusters/{cl['id']}").json()
    assert r["deleted"] and r["photos"] == 2
    assert c.get("/api/faces/clusters").json() == []
    a2 = pid(c, "astro2.jpg")
    names = {t["name"] for t in c.get(f"/api/photos/{a2}").json()["tags"]}
    assert "Astronaute" not in names
    side = json.loads((lib / "Portraits" / ".phototheque.json").read_text(encoding="utf-8"))
    assert "people" not in side["photos"].get("astro2.jpg", {})
    # Les visages ignorés ne reforment pas de groupe au traitement suivant
    svc.enqueue_missing(); svc.process_pending()
    assert c.get("/api/faces/clusters").json() == []


def test_job_scheduling_alternates_and_newest_first(env):
    c, _, svc, _ = env
    db = svc.db
    ids = [r["id"] for r in db.query("SELECT id FROM photo ORDER BY id")]
    for i in ids:
        db.execute("INSERT INTO job(photo_id, type) VALUES (?, 'clip')", (i,))
    for i in ids:
        db.execute("INSERT INTO job(photo_id, type) VALUES (?, 'faces')", (i,))
    first = svc._claim(16)
    second = svc._claim(16)
    assert {first[0]["type"], second[0]["type"]} == {"clip", "faces"}   # en alternance
    faces = first if first[0]["type"] == "faces" else second
    assert faces[0]["photo_id"] == max(ids)                               # les plus récentes d'abord


def test_enqueue_scales_with_many_done_jobs(env):
    import time
    c, _, svc, cfg = env
    db = svc.db
    with db.transaction():
        db.conn.executemany("INSERT INTO photo(root, relpath, folder, filename) VALUES ('/x', ?, 'f', ?)",
                            [(f"f/{i}.jpg", f"{i}.jpg") for i in range(30000)])
        ids = [r["id"] for r in db.conn.execute("SELECT id FROM photo").fetchall()]
        db.conn.executemany("INSERT INTO job(photo_id, type, status, model_version) VALUES (?, 'faces', 'done', 'buffalo_l')",
                            [(i,) for i in ids])
    cfg.settings.ai_faces = True
    t = time.time()
    svc.enqueue_missing()
    assert time.time() - t < 3   # plus de 10 minutes avant la v0.7.3 à cette échelle (x 7)


def test_photo_detail_never_waits_for_writer(env):
    import threading
    import time
    c, _, svc, _ = env
    i = pid(c, "IMG_0001.jpg")
    svc.db.execute("UPDATE photo SET camera=NULL WHERE id=?", (i,))   # comme une photo indexée avant la v0.5
    held, release = threading.Event(), threading.Event()

    def writer():
        with svc.db.transaction():
            held.set()
            release.wait(10)
    th = threading.Thread(target=writer)
    th.start()
    held.wait(5)
    t = time.time()
    r = c.get(f"/api/photos/{i}")
    elapsed = time.time() - t
    release.set()
    th.join()
    assert r.status_code == 200 and r.json()["filename"] == "IMG_0001.jpg" and elapsed < 2
