"""Banc d'essai de montée en charge.

  python tests/bench.py generate /tmp/bench_lib 200000     # photothèque factice (petits JPEG)
  python tests/bench.py run /tmp/bench_lib /tmp/bench_data  # mesures
"""
from __future__ import annotations

import io
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ExifTags

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def generate(root: Path, total: int, per_folder: int = 100) -> None:
    random.seed(0)
    templates = []
    for i in range(240):  # 20 ans x 12 mois
        y, m = 2005 + i // 12, i % 12 + 1
        buf = io.BytesIO()
        img = Image.new("RGB", (64, 48), (random.randrange(256), random.randrange(256), random.randrange(256)))
        exif = Image.Exif()
        exif.get_ifd(ExifTags.IFD.Exif)[36867] = f"{y}:{m:02d}:{random.randint(1, 28):02d} 12:00:00"
        exif[271], exif[272] = "Canon", "Canon EOS 80D"
        img.save(buf, "JPEG", quality=70, exif=exif)
        templates.append(buf.getvalue())
    folders = (total + per_folder - 1) // per_folder
    n = 0
    t0 = time.time()
    for f in range(folders):
        year = 2005 + f * 20 // folders
        d = root / str(year) / f"Dossier {f:05d}"
        d.mkdir(parents=True, exist_ok=True)
        tpl = templates[(f * 240) // folders]
        for i in range(per_folder):
            if n >= total:
                break
            (d / f"IMG_{i:04d}.JPG").write_bytes(tpl)
            n += 1
        if f % 3 == 0:  # un dossier sur trois a un .picasa.ini
            (d / ".picasa.ini").write_text(
                "".join(f"[img_{i:04d}.jpg]\nstar=yes\nkeywords=vacances,famille\nfilters=tilt=1,0.1,0;\n"
                        for i in range(0, per_folder, 7)), encoding="utf-8")
    print(f"{n} photos dans {folders} dossiers en {time.time() - t0:.0f} s")


def timed(label: str, fn, repeat: int = 1):
    best = None
    out = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    unit = f"{best * 1000:.0f} ms" if best < 2 else f"{best:.1f} s"
    print(f"  {label:<52} {unit}")
    return out


def run(lib_root: Path, data_dir: Path, fresh: bool = True) -> None:
    from fastapi.testclient import TestClient
    from phototheque.api import create_app
    from phototheque.config import Config
    if fresh and data_dir.exists():
        shutil.rmtree(data_dir)
    cfg = Config(data_dir)
    cfg.add_root(str(lib_root))
    app = create_app(cfg, scan_on_start=False)
    lib, svc = app.state.library, app.state.ai
    c = TestClient(app)
    print("Indexation")
    timed("premier scan complet", lib.scan_all)
    total = lib.db.one("SELECT COUNT(*) n FROM photo")["n"]
    print(f"  ({total} photos indexées)")
    timed("rescan sans changement", lib.scan_all)
    if hasattr(lib, "scan_all") and "deep" in lib.scan_all.__code__.co_varnames:
        timed("rescan sans changement, vérification fichier par fichier", lambda: lib.scan_all(deep=True))
    print("API")
    timed("1re page « Toutes les photos » (300)", lambda: c.get("/api/photos?limit=300").json(), 3)
    timed("page au milieu (offset 100 000)", lambda: c.get("/api/photos?limit=300&offset=100000").json(), 3)
    timed("arborescence des dossiers", lambda: c.get("/api/folders").json(), 3)
    timed("dates", lambda: c.get("/api/dates").json(), 3)
    timed("tags", lambda: c.get("/api/tags").json(), 3)
    timed("filtre par tag « famille »", lambda: c.get("/api/photos?tag=famille&limit=300").json(), 3)
    timed("recherche texte « IMG_0050 »", lambda: c.get("/api/photos?q=IMG_0050&limit=300").json(), 3)
    pid = lib.db.one("SELECT id FROM photo LIMIT 1 OFFSET 777")["id"]
    timed("détail d'une photo", lambda: c.get(f"/api/photos/{pid}").json(), 3)

    print("Vecteurs CLIP (synthétiques)")
    rng = np.random.default_rng(0)
    ids = [r["id"] for r in lib.db.query("SELECT id FROM photo")]
    vec = rng.standard_normal((len(ids), 512)).astype(np.float32)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    with lib.db.transaction():
        lib.db.conn.executemany("INSERT OR REPLACE INTO embedding(photo_id, model, vector) VALUES (?,?,?)",
                                [(i, "clip-vit-b32-q", v.astype(np.float16).tobytes()) for i, v in zip(ids, vec)])
    svc._vectors_dirty = True
    timed("chargement de la matrice en mémoire", svc._matrix)
    mat = svc._matrix()[1]
    print(f"  {'mémoire de la matrice':<52} {mat.nbytes / 1e6:.0f} Mo")
    q = vec[123]
    if hasattr(svc, "_scores"):
        timed("similarité contre toutes les photos", lambda: svc._scores(q), 3)
    else:
        timed("similarité contre toutes les photos", lambda: mat @ q, 3)

    print("Regroupement des visages (synthétique : 3 000 personnes)")
    people = rng.standard_normal((3000, 512)).astype(np.float32)
    people /= np.linalg.norm(people, axis=1, keepdims=True)

    def faces(n):
        for k in range(n):
            e = people[k % 3000] + rng.standard_normal(512).astype(np.float32) * 0.035
            e /= np.linalg.norm(e)
            with lib.db.transaction():
                svc._assign_cluster(e, None, 0.48)
    timed("6 000 premiers visages", lambda: faces(6000))
    nclu = lib.db.one("SELECT COUNT(*) n FROM face_cluster")["n"]
    timed(f"200 visages de plus ({nclu} groupes existants)", lambda: faces(200))
    rate = None
    t = time.perf_counter()
    faces(500)
    rate = 500 / (time.perf_counter() - t)
    print(f"  {'débit de regroupement':<52} {rate:.0f} visages/s")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "generate":
        generate(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        run(Path(sys.argv[2]), Path(sys.argv[3]))
