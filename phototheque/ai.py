"""Reconnaissance d'image locale (ONNX Runtime) : CLIP et visages (InsightFace).

- CLIP ViT-B/32 : un vecteur par photo -> tags automatiques (vocabulaire bilingue),
  recherche en langage naturel, photos similaires.
- InsightFace (SCRFD + ArcFace) : détection des visages, vecteurs, regroupement par personne.

Tout tourne en local ; les modèles sont téléchargés une fois dans <données>/models.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger("phototheque.ai")

HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
GH = "https://github.com/deepinsight/insightface/releases/download/v0.7"

MODELS = {
    "clip": {
        "dir": "clip-vit-b32",
        "files": {
            "vision_model.onnx": f"{HF}/onnx/vision_model_quantized.onnx",
            "text_model.onnx": f"{HF}/onnx/text_model_quantized.onnx",
            "tokenizer.json": f"{HF}/tokenizer.json",
        },
        "approx_mb": 155,
    },
    "buffalo_l": {
        "dir": "buffalo_l",
        "zip": f"{GH}/buffalo_l.zip",
        "files": {"det.onnx": "det_10g.onnx", "rec.onnx": "w600k_r50.onnx"},
        "approx_mb": 290,
    },
    "buffalo_s": {
        "dir": "buffalo_s",
        "zip": f"{GH}/buffalo_s.zip",
        "files": {"det.onnx": "det_500m.onnx", "rec.onnx": "w600k_mbf.onnx"},
        "approx_mb": 125,
    },
}


def _session(path: Path):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    providers = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    return ort.InferenceSession(str(path), sess_options=opts, providers=providers)


def load_rgb(path: Path, max_side: int, duration: float | None = None) -> Image.Image:
    """Image orientée et réduite, en RGB (HEIC, RAW, et une image extraite pour les vidéos)."""
    from . import media
    im = media.open_image(path, max_side, duration)
    im.thumbnail((max_side, max_side), Image.BILINEAR)
    return im


# --------------------------------------------------------------------------- modèles
class ModelManager:
    def __init__(self, models_dir: Path):
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.progress: dict[str, dict] = {}
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        return self.models_dir / MODELS[key]["dir"]

    def ready(self, key: str) -> bool:
        d = self.path(key)
        return all((d / name).is_file() for name in MODELS[key]["files"])

    def status(self) -> dict:
        return {k: {"ready": self.ready(k), "approx_mb": v["approx_mb"], **self.progress.get(k, {})}
                for k, v in MODELS.items()}

    def download_async(self, key: str) -> None:
        with self._lock:
            if self.ready(key) or self.progress.get(key, {}).get("running"):
                return
            self.progress[key] = {"running": True, "done_mb": 0, "error": None}
        threading.Thread(target=self._download, args=(key,), daemon=True).start()

    def _fetch(self, url: str, target: Path, key: str) -> None:
        tmp = target.with_suffix(target.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "phototheque"})
        from .net import ssl_context
        with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                self.progress[key]["done_mb"] += len(chunk) / (1 << 20)
        tmp.replace(target)

    def _download(self, key: str) -> None:
        spec, d = MODELS[key], self.path(key)
        d.mkdir(parents=True, exist_ok=True)
        try:
            if "zip" in spec:
                archive = d / "model.zip"
                self._fetch(spec["zip"], archive, key)
                with zipfile.ZipFile(archive) as z:
                    names = {Path(n).name: n for n in z.namelist()}
                    for local, inner in spec["files"].items():
                        with z.open(names[inner]) as src, open(d / local, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                archive.unlink()
            else:
                for local, url in spec["files"].items():
                    if not (d / local).is_file():
                        self._fetch(url, d / local, key)
            self.progress[key].update(running=False)
        except Exception as exc:  # réseau, disque...
            log.exception("téléchargement %s", key)
            self.progress[key].update(running=False, error=str(exc))


# --------------------------------------------------------------------------- CLIP
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# Vocabulaire des tags automatiques : (clé anglaise / prompt, libellé français)
VOCABULARY = [
    ("beach", "plage"), ("sea", "mer"), ("mountain", "montagne"), ("snow", "neige"), ("forest", "forêt"),
    ("lake", "lac"), ("river", "rivière"), ("waterfall", "cascade"), ("countryside", "campagne"),
    ("field", "champ"), ("vineyard", "vignes"), ("garden", "jardin"), ("park", "parc"), ("desert", "désert"),
    ("island", "île"), ("cliff", "falaise"), ("cave", "grotte"), ("sky", "ciel"), ("clouds", "nuages"),
    ("sunset", "coucher de soleil"), ("sunrise", "lever de soleil"), ("night", "nuit"), ("rain", "pluie"),
    ("fog", "brouillard"), ("rainbow", "arc-en-ciel"), ("starry sky", "ciel étoilé"),
    ("city", "ville"), ("street", "rue"), ("village", "village"), ("building", "bâtiment"),
    ("architecture", "architecture"), ("facade", "façade"), ("skyscraper", "gratte-ciel"),
    ("church", "église"), ("cathedral", "cathédrale"), ("castle", "château"), ("ruins", "ruines"),
    ("monument", "monument"), ("bridge", "pont"), ("tower", "tour"), ("lighthouse", "phare"),
    ("harbor", "port"), ("canal", "canal"), ("market", "marché"), ("museum", "musée"), ("statue", "statue"),
    ("painting", "tableau"), ("interior", "intérieur"), ("kitchen", "cuisine"), ("living room", "salon"),
    ("house", "maison"), ("road", "route"), ("train", "train"), ("car", "voiture"), ("bicycle", "vélo"),
    ("motorcycle", "moto"), ("boat", "bateau"), ("sailboat", "voilier"), ("airplane", "avion"),
    ("bus", "bus"), ("person", "personne"), ("portrait", "portrait"), ("group of people", "groupe"),
    ("crowd", "foule"), ("child", "enfant"), ("baby", "bébé"), ("family", "famille"), ("couple", "couple"),
    ("selfie", "selfie"), ("wedding", "mariage"), ("birthday party", "anniversaire"), ("party", "fête"),
    ("christmas", "Noël"), ("christmas tree", "sapin de Noël"), ("concert", "concert"),
    ("sport", "sport"), ("football", "football"), ("rugby", "rugby"), ("swimming", "baignade"),
    ("skiing", "ski"), ("hiking", "randonnée"), ("cycling", "cyclisme"), ("dog", "chien"), ("cat", "chat"),
    ("horse", "cheval"), ("cow", "vache"), ("sheep", "mouton"), ("bird", "oiseau"), ("insect", "insecte"),
    ("butterfly", "papillon"), ("fish", "poisson"), ("wild animal", "animal sauvage"), ("flower", "fleur"),
    ("tree", "arbre"), ("plant", "plante"), ("autumn leaves", "feuilles d'automne"), ("food", "repas"),
    ("dish", "plat"), ("dessert", "dessert"), ("cake", "gâteau"), ("bread", "pain"), ("cheese", "fromage"),
    ("fruit", "fruits"), ("vegetables", "légumes"), ("wine", "vin"), ("coffee", "café"),
    ("restaurant", "restaurant"), ("table", "table"), ("document", "document"), ("text", "texte"),
    ("screenshot", "capture d'écran"), ("map", "carte"), ("drawing", "dessin"),
    ("black and white photo", "noir et blanc"), ("old photograph", "photo ancienne"), ("macro photo", "macro"),
    ("aerial view", "vue aérienne"), ("landscape", "paysage"), ("panorama", "panorama"),
    ("fireworks", "feu d'artifice"), ("campfire", "feu de camp"), ("tent", "tente"), ("swimming pool", "piscine"),
    ("playground", "aire de jeux"), ("school", "école"), ("office", "bureau"), ("computer", "ordinateur"),
    ("phone", "téléphone"), ("book", "livre"), ("toy", "jouet"), ("musical instrument", "instrument de musique"),
    ("military", "militaire"), ("uniform", "uniforme"), ("cemetery", "cimetière"), ("drone", "drone"),
]
CLIP_MODEL_ID = "clip-vit-b32-q"


class Clip:
    def __init__(self, model_dir: Path):
        from tokenizers import Tokenizer
        self.vision = _session(model_dir / "vision_model.onnx")
        self.text = _session(model_dir / "text_model.onnx")
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        eos = self.tok.token_to_id("<|endoftext|>")
        self.pad_id = eos if eos is not None else 0
        self.tok.enable_truncation(77)
        self.tok.enable_padding(length=77, pad_id=self.pad_id)
        self.text_inputs = {i.name for i in self.text.get_inputs()}
        self._vocab_matrix = None

    @staticmethod
    def preprocess(img: Image.Image) -> np.ndarray:
        w, h = img.size
        s = 224 / min(w, h)
        img = img.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
        w, h = img.size
        left, top = (w - 224) // 2, (h - 224) // 2
        a = np.asarray(img.crop((left, top, left + 224, top + 224)), dtype=np.float32) / 255.0
        return ((a - MEAN) / STD).transpose(2, 0, 1)

    @staticmethod
    def _pick(outputs, names, wanted: str) -> np.ndarray:
        for name, val in zip(names, outputs):
            if name == wanted:
                return val
        return next(v for v in outputs if v.ndim == 2)

    @staticmethod
    def _norm(v: np.ndarray) -> np.ndarray:
        return (v / np.linalg.norm(v, axis=-1, keepdims=True)).astype(np.float32)

    def embed_images(self, imgs: list[Image.Image]) -> np.ndarray:
        batch = np.stack([self.preprocess(i) for i in imgs])
        names = [o.name for o in self.vision.get_outputs()]
        out = self.vision.run(None, {self.vision.get_inputs()[0].name: batch})
        return self._norm(self._pick(out, names, "image_embeds"))

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        feeds = {}
        if "input_ids" in self.text_inputs:
            feeds["input_ids"] = np.array([e.ids for e in enc], dtype=np.int64)
        if "attention_mask" in self.text_inputs:
            feeds["attention_mask"] = np.array([e.attention_mask for e in enc], dtype=np.int64)
        names = [o.name for o in self.text.get_outputs()]
        out = self.text.run(None, feeds)
        return self._norm(self._pick(out, names, "text_embeds"))

    def vocab_matrix(self) -> np.ndarray:
        if self._vocab_matrix is None:
            prompts = [f"a photo of {en}" for en, _ in VOCABULARY]
            self._vocab_matrix = np.concatenate([self.embed_texts(prompts[i:i + 32])
                                                 for i in range(0, len(prompts), 32)])
        return self._vocab_matrix

    def auto_tags(self, vec: np.ndarray, threshold: float, max_tags: int = 5) -> list[tuple[str, float]]:
        logits = 100.0 * self.vocab_matrix() @ vec
        p = np.exp(logits - logits.max())
        p /= p.sum()
        order = np.argsort(-p)[:max_tags]
        return [(VOCABULARY[i][0], float(p[i])) for i in order if p[i] >= threshold]


def tag_label(key: str, lang: str) -> str:
    if lang == "fr":
        for en, fr in VOCABULARY:
            if en == key:
                return fr
    return key


# --------------------------------------------------------------------------- visages
ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


class Faces:
    def __init__(self, model_dir: Path, det_size: int = 640):
        self.det = _session(model_dir / "det.onnx")
        self.rec = _session(model_dir / "rec.onnx")
        self.det_size = det_size
        self._centers: dict = {}

    def _anchor_centers(self, h: int, w: int, stride: int) -> np.ndarray:
        key = (h, w, stride)
        if key not in self._centers:
            c = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            self._centers[key] = np.repeat(c, 2, axis=0)  # 2 ancres par position
        return self._centers[key]

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[dict]:
        """Renvoie [{box: (x1,y1,x2,y2) px, kps: 5x2 px, score}] dans le repère de img."""
        a = np.asarray(img)
        h, w = a.shape[:2]
        s = self.det_size / max(h, w)
        nh, nw = int(round(h * s)), int(round(w * s))
        canvas = np.zeros((self.det_size, self.det_size, 3), dtype=np.uint8)
        canvas[:nh, :nw] = cv2.resize(a, (nw, nh))
        blob = ((canvas.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.det.run(None, {self.det.get_inputs()[0].name: blob})
        boxes, scores, kpss = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            sc = outs[i].reshape(-1)
            bb = outs[i + 3].reshape(-1, 4) * stride
            kp = outs[i + 6].reshape(-1, 10) * stride
            centers = self._anchor_centers(self.det_size // stride, self.det_size // stride, stride)
            keep = sc >= threshold
            c = centers[keep]
            b = bb[keep]
            boxes.append(np.c_[c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]])
            k = kp[keep].reshape(-1, 5, 2) + c[:, None, :]
            kpss.append(k)
            scores.append(sc[keep])
        boxes, scores, kpss = np.concatenate(boxes), np.concatenate(scores), np.concatenate(kpss)
        if not len(scores):
            return []
        idx = cv2.dnn.NMSBoxes([[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes],
                               scores.tolist(), threshold, 0.4)
        idx = np.array(idx).reshape(-1)
        return [{"box": boxes[i] / s, "kps": kpss[i] / s, "score": float(scores[i])} for i in idx]

    def embed(self, img: Image.Image, kps: np.ndarray) -> np.ndarray:
        M = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST, method=cv2.LMEDS)[0]
        crop = cv2.warpAffine(np.asarray(img), M, (112, 112), borderValue=0.0)
        blob = ((crop.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
        v = self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]
        return (v / np.linalg.norm(v)).astype(np.float32)


def iou(a: tuple, b: tuple) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def vocab_hash() -> str:
    return hashlib.sha1(json.dumps(VOCABULARY).encode()).hexdigest()[:8]
