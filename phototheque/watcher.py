"""Surveillance des dossiers de la photothèque (watchdog : inotify sous Linux, API native sous Windows).

Les événements sont regroupés : quelques secondes après la dernière modification, seuls les
dossiers concernés sont réindexés, puis les traitements (visages, CLIP...) sont mis en file.
Limite : les partages réseau (SMB, NFS) ne signalent pas toujours les changements ; le bouton
« Réindexer » reste alors nécessaire.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .config import IMAGE_EXTENSIONS
from .picasa import INI_NAMES

log = logging.getLogger("phototheque.watch")
QUIET_SECONDS = 3.0


class FolderWatcher:
    def __init__(self, library, on_scanned=None):
        self.lib = library
        self.on_scanned = on_scanned
        self._observer = None
        self._pending: set[str] = set()
        self._last = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ événements
    def _relevant(self, path: str) -> bool:
        p = Path(path)
        if any(part.startswith(".") and part not in INI_NAMES for part in p.parts[-6:-1]):
            return False  # .picasaoriginals, dossiers cachés
        return p.suffix.lower() in IMAGE_EXTENSIONS or p.name in INI_NAMES

    def notify(self, path: str, is_dir: bool = False) -> None:
        if not is_dir and not self._relevant(path):
            return
        folder = str(Path(path) if is_dir else Path(path).parent)
        with self._lock:
            self._pending.add(folder)
            if is_dir:  # dossier supprimé/déplacé : son parent aussi
                self._pending.add(str(Path(path).parent))
            self._last = time.monotonic()

    def _handler(self):
        from watchdog.events import FileSystemEventHandler
        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed_no_write"):
                    return
                if event.is_directory and event.event_type not in ("deleted", "moved"):
                    return
                watcher.notify(event.src_path, event.is_directory)
                dest = getattr(event, "dest_path", "")
                if dest:
                    watcher.notify(dest, event.is_directory)

        return Handler()

    # ------------------------------------------------------------------ cycle de vie
    def start(self) -> bool:
        try:
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog absent : surveillance des dossiers désactivée")
            return False
        self.stop()
        self._stop.clear()
        obs = Observer()
        handler = self._handler()
        for root in self.lib.config.settings.roots:
            if Path(root).is_dir():
                try:
                    obs.schedule(handler, root, recursive=True)
                except OSError as exc:  # limite inotify atteinte, disque absent...
                    log.warning("surveillance impossible pour %s : %s", root, exc)
        obs.daemon = True
        obs.start()
        self._observer = obs
        self._thread = threading.Thread(target=self._loop, daemon=True, name="phototheque-watch")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            try:
                self._observer.stop()
            except Exception:
                pass
            self._observer = None

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                ready = self._pending and time.monotonic() - self._last >= QUIET_SECONDS
                dirs = set(self._pending) if ready else set()
                if ready:
                    self._pending.clear()
            if not dirs:
                continue
            try:
                done = self.lib.scan_folders(dirs)
            except Exception:
                log.exception("rescan ciblé")
                done = True
            if not done:  # un scan complet est en cours : on réessaiera
                with self._lock:
                    self._pending |= dirs
                continue
            if self.on_scanned:
                self.on_scanned()
