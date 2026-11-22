"""Configuration de l'application (fichier settings.json dans le dossier de données)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("PHOTOTHEQUE_DATA", Path.home() / ".phototheque"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif"}

# Nom du fichier de directives écrit dans chaque dossier de photos.
SIDECAR_NAME = ".phototheque.json"


@dataclass
class Settings:
    # Racines de la photothèque (chemins absolus).
    roots: list[str] = field(default_factory=list)
    # Dossiers retirés de la photothèque (conservés sur le disque, ignorés par l'indexation).
    excluded_folders: list[str] = field(default_factory=list)
    # Surveillance des dossiers : nouvelles photos indexées automatiquement.
    watch_folders: bool = True
    # Facteur de conversion de la valeur "tilt" de Picasa vers des degrés.
    # L'unité n'est pas documentée : à étalonner avec `python -m phototheque calibrate`.
    picasa_tilt_deg_per_unit: float = 10.0
    # Sens de rotation de Picasa : False = rotate(n) est un quart de tour anti-horaire
    # (constaté sur de vraies photothèques Picasa).
    picasa_rotate_clockwise: bool = False
    # Focale équivalente 24x36 par défaut quand l'EXIF ne la donne pas (correction de perspective).
    default_f35: float = 28.0
    # Intensité par défaut de la correction des lignes fuyantes.
    perspective_strength: float = 0.8
    # Taille maximale du cache des miniatures et aperçus (Go). Purge automatique au-delà.
    cache_max_gb: float = 10.0
    # Intensité par défaut du bouton « Améliorer » (1.0 = 100 %).
    enhance_strength: float = 1.0
    # Langue par défaut de l'interface ("fr" ou "en").
    language: str = "fr"
    # Thème : "auto" (suit le système), "light" ou "dark".
    theme: str = "auto"
    # Diaporama : secondes par photo.
    slideshow_interval: float = 5.0
    # Tags pour lesquels les lignes fuyantes sont corrigées automatiquement.
    perspective_auto_tags: list[str] = field(default_factory=list)
    # Reconnaissance d'image (modèles téléchargés à la première activation).
    ai_clip: bool = False          # tags automatiques, recherche en langage naturel, photos similaires
    ai_faces: bool = False         # détection et regroupement des visages
    ai_tag_language: str = "fr"    # langue des tags automatiques
    ai_tag_threshold: float = 0.12 # probabilité minimale d'un tag automatique
    face_model: str = "buffalo_l"  # "buffalo_l" (précis) ou "buffalo_s" (léger)
    face_cluster_threshold: float = 0.48  # similarité cosinus pour regrouper deux visages
    # Version du format des réglages (migrations).
    settings_version: int = 3


class Config:
    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        # Repli quand un dossier de photos est en lecture seule.
        self.sidecar_fallback_dir = self.data_dir / "sidecars"
        self.settings_path = self.data_dir / "settings.json"
        self.albums_path = self.data_dir / "albums.json"
        self.db_path = self.data_dir / "index.db"
        self.settings = self._load()

    def _load(self) -> Settings:
        if self.settings_path.exists():
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            known = {k: v for k, v in raw.items() if k in Settings.__dataclass_fields__}
            settings = Settings(**known)
            version = raw.get("settings_version", 1)
            if version < 2:
                # v0.2 : le sens de rotate(n) de Picasa est anti-horaire.
                settings.picasa_rotate_clockwise = False
            if version < 3:
                # v0.3 : le signe du tilt Picasa est géré par la formule ; le facteur est une amplitude.
                settings.picasa_tilt_deg_per_unit = abs(settings.picasa_tilt_deg_per_unit)
            if version < Settings.settings_version:
                settings.settings_version = Settings.settings_version
                self.settings = settings
                self.save()
            return settings
        return Settings()

    def save(self) -> None:
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.settings), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.settings_path)

    def add_root(self, path: str) -> None:
        p = str(Path(path).expanduser().resolve())
        if p not in self.settings.roots:
            self.settings.roots.append(p)
            self.save()
