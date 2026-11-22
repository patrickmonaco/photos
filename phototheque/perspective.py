"""Correction des lignes fuyantes par rotation d'un appareil virtuel.

On estime le point de fuite des verticales, on en déduit l'inclinaison (tangage) et le
roulis de l'appareil, puis on applique l'homographie H = K · R · K⁻¹. Contrairement à
une déformation « 4 coins », les proportions du sujet restent justes.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image

FULL_FRAME_DIAG = 43.267  # diagonale d'un capteur 24x36 en mm


def intrinsics(w: int, h: int, f35: float) -> np.ndarray:
    f = f35 / FULL_FRAME_DIAG * math.hypot(w, h)
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]])


def _segments(gray: np.ndarray) -> np.ndarray:
    try:
        lsd = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)
        lines = lsd.detect(gray)[0]
    except Exception:
        lines = None
    if lines is None or len(lines) == 0:
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 360, 60, minLineLength=gray.shape[0] // 15, maxLineGap=8)
    if lines is None:
        return np.zeros((0, 4))
    return lines.reshape(-1, 4).astype(np.float64)


def estimate(img: Image.Image, f35: float, max_tilt_deg: float = 35.0) -> dict | None:
    """Estime tangage et roulis à partir des verticales. Renvoie None si pas assez d'indices."""
    work = img.convert("L")
    scale = 1000.0 / max(work.size)
    if scale < 1:
        work = work.resize((round(work.width * scale), round(work.height * scale)), Image.BILINEAR)
    gray = np.asarray(work)
    h, w = gray.shape
    K = intrinsics(w, h, f35)
    Kinv = np.linalg.inv(K)

    seg = _segments(gray)
    if len(seg) == 0:
        return None
    dx, dy = seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1]
    length = np.hypot(dx, dy)
    angle_from_vertical = np.degrees(np.arctan2(np.abs(dx), np.abs(dy)))
    keep = (length > 0.04 * math.hypot(w, h)) & (angle_from_vertical < 25)
    seg, length = seg[keep], length[keep]
    if len(seg) < 4:
        return None

    # Segments en coordonnées caméra normalisées
    p1 = (Kinv @ np.c_[seg[:, :2], np.ones(len(seg))].T).T
    p2 = (Kinv @ np.c_[seg[:, 2:], np.ones(len(seg))].T).T
    lines = np.cross(p1, p2)
    lines /= np.linalg.norm(lines[:, :2], axis=1, keepdims=True)
    mid = (p1 + p2) / 2
    sdir = p2[:, :2] - p1[:, :2]
    sdir /= np.linalg.norm(sdir, axis=1, keepdims=True)

    def inliers(d: np.ndarray, tol_deg: float = 1.5) -> np.ndarray:
        if abs(d[2]) < 1e-9:
            vdir = np.tile(d[:2], (len(mid), 1))
        else:
            vdir = d[:2] / d[2] - mid[:, :2]
        n = np.linalg.norm(vdir, axis=1)
        n[n == 0] = 1
        vdir /= n[:, None]
        cosang = np.abs(np.sum(vdir * sdir, axis=1))
        return cosang > math.cos(math.radians(tol_deg))

    rng = np.random.default_rng(0)
    prob = length / length.sum()
    best, best_score = None, 0.0
    for _ in range(400):
        i, j = rng.choice(len(seg), 2, replace=False, p=prob)
        d = np.cross(lines[i], lines[j])
        if np.linalg.norm(d) < 1e-12:
            continue
        d /= np.linalg.norm(d)
        mask = inliers(d)
        score = length[mask].sum()
        if score > best_score:
            best, best_score = mask, score
    if best is None or best.sum() < 4:
        return None
    # Affinage aux moindres carrés : minimiser |L·d|
    A = lines[best] * np.sqrt(length[best])[:, None]
    d = np.linalg.svd(A)[2][-1]
    confidence = float(length[best].sum() / length.sum())

    if d[1] > 0:  # orienter vers le haut de l'image
        d = -d
    d /= np.linalg.norm(d)
    target = np.array([0.0, -1.0, 0.0])
    axis = np.cross(d, target)
    s = np.linalg.norm(axis)
    angle = math.atan2(s, float(np.dot(d, target)))
    rvec = axis / s * angle if s > 1e-12 else np.zeros(3)
    pitch, roll = math.degrees(rvec[0]), math.degrees(rvec[2])
    if abs(pitch) > max_tilt_deg:
        return None
    return {"pitch": round(pitch, 3), "roll": round(roll, 3), "confidence": round(confidence, 3),
            "lines": int(best.sum()), "f35": f35}


def homography(w: int, h: int, f35: float, pitch: float, roll: float, strength: float = 1.0):
    """Renvoie (H, largeur, hauteur) : homographie vers l'image corrigée, recadrée sur la zone utile."""
    K = intrinsics(w, h, f35)
    rvec = np.radians([pitch, 0.0, roll]) * strength
    R = cv2.Rodrigues(rvec)[0]
    H = K @ R @ np.linalg.inv(K)
    corners = np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]], dtype=np.float64).T
    c = H @ corners
    c = c[:2] / c[2]
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = c.T
    left, right = max(x0, x3), min(x1, x2)
    top, bottom = max(y0, y1), min(y2, y3)
    if right - left < 16 or bottom - top < 16:
        raise ValueError("correction trop forte")
    T = np.array([[1, 0, -left], [0, 1, -top], [0, 0, 1]])
    return T @ H, int(round(right - left)), int(round(bottom - top))


def warp(img: Image.Image, f35: float, pitch: float, roll: float, strength: float = 1.0) -> Image.Image:
    w, h = img.size
    H, ow, oh = homography(w, h, f35, pitch, roll, strength)
    arr = np.asarray(img.convert("RGB"))
    out = cv2.warpPerspective(arr, H, (ow, oh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(out)
