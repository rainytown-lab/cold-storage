"""Database layer for Cold Storage — a lab freezer / fridge / LN2 inventory.

Standard library only (sqlite3). One SQLite file holds everything.

Storage layout is a tree kept in a single `nodes` table:

    unit (freezer, fridge, LN2 dewar)
      └─ shelf
           └─ rack
                └─ row
                     └─ box  (grid_rows × grid_cols positions)

Items live either in a box position (node = box, pos_row/pos_col set) or
"loose" — directly in a unit (node_id NULL) or on a shelf (node = shelf).
"""

import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "COLDSTORAGE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "coldstorage.db"))

_local = threading.local()


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',   -- admin | member
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#9aa0a6',
    sort  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS units (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,          -- ln2 | -80 | -20 | +4
    location   TEXT,
    size       TEXT,
    manufacturer     TEXT,
    model            TEXT,
    serial_number    TEXT,
    inventory_number TEXT,
    last_defrost     TEXT,             -- date of last defrost
    last_pm          TEXT,             -- date of last preventive maintenance
    notes      TEXT,
    owner_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_common  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    level       TEXT NOT NULL,          -- shelf | rack | row | box | space (loose-item area on a shelf)
    position    INTEGER NOT NULL DEFAULT 0,
    span        INTEGER NOT NULL DEFAULT 1,  -- spaces: how many rack-widths it takes up
    name        TEXT,                   -- NULL = default label ("Rack 3")
    owner_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_common   INTEGER NOT NULL DEFAULT 0,
    grid_rows   INTEGER,                -- boxes only
    grid_cols   INTEGER,                -- boxes only
    label_style TEXT NOT NULL DEFAULT 'grid',  -- boxes: grid (A1) | number (1..n)
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_unit   ON nodes(unit_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id       INTEGER REFERENCES units(id) ON DELETE SET NULL,
    node_id       INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    pos_row       INTEGER,              -- 0-based, boxes only
    pos_col       INTEGER,
    loose_where   TEXT,                 -- free text for loose items ("door", "bottom bin")
    name          TEXT NOT NULL,
    category_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_common     INTEGER NOT NULL DEFAULT 0,
    quantity      TEXT,
    concentration TEXT,
    source        TEXT,
    lot           TEXT,
    expiry_date   TEXT,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'stored',  -- stored | removed
    date_added    TEXT NOT NULL,
    added_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    date_removed  TEXT,
    removed_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    removal_reason TEXT,
    last_location TEXT,                 -- path snapshot, kept after removal
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_node   ON items(node_id, status);
CREATE INDEX IF NOT EXISTS idx_items_unit   ON items(unit_id, status);

CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL DEFAULT (datetime('now')),
    user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action   TEXT NOT NULL,      -- item.add | item.remove | item.move | item.edit | item.restore | unit.* | node.*
    item_id  INTEGER REFERENCES items(id) ON DELETE SET NULL,
    unit_id  INTEGER REFERENCES units(id) ON DELETE SET NULL,
    summary  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_item ON activity(item_id);
CREATE INDEX IF NOT EXISTS idx_activity_at   ON activity(at);
"""

DEFAULT_CATEGORIES = [
    ("Cells", "#e4572e"),
    ("Plasmids", "#3b82f6"),
    ("Antibodies", "#8b5cf6"),
    ("Glycerol stocks", "#f59e0b"),
    ("DNA", "#14b8a6"),
    ("RNA", "#eab308"),
    ("Protein", "#ec4899"),
    ("Tissue", "#a16207"),
    ("Serum / plasma", "#dc2626"),
    ("Reagents", "#64748b"),
    ("Other", "#9ca3af"),
]


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Lightweight migrations for databases created by older versions.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "span" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN span INTEGER NOT NULL DEFAULT 1")
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO categories (name, color, sort) VALUES (?, ?, ?)",
            [(n, c, i) for i, (n, c) in enumerate(DEFAULT_CATEGORIES)])
    conn.commit()
