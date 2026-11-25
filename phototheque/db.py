"""Index SQLite. C'est un cache reconstructible : la vérité est dans les fichiers
(photos, .picasa.ini, .phototheque.json, albums.json)."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS wp_media (       -- photos envoyées dans la médiathèque WordPress
    photo_id   INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    site       TEXT NOT NULL,
    ops_hash   TEXT NOT NULL,                -- retouches au moment de l'envoi
    size       INTEGER NOT NULL,
    media_id   INTEGER NOT NULL,
    url        TEXT,
    at         TEXT,
    PRIMARY KEY (photo_id, site)
);
CREATE TABLE IF NOT EXISTS wp_post (        -- article WordPress d'un album
    album_id   TEXT NOT NULL,
    site       TEXT NOT NULL,
    post_id    INTEGER NOT NULL,
    link       TEXT,
    status     TEXT,
    at         TEXT,
    PRIMARY KEY (album_id, site)
);
CREATE TABLE IF NOT EXISTS plant_id (
    photo_id   INTEGER PRIMARY KEY REFERENCES photo(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,        -- 'ok' | 'low' (score trop faible) | 'notfound' | 'error'
    species    TEXT,                 -- nom scientifique
    common     TEXT,                 -- nom commun (langue choisie)
    family     TEXT,
    score      REAL,
    caption    TEXT,                 -- légende posée (si la photo n'en avait pas)
    at         TEXT NOT NULL
);

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS photo (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL,
    relpath     TEXT NOT NULL,          -- chemin relatif à la racine, séparateur '/'
    folder      TEXT NOT NULL,          -- dossier relatif ('' = racine)
    filename    TEXT NOT NULL,
    size        INTEGER,
    mtime       REAL,
    width       INTEGER,
    height      INTEGER,
    taken_at    TEXT,                   -- ISO 8601 (EXIF ou date du fichier)
    date_source TEXT,                   -- 'exif' | 'file'
    year        INTEGER, month INTEGER, day INTEGER,
    f35         REAL,                   -- focale équivalente 24x36 (EXIF)
    star        INTEGER DEFAULT 0,
    caption     TEXT,
    ops         TEXT,                   -- opérations effectives (JSON)
    ops_source  TEXT,                   -- 'user' | 'picasa' | NULL
    ops_hash    TEXT,
    picasa_unsupported TEXT,            -- filtres Picasa non repris (JSON)
    file_keywords TEXT,                 -- mots-clés IPTC/XMP lus dans le fichier (JSON)
    file_caption  TEXT,
    merge_sig     TEXT,                 -- signature des sources fusionnées (évite les réécritures)
    camera        TEXT,                 -- modèle d'appareil (EXIF), '' si inconnu
    media         TEXT DEFAULT 'image', -- 'image' | 'video'
    duration      REAL,                 -- durée d'une vidéo (s)
    codec         TEXT,                 -- codec vidéo
    exif_orientation INTEGER,
    UNIQUE(root, relpath)
);
CREATE INDEX IF NOT EXISTS photo_folder ON photo(root, folder);
CREATE INDEX IF NOT EXISTS photo_date ON photo(year, month, day, taken_at);
CREATE INDEX IF NOT EXISTS photo_taken ON photo(taken_at, filename);

CREATE TABLE IF NOT EXISTS tag (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL DEFAULT 'keyword',   -- 'keyword' | 'person'
    UNIQUE(name, kind)
);
CREATE TABLE IF NOT EXISTS photo_tag (
    photo_id INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    source   TEXT NOT NULL,                  -- 'user' | 'picasa' | 'iptc' | 'auto'
    PRIMARY KEY (photo_id, tag_id)
);
CREATE INDEX IF NOT EXISTS photo_tag_tag ON photo_tag(tag_id);

CREATE TABLE IF NOT EXISTS face (
    id       INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    l REAL, t REAL, r REAL, b REAL,          -- coordonnées relatives dans le repère d'origine
    person   TEXT,
    source   TEXT NOT NULL,
    score    REAL,                           -- confiance de la détection (IA)
    embedding BLOB,                          -- vecteur ArcFace (float32, normalisé)
    cluster_id INTEGER,                      -- groupe de visages (IA) ; -1 = hors groupe
    manual   INTEGER DEFAULT 0,              -- décision de l'utilisateur (nommé, retiré du groupe)
    ignored  INTEGER DEFAULT 0               -- « ce n'est pas un visage »
);
CREATE INDEX IF NOT EXISTS face_photo ON face(photo_id);
CREATE INDEX IF NOT EXISTS face_cluster_score ON face(cluster_id, score);

-- Groupes de visages (IA). Le nom donné à un groupe devient un tag « personne ».
CREATE TABLE IF NOT EXISTS face_cluster (
    id       INTEGER PRIMARY KEY,
    name     TEXT,
    centroid BLOB,
    n        INTEGER DEFAULT 0
);
-- Suggestions de regroupement écartées par l'utilisateur (« ce n'est pas la même personne »)
CREATE TABLE IF NOT EXISTS face_cluster_reject (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    PRIMARY KEY (a, b)
);

-- Vecteurs CLIP des photos et tags automatiques (résultats IA, recalculables).
CREATE TABLE IF NOT EXISTS embedding (
    photo_id INTEGER PRIMARY KEY REFERENCES photo(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    vector   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_tag (
    photo_id INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,                  -- clé du vocabulaire (anglais)
    score    REAL NOT NULL,
    model    TEXT NOT NULL,
    PRIMARY KEY (photo_id, label)
);

CREATE TABLE IF NOT EXISTS album (
    id          TEXT PRIMARY KEY,            -- jeton (Picasa ou généré)
    name        TEXT NOT NULL,
    description TEXT,
    date        TEXT,
    source      TEXT NOT NULL                -- 'user' | 'picasa'
);
CREATE TABLE IF NOT EXISTS album_photo (
    album_id TEXT NOT NULL REFERENCES album(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    position INTEGER,
    PRIMARY KEY (album_id, photo_id)
);

-- État des dossiers au dernier scan : un dossier inchangé n'est pas relu fichier par fichier.
CREATE TABLE IF NOT EXISTS folder_state (
    root   TEXT NOT NULL,
    folder TEXT NOT NULL,
    sig    TEXT NOT NULL,     -- dates de modification du dossier, du .picasa.ini, des directives + réglages
    PRIMARY KEY (root, folder)
);

-- File de traitements (IA, perspective...) consommée par les workers.
CREATE TABLE IF NOT EXISTS job (
    id         INTEGER PRIMARY KEY,
    photo_id   INTEGER REFERENCES photo(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | error
    model_version TEXT,
    result     TEXT,
    error      TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS job_status ON job(status, type);
CREATE INDEX IF NOT EXISTS job_photo_type ON job(photo_id, type, status);
CREATE UNIQUE INDEX IF NOT EXISTS job_pending ON job(photo_id, type) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS album_photo_photo ON album_photo(photo_id);
"""


class Database:
    """Accès SQLite en mode WAL : un seul écrivain, des lecteurs qui ne l'attendent jamais.

    - Les écritures (execute, transaction) passent par une connexion unique, protégée par un verrou.
    - Les lectures (query, one) d'un fil qui n'écrit pas utilisent une connexion propre à ce fil :
      en WAL, elles lisent le dernier état validé sans attendre l'indexation ni l'IA.
    - Un fil qui tient le verrou d'écriture lit par la connexion d'écriture (il voit ses propres
      modifications en cours de transaction).
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.RLock()
        self._owner = None
        self._depth = 0
        self._local = threading.local()
        with self.lock:
            self._migrate()
            self.conn.executescript(SCHEMA)

    # Colonnes ajoutées après la v0.1 (une base existante n'est pas recréée).
    _ADDED_COLUMNS = {
        "photo": {"merge_sig": "TEXT", "camera": "TEXT", "exif_orientation": "INTEGER",
                  "media": "TEXT DEFAULT 'image'", "duration": "REAL", "codec": "TEXT"},
        "face": {"score": "REAL", "embedding": "BLOB", "cluster_id": "INTEGER", "manual": "INTEGER DEFAULT 0",
                 "ignored": "INTEGER DEFAULT 0"},
    }

    def _migrate(self) -> None:
        for table, cols in self._ADDED_COLUMNS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table absente : créée par le schéma
            for col, decl in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # -- verrou d'écriture (réentrant), avec le fil propriétaire
    def _acquire(self) -> None:
        self.lock.acquire()
        self._owner = threading.get_ident()
        self._depth += 1

    def _release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self.lock.release()

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=1")
            self._local.conn = conn
        return conn

    def _read(self, sql: str, params, fetch: str):
        if self._owner == threading.get_ident():
            with self.lock:
                cur = self.conn.execute(sql, params)
                return cur.fetchall() if fetch == "all" else cur.fetchone()
        cur = self._reader().execute(sql, params)
        return cur.fetchall() if fetch == "all" else cur.fetchone()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._read(sql, params, "all")

    def one(self, sql: str, params: tuple | dict = ()):
        return self._read(sql, params, "one")

    def execute(self, sql: str, params: tuple | dict = ()):
        self._acquire()
        try:
            return self.conn.execute(sql, params)
        finally:
            self._release()

    def try_execute(self, sql: str, params: tuple | dict = ()) -> bool:
        """Écriture facultative : abandonnée si le verrou n'est pas libre (ne fait jamais attendre)."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self._owner = threading.get_ident()
            self._depth += 1
            self.conn.execute(sql, params)
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
            self.lock.release()

    def transaction(self):
        return _Tx(self)

    def tag_id(self, name: str, kind: str = "keyword") -> int:
        name = name.strip()
        self._acquire()
        try:
            row = self.conn.execute("SELECT id FROM tag WHERE name=? AND kind=?", (name, kind)).fetchone()
            if row:
                return row["id"]
            return self.conn.execute("INSERT INTO tag(name, kind) VALUES (?, ?)", (name, kind)).lastrowid
        finally:
            self._release()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._acquire()
        try:
            self.db.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.db._release()
            raise
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._release()
        return False
