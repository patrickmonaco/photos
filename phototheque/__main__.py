"""Ligne de commande.

  python -m phototheque serve [--library DOSSIER ...] [--data DOSSIER] [--port 8765]
  python -m phototheque scan  [--library DOSSIER ...] [--full]
  python -m phototheque calibrate [--apply]
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import threading
import time
import webbrowser

from .config import Config


def server_running(port: int) -> bool:
    """Vrai si l'application semble déjà tourner sur ce port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def reset_index(config: Config) -> list[str]:
    """Supprime l'index SQLite et le cache des rendus (les modèles d'IA sont conservés)."""
    removed = []
    for name in ("index.db", "index.db-wal", "index.db-shm"):
        p = config.data_dir / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    if config.cache_dir.exists():
        shutil.rmtree(config.cache_dir)
        removed.append(str(config.cache_dir))
    config.cache_dir.mkdir(exist_ok=True)
    return removed


def run_scan(config: Config, deep: bool = False) -> dict:
    from .library import Library
    lib = Library(config)
    done = threading.Event()

    def progress():
        while not done.wait(2):
            st = lib.scan_state
            print(f"\r  {st['done']} photos… {str(st['folder'] or '')[:60]:<60}", end="", flush=True)

    threading.Thread(target=progress, daemon=True).start()
    start = time.time()
    state = lib.scan_all(deep=deep)
    done.set()
    total = lib.db.one("SELECT COUNT(*) n FROM photo")["n"]
    print(f"\r  {total} photos indexées en {time.time() - start:.0f} s" + " " * 50)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(prog="phototheque")
    parser.add_argument("command", choices=["serve", "scan", "calibrate"])
    parser.add_argument("--library", "-l", action="append", default=[], help="dossier racine de photos")
    parser.add_argument("--data", help="dossier de données (index, cache, albums)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--apply", action="store_true", help="calibrate : enregistrer le facteur trouvé")
    parser.add_argument("--deep", action="store_true",
                        help="scan : vérifier chaque fichier (sinon les dossiers inchangés sont sautés)")
    parser.add_argument("--full", action="store_true",
                        help="scan : reconstruire l'index à partir de zéro (supprime index.db et le cache)")
    args = parser.parse_args()

    config = Config(args.data)
    for lib in args.library:
        config.add_root(lib)

    if args.command == "scan":
        if args.full:
            if server_running(args.port):
                print(f"L'application tourne sur le port {args.port} : arrêtez-la (Ctrl+C) avant "
                      "de reconstruire l'index.")
                sys.exit(1)
            try:
                removed = reset_index(config)
            except PermissionError as exc:
                print(f"Impossible de supprimer {exc.filename} : l'application est-elle encore ouverte ?")
                sys.exit(1)
            print("Reconstruction complète. Supprimé :" if removed else "Reconstruction complète.")
            for r in removed:
                print(f"  - {r}")
            print("Conservés : réglages, albums, modèles d'IA, directives et .picasa.ini des dossiers.")
        state = run_scan(config, deep=args.deep or args.full)
        if state.get("error"):
            print(json.dumps(state, ensure_ascii=False))
            sys.exit(1)
        if args.full:
            print("Les analyses d'IA (si activées) reprendront au prochain lancement de « phototheque serve ».")
    elif args.command == "calibrate":
        from .calibrate import calibrate
        result = calibrate(config.settings.roots)
        for s in result["samples"]:
            print(f"{s['file']}: tilt={s['tilt']} -> {s['deg']}° (facteur {s['factor']})")
        if result["factor"] is None:
            print("Aucune photo exploitable (il faut des originaux dans .picasaoriginals).")
        else:
            print(f"Facteur médian : {result['factor']} degrés par unité ({result['count']} photos)")
            if args.apply:
                config.settings.picasa_tilt_deg_per_unit = result["factor"]
                config.save()
                print("Facteur enregistré. Relancez un scan pour l'appliquer.")
    else:
        import uvicorn
        from .api import create_app
        app = create_app(config)
        url = f"http://{args.host}:{args.port}/"
        print(f"Photothèque : {url}  (données : {config.data_dir})")
        print(f"Journal : {config.data_dir / 'phototheque.log'}")
        if not args.no_browser:
            webbrowser.open(url)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
