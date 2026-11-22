"""Amélioration automatique (équivalent du bouton « Améliorer » de Google Photos, sans IA).

L'analyse est faite une seule fois (au clic) et produit des paramètres enregistrés dans
l'opération « autoenhance ». Le rendu à la volée ne fait qu'appliquer ces paramètres :
le résultat est toujours le même et le calcul reste rapide.

Étapes du rendu, dans l'ordre :
  1. balance des blancs (gains R, V, B)
  2. niveaux (point noir, point blanc) et exposition (gamma)
  3. contraste local CLAHE sur la luminance (espace Lab) : débouche les ombres
  4. vibrance : renforce les couleurs ternes, ménage les couleurs déjà vives (teints)
  5. légère netteté
  6. mélange avec l'image d'origine selon l'intensité
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageFilter

TARGET_MEDIAN = 0.47   # luminosité médiane visée (0..1)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def analyze(img: Image.Image) -> dict:
    """Mesure l'image et renvoie les paramètres de correction."""
    work = img.convert("RGB")
    work.thumbnail((800, 800), Image.BILINEAR)
    rgb = np.asarray(work, dtype=np.float32) / 255.0
    y = _luma(rgb)

    # 1. Balance des blancs « monde gris » sur les tons moyens, peu saturés.
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    mask = (y > 0.15) & (y < 0.9) & (sat < 0.6)
    if mask.sum() < 500:
        mask = (y > 0.05) & (y < 0.97)
    means = rgb[mask].mean(axis=0) if mask.any() else rgb.reshape(-1, 3).mean(axis=0)
    gray = float(means.mean())
    gains = gray / np.maximum(means, 1e-6)
    # Correction atténuée (une partie de la dominante est souvent voulue : coucher de soleil...)
    gains = 1.0 + (gains - 1.0) * 0.6
    gains = np.clip(gains, 0.85, 1.2)
    gains /= gains.mean()
    balanced = np.clip(rgb * gains, 0, 1)
    y = _luma(balanced)

    # 2. Niveaux puis exposition.
    black = float(np.percentile(y, 0.4))
    white = float(np.percentile(y, 99.6))
    black = min(black, 0.08)          # ne pas écraser des noirs volontairement doux
    white = max(white, 0.85)
    yl = np.clip((y - black) / max(white - black, 1e-3), 0, 1)
    median = float(np.median(yl))
    gamma = 1.0
    if 0.02 < median < 0.98:
        gamma = float(np.clip(np.log(TARGET_MEDIAN) / np.log(median), 0.65, 1.4))
        gamma = 1.0 + (gamma - 1.0) * 0.75

    # 3. Contraste local : plus fort quand l'image est plate.
    spread = float(np.std(yl ** gamma))
    clahe = float(np.clip(3.2 - spread * 8.0, 1.2, 2.6))

    # 4. Vibrance selon la saturation moyenne.
    mean_sat = float(sat.mean())
    vibrance = float(np.clip(0.35 - mean_sat, 0.05, 0.3))

    return {"wb": [round(float(g), 4) for g in gains], "black": round(black, 4), "white": round(white, 4),
            "gamma": round(gamma, 4), "clahe": round(clahe, 2), "vibrance": round(vibrance, 3)}


def apply(img: Image.Image, params: dict, strength: float = 1.0) -> Image.Image:
    strength = float(np.clip(strength, 0.0, 1.5))
    if strength <= 0:
        return img
    src = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0

    # 1-2. Balance des blancs, niveaux, exposition
    out = src * np.array(params.get("wb", [1, 1, 1]), dtype=np.float32)
    black, white = params.get("black", 0.0), params.get("white", 1.0)
    out = np.clip((out - black) / max(white - black, 1e-3), 0, 1)
    gamma = params.get("gamma", 1.0)
    if abs(gamma - 1.0) > 1e-3:
        out = out ** gamma

    # 3. CLAHE sur L (Lab), mélangé à 60 % pour rester naturel
    lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB)          # L dans [0, 100]
    L = lab[..., 0]
    h, w = L.shape
    tiles = (max(2, round(w / 200)), max(2, round(h / 200)))
    L8 = np.clip(L * 2.55, 0, 255).astype(np.uint8)
    Lc = cv2.createCLAHE(clipLimit=params.get("clahe", 2.0), tileGridSize=tiles).apply(L8).astype(np.float32) / 2.55
    lab[..., 0] = L * 0.4 + Lc * 0.6

    # 4. Vibrance : a, b amplifiés d'autant plus que la couleur est terne
    v = params.get("vibrance", 0.15)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    boost = 1.0 + v * np.clip(1.0 - chroma / 60.0, 0.0, 1.0)
    lab[..., 1] *= boost
    lab[..., 2] *= boost
    out = np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1)

    # 6. Intensité
    if strength != 1.0:
        out = np.clip(src + (out - src) * strength, 0, 1)
    result = Image.fromarray((out * 255 + 0.5).astype(np.uint8))

    # 5. Netteté légère (proportionnelle à la taille et à l'intensité)
    radius = max(0.6, min(img.width, img.height) / 1500)
    return result.filter(ImageFilter.UnsharpMask(radius=radius, percent=int(35 * min(strength, 1.0)), threshold=2))
