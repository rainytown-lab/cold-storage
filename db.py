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

import json
import os
import secrets
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

-- Kinds of containers that can sit in a loose space (Box, Freezing cylinder, …); editable in Settings.
CREATE TABLE IF NOT EXISTS container_types (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sort  INTEGER NOT NULL DEFAULT 0
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
    device_name      TEXT,             -- manufacturer's device/product name, e.g. "CryoExtra"
    model            TEXT,
    serial_number    TEXT,
    inventory_number TEXT,
    last_defrost     TEXT,             -- date of last defrost
    last_pm          TEXT,             -- date of last preventive maintenance
    notes      TEXT,
    owner_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_common  INTEGER NOT NULL DEFAULT 0,
    plan       TEXT,                   -- JSON rack arrangement for LN2 tanks (see PLAN below), NULL = ring
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ready-made set-ups for common models (picked when adding a unit; all values stay editable).
CREATE TABLE IF NOT EXISTS presets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    manufacturer TEXT,
    device_name  TEXT,
    model        TEXT,
    size         TEXT,
    layout       TEXT NOT NULL,        -- JSON {shelves, racks, rows, boxes, grid_rows, grid_cols, label_style}
    plan         TEXT,                 -- JSON rack arrangement, optional
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    level       TEXT NOT NULL,          -- shelf | rack | row | box | space (loose-item area) | bin (container in a space)
    kind        TEXT,                   -- bins: container type ("Freezing cylinder")
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
    summary  TEXT NOT NULL,
    batch    TEXT,               -- one id per user action (a request), so it's undone as a whole
    undo     TEXT,               -- JSON: how things were just before (see app.py, Undo)
    undone_at TEXT,
    undone_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_item ON activity(item_id);
CREATE INDEX IF NOT EXISTS idx_activity_at   ON activity(at);
"""

DEFAULT_CONTAINER_TYPES = ["Box", "Freezing cylinder", "Bag", "Canister", "Tray"]

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


# A "plan" places a tank's racks on a grid instead of a ring, as seen from above.
# slots are listed in rack order (rack position 0 sits in slots[0], …); c/r are grid
# column/row; "dividers" draws the compartment walls through the centre.
CE8120_PLAN = {
    "cols": 4, "rows": 4, "dividers": True,
    "slots": [
        {"name": "A1", "c": 1, "r": 0}, {"name": "A2", "c": 1, "r": 1}, {"name": "A3", "c": 0, "r": 1},
        {"name": "B1", "c": 3, "r": 1}, {"name": "B2", "c": 2, "r": 1}, {"name": "B3", "c": 2, "r": 0},
        {"name": "C1", "c": 0, "r": 2}, {"name": "C2", "c": 1, "r": 2}, {"name": "C3", "c": 1, "r": 3},
        {"name": "D1", "c": 2, "r": 3}, {"name": "D2", "c": 2, "r": 2}, {"name": "D3", "c": 3, "r": 2},
    ],
}

BUILTIN_PRESETS = [
    {
        "name": "Thermo CryoExtra CE8120 (LN₂)",
        "type": "ln2",
        "manufacturer": "Thermo Fisher Scientific Inc (ECRI™)",
        "device_name": "CryoExtra",
        "model": "CE8120",
        # 4 compartments × 3 racks; each rack is a vertical stack of boxes.
        # Boxes per rack is a starting guess — adjust to your racks.
        "layout": {"racks": 12, "boxes": 12, "grid_rows": 9, "grid_cols": 9, "label_style": "grid"},
        "plan": CE8120_PLAN,
        "notes": "Four compartments (A–D), three racks each.",
    },
]


def _flatten_ln2(conn):
    """LN2 tanks have no shelves or rows: racks stand in the tank, boxes stack in the rack.
    Converts tanks made by older versions (shelf › rack › row › box). Safe to run every start."""
    for (uid,) in conn.execute("SELECT id FROM units WHERE type = 'ln2'").fetchall():
        shelves = conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND level = 'shelf' ORDER BY position, id",
                               (uid,)).fetchall()
        has_rows = conn.execute("SELECT 1 FROM nodes WHERE unit_id = ? AND level = 'row'", (uid,)).fetchone()
        if not shelves and not has_rows:
            continue
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM nodes WHERE unit_id = ? AND parent_id IS NULL"
                           " AND level != 'shelf'", (uid,)).fetchone()[0]
        for (sid,) in shelves:
            pieces = conn.execute("SELECT id, position FROM nodes WHERE parent_id = ? ORDER BY position, id", (sid,)).fetchall()
            for pid, ppos in pieces:
                # A single tier keeps its positions (arranged tanks' slots); several tiers are laid end to end.
                conn.execute("UPDATE nodes SET parent_id = NULL, position = ? WHERE id = ?",
                             (ppos if len(shelves) == 1 else pos, pid))
                pos += 1
            conn.execute("UPDATE items SET node_id = NULL WHERE node_id = ?", (sid,))   # loose on a tier → in the tank
            conn.execute("DELETE FROM nodes WHERE id = ?", (sid,))
        for (rid,) in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND level = 'rack'", (uid,)).fetchall():
            bpos = 0
            for (row_id,) in conn.execute("SELECT id FROM nodes WHERE parent_id = ? AND level = 'row' ORDER BY position, id",
                                          (rid,)).fetchall():
                for (bid,) in conn.execute("SELECT id FROM nodes WHERE parent_id = ? ORDER BY position, id", (row_id,)).fetchall():
                    conn.execute("UPDATE nodes SET parent_id = ?, position = ? WHERE id = ?", (rid, bpos, bid))
                    bpos += 1
                conn.execute("DELETE FROM nodes WHERE id = ?", (row_id,))
    # LN2 presets: {shelves, racks, rows, boxes} → {racks, boxes per rack}
    for pid, layout in conn.execute("SELECT id, layout FROM presets WHERE type = 'ln2'").fetchall():
        L = json.loads(layout)
        if "rows" in L or "shelves" in L:
            new = {"racks": L.get("racks", 1) * max(L.get("shelves", 1), 1),
                   "boxes": max(L.get("rows", 1), 1) * max(L.get("boxes", 1), 1)}
            new.update({k: L[k] for k in ("grid_rows", "grid_cols", "label_style") if k in L})
            conn.execute("UPDATE presets SET layout = ? WHERE id = ?", (json.dumps(new), pid))


def read_conn():
    """A separate connection: it sees only committed data, i.e. how things were before the
    current request's (not yet committed) changes. Used to record undo information."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _home_loose_items(conn):
    """Every loose item must sit in a loose space (a drawn section of the unit) or a container in one,
    like a tube sits in a box. Items left directly in a unit or on a shelf by older versions get a new loose space beside
    the racks: on their shelf, on the first shelf (freezers), or in the tank. Safe to run every start."""
    strays = conn.execute("""
        SELECT i.id, i.unit_id, i.node_id, u.type FROM items i JOIN units u ON u.id = i.unit_id
        LEFT JOIN nodes n ON n.id = i.node_id
        WHERE i.status = 'stored' AND i.pos_row IS NULL AND (i.node_id IS NULL OR n.level NOT IN ('space', 'bin'))""").fetchall()
    made = {}
    for iid, uid, node_id, utype in strays:
        holder = node_id
        if holder is None and utype != "ln2":
            first = conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS NULL AND level = 'shelf'"
                                 " ORDER BY position, id LIMIT 1", (uid,)).fetchone()
            if not first:
                cur = conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position) VALUES (?, NULL, 'shelf', 0)", (uid,))
                first = (cur.lastrowid,)
            holder = first[0]
        key = (uid, holder)
        if key not in made:
            taken = {r[0] for r in conn.execute("SELECT name FROM nodes WHERE unit_id = ? AND parent_id IS ? AND name IS NOT NULL",
                                                (uid, holder))}
            n = 1
            while f"Loose space {n}" in taken:
                n += 1
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM nodes WHERE unit_id = ? AND parent_id IS ?",
                               (uid, holder)).fetchone()[0]
            cur = conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, span, name) VALUES (?,?, 'space', ?, 1, ?)",
                               (uid, holder, pos, f"Loose space {n}"))
            made[key] = cur.lastrowid
        conn.execute("UPDATE items SET node_id = ? WHERE id = ?", (made[key], iid))


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Lightweight migrations for databases created by older versions.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "span" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN span INTEGER NOT NULL DEFAULT 1")
    acols = {r[1] for r in conn.execute("PRAGMA table_info(activity)")}
    for col, decl in (("batch", "TEXT"), ("undo", "TEXT"), ("undone_at", "TEXT"), ("undone_by", "INTEGER")):
        if col not in acols:
            conn.execute(f"ALTER TABLE activity ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_unit ON activity(unit_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_batch ON activity(batch)")
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(units)")}
    ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "kind" not in ncols:
        conn.execute("ALTER TABLE nodes ADD COLUMN kind TEXT")
    if conn.execute("SELECT COUNT(*) FROM container_types").fetchone()[0] == 0 and \
            not conn.execute("SELECT 1 FROM settings WHERE key = 'container_types_seeded'").fetchone():
        conn.executemany("INSERT INTO container_types (name, sort) VALUES (?, ?)",
                         [(n, i) for i, n in enumerate(DEFAULT_CONTAINER_TYPES)])
        conn.execute("INSERT INTO settings (key, value) VALUES ('container_types_seeded', '1')")
    for col in ("device_name", "plan"):
        if col not in ucols:
            conn.execute(f"ALTER TABLE units ADD COLUMN {col} TEXT")
    # Seed the built-in presets once (deleting one later doesn't bring it back).
    if not conn.execute("SELECT 1 FROM settings WHERE key = 'presets_seeded'").fetchone():
        for p in BUILTIN_PRESETS:
            conn.execute(
                "INSERT INTO presets (name, type, manufacturer, device_name, model, size, layout, plan, notes)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (p["name"], p["type"], p["manufacturer"], p["device_name"], p["model"], p.get("size"),
                 json.dumps(p["layout"]), json.dumps(p["plan"]) if p.get("plan") else None, p.get("notes")))
        conn.execute("INSERT INTO settings (key, value) VALUES ('presets_seeded', '1')")
    _flatten_ln2(conn)
    _home_loose_items(conn)
    # Labs set up before lab codes existed get one now (an admin can change it in Settings).
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] and \
            not conn.execute("SELECT 1 FROM settings WHERE key = 'join_code'").fetchone():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        conn.execute("INSERT INTO settings (key, value) VALUES ('join_code', ?)",
                     ("".join(secrets.choice(alphabet) for _ in range(6)),))
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO categories (name, color, sort) VALUES (?, ?, ?)",
            [(n, c, i) for i, (n, c) in enumerate(DEFAULT_CATEGORIES)])
    conn.commit()
