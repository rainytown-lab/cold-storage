"""Cold Storage — inventory for a lab's freezers, fridges and liquid nitrogen.

Standard-library only: http.server + sqlite3, serving a JSON API and a
vanilla-JS single-page app from ./static.

Run:  python3 app.py        (then open http://localhost:8100)
      PORT=9000 python3 app.py
"""

import csv
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_DAYS = 30
COOKIE = "cs_session"
# Add "Secure" to the login cookie (only sent over HTTPS). Off for local/LAN use;
# on in production behind HTTPS (fly.toml sets COLDSTORAGE_SECURE_COOKIE=1).
SECURE_COOKIE = os.environ.get("COLDSTORAGE_SECURE_COOKIE", "").lower() in ("1", "true", "yes")
MAX_BODY = 4 * 1024 * 1024

UNIT_TYPES = ("ln2", "-80", "-20", "+4")
UNIT_TYPE_LABEL = {"ln2": "Liquid nitrogen", "-80": "−80 °C freezer",
                   "-20": "−20 °C freezer", "+4": "+4 °C fridge"}
LEVELS = ("shelf", "rack", "row", "box")          # freezers and fridges
# Each storage type has its own chain of levels. LN2 tanks have no shelves or rows:
# racks stand straight in the tank and hold a vertical stack of boxes.
CHAINS = {"ln2": ("rack", "box")}


def chain_for(unit_type):
    return CHAINS.get(unit_type, LEVELS)
LEVEL_LABEL = {"shelf": "Shelf", "rack": "Rack", "row": "Row", "box": "Box", "space": "Loose space", "bin": "Container"}
SHELF_CHILDREN = ("rack", "space")   # things that sit side by side on a shelf
MAX_SPAN = 30

# Bump when the API changes, so an already-open page can tell the server needs a restart.
API_VERSION = 15
LAYOUT_KEYS = {"shelf": "shelves", "rack": "racks", "row": "rows", "box": "boxes"}
LAYOUT_MAX = {"shelves": 30, "racks": 30, "rows": 40, "boxes": 40}
MAX_BOXES_PER_UNIT = 6000
MAX_GRID = 26  # rows (A–Z) and columns

ITEM_FIELDS = ("name", "category_id", "owner_id", "is_common", "quantity",
               "concentration", "source", "lot", "expiry_date", "notes",
               "date_added", "loose_where")


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def require(cond, message, status=400):
    if not cond:
        raise ApiError(message, status)


ROUTES = []


def route(method, pattern, auth=True, admin=False):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn, auth, admin))
        return fn
    return deco


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def today():
    return datetime.date.today().isoformat()


def text(body, key, maxlen=500):
    v = body.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v[:maxlen] if v else None


def as_int(v, name, lo=None, hi=None, allow_none=True):
    if v is None or v == "":
        require(allow_none, f"{name} is required")
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a whole number")
    if lo is not None:
        require(n >= lo, f"{name} must be at least {lo}")
    if hi is not None:
        require(n <= hi, f"{name} can be at most {hi}")
    return n


def as_date(v, name):
    if v in (None, ""):
        return None
    try:
        return datetime.date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ApiError(f"{name} must be a date (YYYY-MM-DD)")


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def log(ctx, action, summary, item_id=None, unit_id=None):
    """Record a change in the activity log, plus what's needed to undo it.

    Undo info is read through a second connection, which still sees the data as it was before this
    request (our changes aren't committed yet): the item's previous row, or the unit's whole layout."""
    ctx.setdefault("batch", secrets.token_hex(8))
    undo = None
    try:
        rc = db.read_conn()
        if action.startswith("item.") and item_id:
            r = rc.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            undo = {"kind": "item", "before": dict(r) if r else None}
        elif action.startswith("node.") and unit_id and unit_id not in ctx.setdefault("_snapped", set()):
            ctx["_snapped"].add(unit_id)
            undo = {"kind": "layout", "unit_id": unit_id,
                    "nodes": [dict(r) for r in rc.execute("SELECT * FROM nodes WHERE unit_id = ?", (unit_id,))],
                    "items": [dict(r) for r in rc.execute(
                        "SELECT id, unit_id, node_id, pos_row, pos_col, status FROM items WHERE unit_id = ?", (unit_id,))]}
        elif action == "unit.edit" and unit_id:
            r = rc.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
            undo = {"kind": "unit", "before": dict(r) if r else None}
        elif action == "unit.add" and unit_id:
            undo = {"kind": "unit.add"}
        rc.close()
    except sqlite3.Error:
        undo = None
    ctx["conn"].execute(
        "INSERT INTO activity (user_id, action, item_id, unit_id, summary, batch, undo) VALUES (?,?,?,?,?,?,?)",
        (ctx["user"]["id"] if ctx.get("user") else None, action, item_id, unit_id, summary, ctx["batch"],
         json.dumps(undo) if undo else None))


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


# --------------------------------------------------------------------------- #
# Tree helpers: labels, paths, effective owner
# --------------------------------------------------------------------------- #

class Tree:
    """All units + nodes, indexed, with sibling indices for default labels."""

    def __init__(self, conn, unit_id=None):
        if unit_id is None:
            self.units = {u["id"]: u for u in rows(conn.execute("SELECT * FROM units"))}
            nodes = rows(conn.execute("SELECT * FROM nodes"))
        else:
            self.units = {u["id"]: u for u in rows(conn.execute(
                "SELECT * FROM units WHERE id = ?", (unit_id,)))}
            nodes = rows(conn.execute("SELECT * FROM nodes WHERE unit_id = ?", (unit_id,)))
        self.nodes = {n["id"]: n for n in nodes}
        groups = defaultdict(list)
        for n in nodes:
            # Number per level, so "Rack 3" ignores loose spaces sitting between racks.
            # (containers are numbered per type: "Box 1", "Freezing cylinder 1")
            groups[(n["unit_id"], n["parent_id"], n["level"], n.get("kind") if n["level"] == "bin" else None)].append(n)
        for g in groups.values():
            g.sort(key=lambda n: (n["position"], n["id"]))
            for i, n in enumerate(g):
                n["index"] = i

    def label(self, n):
        if n["name"]:
            return n["name"]
        if n["level"] == "bin":   # containers are named by type: "Freezing cylinder 2"
            return f"{n.get('kind') or 'Container'} {n['index'] + 1}"
        return f"{LEVEL_LABEL[n['level']]} {n['index'] + 1}"

    def chain(self, node_id):
        out = []
        n = self.nodes.get(node_id)
        while n:
            out.append(n)
            n = self.nodes.get(n["parent_id"])
        out.reverse()
        return out

    def pos_label(self, box, r, c):
        if r is None or c is None or not box:
            return None
        if box.get("label_style") == "number":
            return str(r * (box["grid_cols"] or 1) + c + 1)
        return f"{chr(65 + r)}{c + 1}"

    def location(self, item):
        unit = self.units.get(item.get("unit_id"))
        if not unit:
            return {"parts": [], "text": item.get("last_location") or "—", "pos": None}
        chain = self.chain(item.get("node_id")) if item.get("node_id") else []
        parts = [{"kind": "unit", "id": unit["id"], "label": unit["name"]}]
        parts += [{"kind": n["level"], "id": n["id"], "label": self.label(n)} for n in chain]
        pos = None
        if chain and chain[-1]["level"] == "box":
            pos = self.pos_label(chain[-1], item.get("pos_row"), item.get("pos_col"))
        txt = " › ".join(p["label"] for p in parts)
        if pos:
            txt += f" › {pos}"
        elif item.get("pos_row") is None and chain and chain[-1]["level"] in ("space", "bin"):
            txt += f" ({item['loose_where']})" if item.get("loose_where") else ""
        elif item.get("pos_row") is None:
            txt += " › loose" + (f" ({item['loose_where']})" if item.get("loose_where") else "")
        return {"parts": parts, "text": txt, "pos": pos}

    def effective_owner(self, item):
        """(owner_id, is_common, source_label) — item's own setting, else inherited."""
        if item.get("owner_id") or item.get("is_common"):
            return item.get("owner_id"), bool(item.get("is_common")), "item"
        for n in reversed(self.chain(item.get("node_id")) if item.get("node_id") else []):
            if n["owner_id"] or n["is_common"]:
                return n["owner_id"], bool(n["is_common"]), self.label(n)
        unit = self.units.get(item.get("unit_id"))
        if unit and (unit["owner_id"] or unit["is_common"]):
            return unit["owner_id"], bool(unit["is_common"]), unit["name"]
        return None, False, None


def subtree_ids(conn, node_id):
    return [r[0] for r in conn.execute("""
        WITH RECURSIVE t(id) AS (SELECT ? UNION ALL SELECT n.id FROM nodes n JOIN t ON n.parent_id = t.id)
        SELECT id FROM t""", (node_id,))]


def next_level(level, chain=LEVELS):
    if level not in chain:
        return None
    i = chain.index(level)
    return chain[i + 1] if i + 1 < len(chain) else None


def unit_chain(conn, unit_id):
    r = conn.execute("SELECT type FROM units WHERE id = ?", (unit_id,)).fetchone()
    return chain_for(r[0] if r else None)


def clean_layout(layout, from_level, chain=LEVELS):
    """Validate the per-level counts needed below (and including) from_level."""
    out = {}
    start = chain.index(from_level)
    boxes_total = 1
    # Loose-space sections per shelf (or in a tank), each optionally starting with some containers —
    # for fridges and tanks that don't hold racks at all.
    out["sections"] = as_int(layout.get("sections") or 0, "Loose-space sections", 0, 20)
    out["containers"] = as_int(layout.get("containers") or 0, "Containers per section", 0, 50)
    out["container_type"] = text(layout, "container_type", 60)
    require(not out["containers"] or out["container_type"], "Pick a container type")
    for lvl in chain[start:]:
        key = LAYOUT_KEYS[lvl]
        lowest = 1 if lvl == from_level and not (lvl == "rack" and out["sections"]) else 0
        out[key] = as_int(layout.get(key) or 0, key.capitalize(), lowest, LAYOUT_MAX[key], allow_none=False)
        boxes_total *= max(out[key], 0)
    out["grid_rows"] = as_int(layout.get("grid_rows") or 9, "Box rows", 1, MAX_GRID)
    out["grid_cols"] = as_int(layout.get("grid_cols") or 9, "Box columns", 1, MAX_GRID)
    out["label_style"] = "number" if layout.get("label_style") == "number" else "grid"
    require(boxes_total <= MAX_BOXES_PER_UNIT,
            f"That layout makes {boxes_total} boxes — the limit is {MAX_BOXES_PER_UNIT}.")
    return out


def add_sections(conn, unit_id, holder_ids, layout):
    """Add the layout's loose-space sections (and their starting containers) to each holder:
    each new shelf, or the tank itself (None). They go after any racks already there."""
    for hid in holder_ids:
        for _ in range(layout.get("sections") or 0):
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM nodes WHERE unit_id = ? AND parent_id IS ?",
                               (unit_id, hid)).fetchone()[0]
            sp = conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, span) VALUES (?,?, 'space', ?, 1)",
                              (unit_id, hid, pos)).lastrowid
            for k in range(layout.get("containers") or 0):
                conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, kind) VALUES (?,?, 'bin', ?, ?)",
                             (unit_id, sp, k, layout["container_type"]))


def build_nodes(conn, unit_id, parent_id, level, count, layout, start_pos=0, chain=LEVELS):
    created = []
    for i in range(count):
        is_box = level == "box"
        cur = conn.execute(
            "INSERT INTO nodes (unit_id, parent_id, level, position, grid_rows, grid_cols, label_style)"
            " VALUES (?,?,?,?,?,?,?)",
            (unit_id, parent_id, level, start_pos + i,
             layout["grid_rows"] if is_box else None,
             layout["grid_cols"] if is_box else None,
             layout["label_style"] if is_box else "grid"))
        created.append(cur.lastrowid)
        child = next_level(level, chain)
        if child and layout.get(LAYOUT_KEYS[child]):
            build_nodes(conn, unit_id, cur.lastrowid, child, layout[LAYOUT_KEYS[child]], layout, 0, chain)
    return created


def owner_fields(conn, body):
    owner_id = body.get("owner_id")
    owner_id = as_int(owner_id, "Owner") if owner_id not in (None, "", "common") else None
    if owner_id:
        require(conn.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone(),
                "Unknown user")
    is_common = 1 if (body.get("is_common") or body.get("owner_id") == "common") else 0
    if is_common:
        owner_id = None
    return owner_id, is_common


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                 (token, user_id, exp))
    return token


def _user_public(u):
    return {k: u[k] for k in ("id", "name", "email", "role", "active")}


@route("GET", "/api/status", auth=False)
def status(ctx):
    conn = ctx["conn"]
    return {"api_version": API_VERSION,
            "needs_setup": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0,
            "lab_name": get_setting(conn, "lab_name", "Cold Storage")}


@route("POST", "/api/setup", auth=False)
def setup(ctx):
    conn, b = ctx["conn"], ctx["body"]
    require(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0,
            "Setup has already been done — sign in instead.", 409)
    name, email, pw = text(b, "name", 120), text(b, "email", 200), b.get("password") or ""
    lab = text(b, "lab_name", 120) or "Our lab"
    require(name and email and "@" in email, "Name and a valid email are required")
    require(len(pw) >= 6, "Password must be at least 6 characters")
    cur = conn.execute("INSERT INTO users (email, name, password_hash, role) VALUES (?,?,?, 'admin')",
                       (email, name, hash_password(pw)))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('lab_name', ?)", (lab,))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('join_code', ?)", (new_join_code(),))
    ctx["set_cookie"] = _create_session(conn, cur.lastrowid)
    conn.commit()
    return {"ok": True}


@route("POST", "/api/login", auth=False)
def login(ctx):
    conn, b = ctx["conn"], ctx["body"]
    u = one(conn.execute("SELECT * FROM users WHERE email = ?", ((b.get("email") or "").strip(),)))
    require(u and verify_password(b.get("password") or "", u["password_hash"]),
            "Wrong email or password", 401)
    require(u["active"], "This account has been deactivated", 403)
    ctx["set_cookie"] = _create_session(conn, u["id"])
    conn.commit()
    return {"ok": True}


@route("POST", "/api/logout")
def logout(ctx):
    ctx["conn"].execute("DELETE FROM sessions WHERE token = ?", (ctx["token"],))
    ctx["conn"].commit()
    ctx["set_cookie"] = ""
    return {"ok": True}


@route("GET", "/api/me")
def me(ctx):
    return {"user": _user_public(ctx["user"]),
            "lab_name": get_setting(ctx["conn"], "lab_name", "Cold Storage")}


JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I mix-ups


def new_join_code():
    return "".join(secrets.choice(JOIN_ALPHABET) for _ in range(6))


def clean_code(raw):
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


@route("GET", "/api/lab")
def get_lab(ctx):
    """Lab name and join code — visible to every member so they can invite people."""
    conn = ctx["conn"]
    return {"lab_name": get_setting(conn, "lab_name", "Cold Storage"), "join_code": get_setting(conn, "join_code")}


@route("PUT", "/api/lab", admin=True)
def update_lab(ctx):
    conn, b = ctx["conn"], ctx["body"]
    if "lab_name" in b:
        name = text(b, "lab_name", 120)
        require(name, "Lab name is required")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('lab_name', ?)", (name,))
    if b.get("regenerate_code"):
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('join_code', ?)", (new_join_code(),))
    elif "join_code" in b:
        code = clean_code(b["join_code"])
        require(4 <= len(code) <= 12, "The lab code must be 4–12 letters or digits")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('join_code', ?)", (code,))
    conn.commit()
    return get_lab(ctx)


@route("POST", "/api/join", auth=False)
def join_lab(ctx):
    """Create your own account by entering the lab code."""
    conn, b = ctx["conn"], ctx["body"]
    code = get_setting(conn, "join_code")
    require(code and hmac.compare_digest(clean_code(b.get("code")), code),
            "That lab code isn't right — ask someone in the lab (it's under Settings › Lab)", 403)
    name, email, pw = text(b, "name", 120), text(b, "email", 200), b.get("password") or ""
    require(name and email and "@" in email, "Name and a valid email are required")
    require(len(pw) >= 6, "Password must be at least 6 characters")
    existing = one(conn.execute("SELECT * FROM users WHERE email = ?", (email,)))
    require(not existing, "There's already an account with that email — sign in instead"
            + ("" if not existing or existing["active"] else " (or ask an admin to reactivate it)"), 409)
    cur = conn.execute("INSERT INTO users (email, name, password_hash, role) VALUES (?,?,?, 'member')",
                       (email, name, hash_password(pw)))
    ctx["set_cookie"] = _create_session(conn, cur.lastrowid)
    ctx["user"] = {"id": cur.lastrowid}
    log(ctx, "user.join", f"{name} joined the lab")
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

@route("GET", "/api/users")
def list_users(ctx):
    return rows(ctx["conn"].execute(
        "SELECT id, name, email, role, active FROM users ORDER BY active DESC, name COLLATE NOCASE"))


@route("POST", "/api/users", admin=True)
def create_user(ctx):
    conn, b = ctx["conn"], ctx["body"]
    name, email, pw = text(b, "name", 120), text(b, "email", 200), b.get("password") or ""
    require(name and email and "@" in email, "Name and a valid email are required")
    require(len(pw) >= 6, "Temporary password must be at least 6 characters")
    require(not conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone(),
            "Someone already uses that email")
    role = "admin" if b.get("role") == "admin" else "member"
    cur = conn.execute("INSERT INTO users (email, name, password_hash, role) VALUES (?,?,?,?)",
                       (email, name, hash_password(pw), role))
    conn.commit()
    return {"id": cur.lastrowid}


@route("PUT", "/api/users/<id>")
def update_user(ctx):
    conn, b, me_ = ctx["conn"], ctx["body"], ctx["user"]
    uid = as_int(ctx["params"]["id"], "id")
    u = one(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)))
    require(u, "User not found", 404)
    is_admin, is_self = me_["role"] == "admin", me_["id"] == uid
    require(is_admin or is_self, "Only an admin can edit other people", 403)
    if "name" in b:
        name = text(b, "name", 120)
        require(name, "Name is required")
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
    if "email" in b:
        email = text(b, "email", 200)
        require(email and "@" in email, "A valid email is required")
        dup = conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, uid)).fetchone()
        require(not dup, "Someone already uses that email")
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, uid))
    if b.get("password"):
        require(len(b["password"]) >= 6, "Password must be at least 6 characters")
        if is_self and not is_admin:
            require(verify_password(b.get("current_password") or "", u["password_hash"]),
                    "Current password is wrong")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(b["password"]), uid))
        if not is_self:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    if is_admin and "role" in b:
        role = "admin" if b["role"] == "admin" else "member"
        if role != "admin" and u["role"] == "admin":
            n = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            require(n > 1, "There must be at least one admin")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    if is_admin and "active" in b and not is_self:
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if b["active"] else 0, uid))
        if not b["active"]:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #

def _color(b):
    c = text(b, "color", 20) or "#9ca3af"
    require(re.fullmatch(r"#[0-9a-fA-F]{6}", c), "Colour must look like #a1b2c3")
    return c


@route("GET", "/api/container-types")
def list_container_types(ctx):
    return rows(ctx["conn"].execute("""
        SELECT t.*, (SELECT COUNT(*) FROM nodes n WHERE n.level = 'bin' AND n.kind = t.name) AS used
        FROM container_types t ORDER BY sort, name COLLATE NOCASE"""))


@route("POST", "/api/container-types")
def create_container_type(ctx):
    conn = ctx["conn"]
    name = text(ctx["body"], "name", 60)
    require(name, "Name is required")
    require(not conn.execute("SELECT 1 FROM container_types WHERE name = ?", (name,)).fetchone(), "That type already exists")
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM container_types").fetchone()[0]
    cur = conn.execute("INSERT INTO container_types (name, sort) VALUES (?, ?)", (name, sort))
    conn.commit()
    return {"id": cur.lastrowid}


@route("PUT", "/api/container-types/<id>")
def rename_container_type(ctx):
    conn = ctx["conn"]
    tid = as_int(ctx["params"]["id"], "id")
    old = one(conn.execute("SELECT * FROM container_types WHERE id = ?", (tid,)))
    require(old, "Not found", 404)
    name = text(ctx["body"], "name", 60)
    require(name, "Name is required")
    dup = conn.execute("SELECT 1 FROM container_types WHERE name = ? AND id != ?", (name, tid)).fetchone()
    require(not dup, "That type already exists")
    conn.execute("UPDATE container_types SET name = ? WHERE id = ?", (name, tid))
    conn.execute("UPDATE nodes SET kind = ? WHERE level = 'bin' AND kind = ?", (name, old["name"]))   # containers follow
    conn.commit()
    return {"ok": True}


@route("DELETE", "/api/container-types/<id>")
def delete_container_type(ctx):
    conn = ctx["conn"]
    tid = as_int(ctx["params"]["id"], "id")
    t = one(conn.execute("SELECT * FROM container_types WHERE id = ?", (tid,)))
    require(t, "Not found", 404)
    n = conn.execute("SELECT COUNT(*) FROM nodes WHERE level = 'bin' AND kind = ?", (t["name"],)).fetchone()[0]
    require(n == 0, f"{n} container(s) are this type — change them first.", 409)
    conn.execute("DELETE FROM container_types WHERE id = ?", (tid,))
    conn.commit()
    return {"ok": True}


FIELD_TYPES = ("text", "longtext", "number", "date", "choice")
COLUMN_FIELDS = ("concentration", "source", "lot")   # stored in the item's own columns, not in extra


def clean_fields(raw):
    """Validate a category's field list: [{key, label, type, options?}, …]."""
    require(isinstance(raw, list) and len(raw) <= 40, "Up to 40 fields per category")
    out, keys = [], set()
    for f in raw:
        require(isinstance(f, dict), "Bad field")
        label = text(f, "label", 60)
        require(label, "Every field needs a name")
        key = re.sub(r"[^a-z0-9_]", "", (f.get("key") or re.sub(r"\W+", "_", label.lower())).lower())[:40] or "field"
        base, i = key, 2
        while key in keys:
            key, i = f"{base}_{i}", i + 1
        keys.add(key)
        ftype = f.get("type") if f.get("type") in FIELD_TYPES else "text"
        d = {"key": key, "label": label, "type": ftype}
        if ftype == "choice":
            opts = [str(o).strip()[:60] for o in (f.get("options") or []) if str(o).strip()][:40]
            require(opts, f"“{label}” is a choice — give it some options")
            d["options"] = opts
        out.append(d)
    return out


def _category_out(c):
    c = dict(c)
    c["fields"] = json.loads(c["fields"]) if c.get("fields") else []
    return c


@route("GET", "/api/categories")
def list_categories(ctx):
    return [_category_out(c) for c in rows(ctx["conn"].execute("""
        SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.category_id = c.id AND i.status='stored') AS stored
        FROM categories c ORDER BY sort, name COLLATE NOCASE"""))]


@route("POST", "/api/categories")
def create_category(ctx):
    conn, b = ctx["conn"], ctx["body"]
    name = text(b, "name", 80)
    require(name, "Name is required")
    require(not conn.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone(),
            "That category already exists")
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories").fetchone()[0]
    fields = clean_fields(b["fields"]) if "fields" in b else [{"key": k, "label": k.capitalize(), "type": "text"} for k in COLUMN_FIELDS]
    cur = conn.execute("INSERT INTO categories (name, color, sort, fields) VALUES (?,?,?,?)",
                       (name, _color(b), sort, json.dumps(fields)))
    conn.commit()
    return {"id": cur.lastrowid}


@route("PUT", "/api/categories/<id>")
def update_category(ctx):
    conn, b = ctx["conn"], ctx["body"]
    cid = as_int(ctx["params"]["id"], "id")
    cur = one(conn.execute("SELECT * FROM categories WHERE id = ?", (cid,)))
    require(cur, "Not found", 404)
    if "name" not in b:
        b = dict(b, name=cur["name"], color=b.get("color", cur["color"]))
    name = text(b, "name", 80)
    require(name, "Name is required")
    dup = conn.execute("SELECT 1 FROM categories WHERE name = ? AND id != ?", (name, cid)).fetchone()
    require(not dup, "That category already exists")
    conn.execute("UPDATE categories SET name = ?, color = ? WHERE id = ?", (name, _color(b), cid))
    if "fields" in b:
        conn.execute("UPDATE categories SET fields = ? WHERE id = ?", (json.dumps(clean_fields(b["fields"])), cid))
    conn.commit()
    return {"ok": True}


@route("DELETE", "/api/categories/<id>")
def delete_category(ctx):
    conn = ctx["conn"]
    cid = as_int(ctx["params"]["id"], "id")
    n = conn.execute("SELECT COUNT(*) FROM items WHERE category_id = ?", (cid,)).fetchone()[0]
    require(n == 0, f"{n} item(s) still use this category — re-categorise them first.", 409)
    conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Storage units & layout nodes
# --------------------------------------------------------------------------- #

def _unit_fields(conn, b, partial=False):
    out = {}
    if not partial or "name" in b:
        out["name"] = text(b, "name", 120)
        require(out["name"], "Name is required")
    if not partial or "type" in b:
        require(b.get("type") in UNIT_TYPES, "Pick a storage type")
        out["type"] = b["type"]
    for k in ("location", "size", "manufacturer", "device_name", "model", "serial_number", "inventory_number", "notes"):
        if not partial or k in b:
            out[k] = text(b, k, 2000 if k == "notes" else 200)
    for k, label in (("last_defrost", "Date of last defrost"), ("last_pm", "Date of last PM")):
        if not partial or k in b:
            out[k] = as_date(b.get(k), label)
    if not partial or "owner_id" in b or "is_common" in b:
        out["owner_id"], out["is_common"] = owner_fields(conn, b)
    return out


def clean_plan(p):
    """Validate a tank arrangement (see db.CE8120_PLAN). Returns a dict or None."""
    if not p:
        return None
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            raise ApiError("Bad rack arrangement")
    require(isinstance(p, dict), "Bad rack arrangement")
    cols = as_int(p.get("cols"), "Arrangement columns", 1, 10, False)
    rows_ = as_int(p.get("rows"), "Arrangement rows", 1, 10, False)
    slots, seen = [], set()
    for sl in p.get("slots") or []:
        c, r = as_int(sl.get("c"), "Slot column", 0, cols - 1, False), as_int(sl.get("r"), "Slot row", 0, rows_ - 1, False)
        require((c, r) not in seen, "Two slots in the same place")
        seen.add((c, r))
        slots.append({"name": str(sl.get("name") or "")[:12], "c": c, "r": r})
    require(1 <= len(slots) <= 60, "An arrangement needs 1–60 slots")
    return {"cols": cols, "rows": rows_, "dividers": bool(p.get("dividers")), "slots": slots}


def unit_plan(conn, unit_id):
    r = conn.execute("SELECT plan FROM units WHERE id = ?", (unit_id,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


UNIT_LIST_SQL = """
    SELECT u.*,
      (SELECT COUNT(*) FROM items i WHERE i.unit_id = u.id AND i.status = 'stored') AS stored,
      (SELECT COUNT(*) FROM items i WHERE i.unit_id = u.id AND i.status = 'stored'
                                    AND i.pos_row IS NULL) AS loose,
      (SELECT COALESCE(SUM(grid_rows * grid_cols), 0) FROM nodes n
         WHERE n.unit_id = u.id AND n.level = 'box') AS capacity,
      (SELECT COUNT(*) FROM nodes n WHERE n.unit_id = u.id AND n.level = 'box') AS box_count
    FROM units u"""


@route("GET", "/api/units")
def list_units(ctx):
    units = rows(ctx["conn"].execute(UNIT_LIST_SQL + " ORDER BY u.location COLLATE NOCASE, u.name COLLATE NOCASE"))
    if ctx["query"].get("layout"):
        mini = _mini_layouts(ctx["conn"])
        for u in units:
            u["mini"] = mini.get(u["id"], [])
    return units


def _mini_layouts(conn):
    """For the Storage page's little diagrams: per unit, its shelves (or, for a tank, the tank) as
    lists of [kind, width, position] — kind "r" = rack, "s" = loose space. Nothing deeper."""
    nodes = rows(conn.execute("SELECT id, unit_id, parent_id, level, position, span FROM nodes "
                              "WHERE level IN ('shelf', 'rack', 'space') ORDER BY position, id"))
    by_id = {n["id"]: n for n in nodes}
    out = defaultdict(dict)                  # unit -> holder id (None = tank) -> pieces
    for n in nodes:
        if n["level"] == "shelf":
            out[n["unit_id"]].setdefault(n["id"], [])
    for n in nodes:
        if n["level"] == "shelf":
            continue
        parent = by_id.get(n["parent_id"]) if n["parent_id"] else None
        if n["parent_id"] and (not parent or parent["level"] != "shelf"):
            continue                         # spaces inside racks/rows aren't drawn here
        out[n["unit_id"]].setdefault(n["parent_id"], []).append(
            ["s" if n["level"] == "space" else "r", n["span"] or 1, n["position"]])
    shelf_order = {n["id"]: (n["position"], n["id"]) for n in nodes if n["level"] == "shelf"}
    return {uid: [holders[h] for h in sorted(holders, key=lambda h: shelf_order.get(h, (-1, 0)))]
            for uid, holders in out.items()}


@route("POST", "/api/units")
def create_unit(ctx):
    conn, b = ctx["conn"], ctx["body"]
    f = _unit_fields(conn, b)
    chain = chain_for(f["type"])
    layout = clean_layout(b.get("layout") or {}, chain[0], chain)
    plan = clean_plan(b.get("plan")) if f["type"] == "ln2" else None
    if plan and layout["racks"] != len(plan["slots"]):
        plan = None   # the arrangement only applies when every slot gets exactly one rack
    f["plan"] = json.dumps(plan) if plan else None
    cur = conn.execute(f"INSERT INTO units ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                       tuple(f.values()))
    uid = cur.lastrowid
    build_nodes(conn, uid, None, chain[0], layout[LAYOUT_KEYS[chain[0]]], layout, 0, chain)
    if layout["sections"]:
        holders = ([r[0] for r in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND level = 'shelf' ORDER BY position, id", (uid,))]
                   if chain[0] == "shelf" else [None])
        add_sections(conn, uid, holders, layout)
    if plan:   # racks take the slot names (A1, A2, …) in slot order
        racks = conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS NULL ORDER BY position, id", (uid,)).fetchall()
        for i, (rid,) in enumerate(racks):
            conn.execute("UPDATE nodes SET name = ?, position = ? WHERE id = ?", (plan["slots"][i]["name"] or None, i, rid))
    log(ctx, "unit.add", f"Added {UNIT_TYPE_LABEL[f['type']]} “{f['name']}”", unit_id=uid)
    conn.commit()
    return {"id": uid}


@route("GET", "/api/units/<id>")
def get_unit(ctx):
    conn = ctx["conn"]
    uid = as_int(ctx["params"]["id"], "id")
    unit = one(conn.execute(UNIT_LIST_SQL + " WHERE u.id = ?", (uid,)))
    require(unit, "Storage unit not found", 404)
    nodes = rows(conn.execute(
        "SELECT id, parent_id, level, position, span, kind, name, owner_id, is_common, grid_rows, grid_cols,"
        " label_style, notes FROM nodes WHERE unit_id = ? ORDER BY position, id", (uid,)))
    items = rows(conn.execute("""
        SELECT id, name, category_id, node_id, pos_row, pos_col, owner_id, is_common, quantity,
               concentration, loose_where, date_added, expiry_date
        FROM items WHERE unit_id = ? AND status = 'stored'""", (uid,)))
    return {"unit": unit, "nodes": nodes, "items": items}


@route("PUT", "/api/units/<id>")
def update_unit(ctx):
    conn = ctx["conn"]
    uid = as_int(ctx["params"]["id"], "id")
    require(conn.execute("SELECT 1 FROM units WHERE id = ?", (uid,)).fetchone(), "Not found", 404)
    f = _unit_fields(conn, ctx["body"], partial=True)
    if "plan" in ctx["body"]:
        plan = clean_plan(ctx["body"]["plan"])
        f["plan"] = json.dumps(plan) if plan else None
    if f:
        conn.execute(f"UPDATE units SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?",
                     (*f.values(), uid))
        log(ctx, "unit.edit", f"Edited storage unit “{f.get('name') or uid}”", unit_id=uid)
    conn.commit()
    return {"ok": True}


@route("DELETE", "/api/units/<id>", admin=True)
def delete_unit(ctx):
    conn = ctx["conn"]
    uid = as_int(ctx["params"]["id"], "id")
    unit = one(conn.execute("SELECT * FROM units WHERE id = ?", (uid,)))
    require(unit, "Not found", 404)
    tree = Tree(conn, uid)
    stored = rows(conn.execute("SELECT * FROM items WHERE unit_id = ? AND status='stored'", (uid,)))
    for it in stored:
        conn.execute("""UPDATE items SET status='removed', date_removed=?, removed_by=?,
                        removal_reason=?, last_location=?, updated_at=datetime('now') WHERE id=?""",
                     (today(), ctx["user"]["id"], "Storage unit deleted",
                      tree.location(it)["text"], it["id"]))
    conn.execute("UPDATE items SET last_location = COALESCE(last_location, ?) WHERE unit_id = ?",
                 (unit["name"], uid))
    conn.execute("DELETE FROM units WHERE id = ?", (uid,))
    log(ctx, "unit.delete", f"Deleted storage unit “{unit['name']}” ({len(stored)} item(s) marked removed)")
    conn.commit()
    return {"ok": True}


def _free_slots(conn, uid, holder_id):
    """Empty slot indexes in an arranged tank ([] if not arranged). Tank racks stand at the root."""
    plan = unit_plan(conn, uid)
    if not plan or holder_id is not None:
        return []
    used = {r[0] for r in conn.execute("SELECT position FROM nodes WHERE unit_id = ? AND parent_id IS NULL", (uid,))}
    return [i for i in range(len(plan["slots"])) if i not in used]


def _next_pos(conn, uid, parent_id):
    free = _free_slots(conn, uid, parent_id)
    if free:
        return free[0]
    return conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM nodes WHERE unit_id = ? AND parent_id IS ?",
        (uid, parent_id)).fetchone()[0]


def _stored_under(conn, node_id):
    ids = subtree_ids(conn, node_id)
    q = ",".join("?" * len(ids))
    return conn.execute(f"SELECT COUNT(*) FROM items WHERE status='stored' AND node_id IN ({q})", ids).fetchone()[0]


def _slot_word(level, n=1):
    """What a loose space's width is counted in, where it stands: rack-widths, rows or box-widths."""
    word = {"rack": "rack-width", "row": "row", "box": "box-width"}.get(level, "slot")
    return word + ("s" if n != 1 else "")


@route("POST", "/api/units/<id>/nodes")
def add_nodes(ctx):
    """Add children: shelves to a unit (parent_id null) or racks/rows/boxes to a node."""
    conn, b = ctx["conn"], ctx["body"]
    uid = as_int(ctx["params"]["id"], "id")
    require(conn.execute("SELECT 1 FROM units WHERE id = ?", (uid,)).fetchone(), "Not found", 404)
    chain = unit_chain(conn, uid)
    parent_id = as_int(b.get("parent_id"), "parent") or None
    parent = None
    if parent_id:
        parent = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ?", (parent_id, uid)))
        require(parent, "Parent not found", 404)
        level = next_level(parent["level"], chain)
        # Containers and loose spaces have their own rules below.
        require(level or b.get("level") in ("bin", "space"), "That can't contain other containers")
    else:
        level = chain[0]
    if b.get("level") == "bin":
        require(parent and parent["level"] == "space", "Containers go in a loose space")
        kind = text(b, "kind", 60)
        require(kind, "Pick a container type")
        count = as_int(b.get("count") or 1, "Number of containers", 1, 50)
        name = text(b, "name", 80) if count == 1 else None      # several at once are numbered: Box 1, Box 2…
        ids = []
        for _ in range(count):
            ids.append(conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, kind, name) VALUES (?,?,?,?,?,?)",
                                    (uid, parent_id, "bin", _next_pos(conn, uid, parent_id), kind, name)).lastrowid)
        log(ctx, "node.add", (f"Added {count} {kind.lower()}s" if count > 1 else f"Added a {kind.lower()}")
            + (f" “{name}”" if name else ""), unit_id=uid)
        conn.commit()
        return {"ids": ids}
    if b.get("level") == "space":
        # Loose spaces take the place of whatever normally sits there: racks on a shelf (or in a
        # tank), rows in a rack, boxes in a row (or in a tank rack).
        require((parent and parent["level"] in ("shelf", "rack", "row")) or (not parent and chain[0] == "rack"),
                "A loose space goes on a shelf, in a rack or in a row")
        span = as_int(b.get("span") or 1, "Width", 1, MAX_SPAN)
        start = _next_pos(conn, uid, parent_id)
        cur = conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, span, name) VALUES (?,?,?,?,?,?)",
                           (uid, parent_id, "space", start, span, text(b, "name", 80)))
        log(ctx, "node.add", f"Added a loose space ({span} {_slot_word(level, span)})", unit_id=uid)
        conn.commit()
        return {"ids": [cur.lastrowid]}
    count = as_int(b.get("count") or 1, "Count", 1, LAYOUT_MAX[LAYOUT_KEYS[level]])
    layout = clean_layout({**(b.get("layout") or {}), LAYOUT_KEYS[level]: count}, level, chain)
    total = conn.execute("SELECT COUNT(*) FROM nodes WHERE unit_id=? AND level='box'", (uid,)).fetchone()[0]
    per = 1
    for lvl in chain[chain.index(level):]:
        per *= layout[LAYOUT_KEYS[lvl]]
    require(total + per <= MAX_BOXES_PER_UNIT, f"That would exceed {MAX_BOXES_PER_UNIT} boxes in this unit")
    if _free_slots(conn, uid, parent_id):
        # Arranged tank: each new rack fills the next empty slot.
        ids = []
        plan = unit_plan(conn, uid)
        for _ in range(count):
            pos = _next_pos(conn, uid, parent_id)
            ids += build_nodes(conn, uid, parent_id, level, 1, layout, pos, chain)
            # Name it after its slot ("C3") unless another rack in the tank already has that name.
            slot_name = plan["slots"][pos]["name"] if pos < len(plan["slots"]) else None
            taken = conn.execute("SELECT 1 FROM nodes WHERE unit_id = ? AND parent_id IS ? AND name = ?",
                                 (uid, parent_id, slot_name)).fetchone()
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?",
                         (slot_name if slot_name and not taken else _free_name(conn, uid, parent_id, "Rack", ids[-1]), ids[-1]))
    else:
        ids = build_nodes(conn, uid, parent_id, level, count, layout, _next_pos(conn, uid, parent_id), chain)
        if level == "shelf" and layout["sections"]:
            add_sections(conn, uid, ids, layout)
    log(ctx, "node.add", f"Added {count} {LEVEL_LABEL[level].lower()}{'s' if count > 1 else ''}", unit_id=uid)
    conn.commit()
    return {"ids": ids}


@route("PUT", "/api/nodes/<id>")
def update_node(ctx):
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n, "Not found", 404)
    sets = {}
    if "name" in b:
        sets["name"] = text(b, "name", 80)
    if "notes" in b:
        sets["notes"] = text(b, "notes", 2000)
    if "owner_id" in b or "is_common" in b:
        sets["owner_id"], sets["is_common"] = owner_fields(conn, b)
    if n["level"] == "bin" and "kind" in b:
        sets["kind"] = text(b, "kind", 60)
        require(sets["kind"], "Pick a container type")
    if n["level"] == "space" and "span" in b:
        sets["span"] = as_int(b["span"], "Width", 1, MAX_SPAN, False)
    if n["level"] == "box":
        if "grid_rows" in b or "grid_cols" in b:
            gr = as_int(b.get("grid_rows", n["grid_rows"]), "Rows", 1, MAX_GRID, False)
            gc = as_int(b.get("grid_cols", n["grid_cols"]), "Columns", 1, MAX_GRID, False)
            out = conn.execute("""SELECT COUNT(*) FROM items WHERE node_id = ? AND status='stored'
                                  AND (pos_row >= ? OR pos_col >= ?)""", (nid, gr, gc)).fetchone()[0]
            require(out == 0, f"{out} item(s) sit outside a {gr}×{gc} grid — move them first.", 409)
            sets["grid_rows"], sets["grid_cols"] = gr, gc
        if "label_style" in b:
            sets["label_style"] = "number" if b["label_style"] == "number" else "grid"
    if sets:
        conn.execute(f"UPDATE nodes SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), nid))
        log(ctx, "node.edit", f"Edited {LEVEL_LABEL[n['level']].lower()} {sets.get('name') or n['name'] or ''}".strip(),
            unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True}


@route("DELETE", "/api/nodes/<id>")
def delete_node(ctx):
    conn = ctx["conn"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n, "Not found", 404)
    cnt = _stored_under(conn, nid)
    require(cnt == 0, f"{cnt} item(s) are still stored in there — move or remove them first.", 409)
    tree = Tree(conn, n["unit_id"])
    label = " › ".join(tree.label(x) for x in tree.chain(nid))
    conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
    log(ctx, "node.delete", f"Removed {label} from the layout", unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True}


def _freeze_labels(conn, tree, parent_ids):
    """Give unnamed children of these parents their current label as a real name,
    so reordering doesn't silently renumber them (and the item paths that use them)."""
    for n in tree.nodes.values():
        if n["parent_id"] in parent_ids and not n["name"]:
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (tree.label(n), n["id"]))


def _free_name(conn, unit_id, holder_id, base, exclude_id):
    """'Rack 4' / 'Loose space 2' — the first '<base> N' not used by another piece in this holder."""
    taken = {r[0] for r in conn.execute(
        "SELECT name FROM nodes WHERE unit_id = ? AND parent_id IS ? AND id != ? AND name IS NOT NULL",
        (unit_id, holder_id, exclude_id))}
    base = re.sub(r"\s*\d+$", "", base).strip() or "Rack"
    i = 1
    while f"{base} {i}" in taken:
        i += 1
    return f"{base} {i}"


@route("POST", "/api/nodes/<id>/convert")
def convert_node(ctx):
    """Swap an empty rack, row or box for a loose space in the same spot — or a loose space back
    into whatever sits beside it (a rack on a shelf, a row in a rack, a box in a row or tank rack)."""
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n and n["level"] in ("rack", "row", "box", "space"), "Only racks, rows, boxes and loose spaces can be converted")
    cnt = _stored_under(conn, nid)
    tree = Tree(conn, n["unit_id"])
    old_label = tree.label(tree.nodes[nid])
    require(cnt == 0, f"{old_label} still holds {cnt} item(s) — move or remove them first.", 409)
    chain = unit_chain(conn, n["unit_id"])
    parent = tree.nodes.get(n["parent_id"])
    kind = next_level(parent["level"], chain) if parent else chain[0]       # what normally sits here
    require(kind, "Nothing can stand here")
    # Pin the other pieces' names so converting this one doesn't renumber its neighbours.
    _freeze_labels(conn, tree, {n["parent_id"]})
    if n["level"] != "space":
        conn.execute("DELETE FROM nodes WHERE parent_id = ?", (nid,))   # its (empty) contents
        name = n["name"] if n["name"] and not re.fullmatch(rf"{LEVEL_LABEL[n['level']]} \d+", n["name"]) else None
        name = name or _free_name(conn, n["unit_id"], n["parent_id"], "Loose space", nid)
        conn.execute("UPDATE nodes SET level='space', span=1, name=? WHERE id=?", (name, nid))
        log(ctx, "node.convert", f"Replaced {old_label} with a loose space", unit_id=n["unit_id"])
    else:
        conn.execute("DELETE FROM nodes WHERE parent_id = ?", (nid,))      # its (empty) containers
        inner = next_level(kind, chain)
        layout = clean_layout(b.get("layout") or {}, inner, chain) if inner else clean_layout(b.get("layout") or {}, "box", chain)
        name = n["name"] if n["name"] and not re.fullmatch(r"Loose space \d+", n["name"]) else None
        name = name or _free_name(conn, n["unit_id"], n["parent_id"], LEVEL_LABEL[kind], nid)
        if kind == "box":
            conn.execute("UPDATE nodes SET level='box', span=1, name=?, grid_rows=?, grid_cols=?, label_style=? WHERE id=?",
                         (name, layout["grid_rows"], layout["grid_cols"], layout["label_style"], nid))
        else:
            conn.execute("UPDATE nodes SET level=?, span=1, name=? WHERE id=?", (kind, name, nid))
            build_nodes(conn, n["unit_id"], nid, inner, layout[LAYOUT_KEYS[inner]], layout, 0, chain)
        log(ctx, "node.convert", f"Replaced {old_label} with a {LEVEL_LABEL[kind].lower()}", unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True}


def _group(conn, ids, levels):
    """Load nodes for a group action: same unit, same holder, allowed levels. Returns
    (nodes in sibling order, the holder's full sibling id list)."""
    ids = [as_int(i, "id") for i in (ids or [])]
    require(ids, "Nothing selected")
    q = ",".join("?" * len(ids))
    nodes = rows(conn.execute(f"SELECT * FROM nodes WHERE id IN ({q})", ids))
    require(len(nodes) == len(set(ids)), "Some of those no longer exist — reload the page")
    require(all(n["level"] in levels for n in nodes), "Select only racks and loose spaces")
    require(len({(n["unit_id"], n["parent_id"]) for n in nodes}) == 1, "Select pieces on the same shelf")
    unit_id, holder = nodes[0]["unit_id"], nodes[0]["parent_id"]
    sibs = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS ? ORDER BY position, id",
                                       (unit_id, holder))]
    nodes.sort(key=lambda n: sibs.index(n["id"]))
    return nodes, sibs


@route("POST", "/api/nodes/to-space")
def group_to_space(ctx):
    """Turn selected racks into loose space. mode "one" (default): side-by-side racks/spaces become ONE
    loose space as wide as all of them (items in selected spaces move over). mode "each": every selected
    rack becomes its own loose space in its own spot. Racks must be empty either way."""
    conn = ctx["conn"]
    nodes, sibs = _group(conn, ctx["body"].get("ids"), SHELF_CHILDREN)
    mode = ctx["body"].get("mode") or "one"
    require(mode in ("one", "each"), "Unknown option")
    if mode == "one":
        idx = [sibs.index(n["id"]) for n in nodes]
        require(idx == list(range(idx[0], idx[0] + len(idx))), "The selected pieces must be side by side to make one space")
    for n in nodes:
        if n["level"] == "rack":
            cnt = _stored_under(conn, n["id"])
            tree0 = Tree(conn, n["unit_id"])
            require(cnt == 0, f"{tree0.label(tree0.nodes[n['id']])} still holds {cnt} item(s) — move or remove them first.", 409)
    unit_id, holder = nodes[0]["unit_id"], nodes[0]["parent_id"]
    tree = Tree(conn, unit_id)
    labels = [tree.label(tree.nodes[n["id"]]) for n in nodes]
    _freeze_labels(conn, tree, {holder})
    if mode == "each":
        made = 0
        for n in nodes:
            if n["level"] != "rack":
                continue
            conn.execute("DELETE FROM nodes WHERE parent_id = ?", (n["id"],))
            conn.execute("UPDATE nodes SET level = 'space', span = 1, name = ? WHERE id = ?",
                         (_free_name(conn, unit_id, holder, "Loose space", n["id"]), n["id"]))
            made += 1
        log(ctx, "node.convert", f"Replaced {', '.join(labels)} with {made} separate loose space(s)", unit_id=unit_id)
        conn.commit()
        return {"ok": True, "made": made}
    require(not (holder is None and unit_plan(conn, unit_id)),
            "Racks in this tank sit in fixed slots, so they can't be combined — choose a separate space for each rack")
    keep = nodes[0]
    width = sum((n["span"] or 1) if n["level"] == "space" else 1 for n in nodes)
    for n in nodes[1:]:
        conn.execute("UPDATE items SET node_id = ? WHERE node_id = ?", (keep["id"], n["id"]))
        if n["level"] == "space":
            conn.execute("UPDATE nodes SET parent_id = ? WHERE parent_id = ?", (keep["id"], n["id"]))
        conn.execute("DELETE FROM nodes WHERE id = ?", (n["id"],))
    if keep["level"] == "rack":
        conn.execute("DELETE FROM nodes WHERE parent_id = ?", (keep["id"],))  # an empty rack's rows/boxes
    name = keep["name"] if keep["level"] == "space" and keep["name"] else _free_name(conn, unit_id, holder, "Loose space", keep["id"])
    conn.execute("UPDATE nodes SET level = 'space', span = ?, name = ? WHERE id = ?", (min(width, MAX_SPAN), name, keep["id"]))
    log(ctx, "node.convert", f"Replaced {', '.join(labels)} with one loose space ({width} rack-widths)", unit_id=unit_id)
    conn.commit()
    return {"ok": True, "id": keep["id"]}


@route("POST", "/api/nodes/assign")
def group_assign(ctx):
    """Assign several racks / loose spaces to one person, to Common, or back to inheriting (owner_id null)."""
    conn, b = ctx["conn"], ctx["body"]
    nodes, _ = _group(conn, b.get("ids"), SHELF_CHILDREN + ("shelf", "row", "box"))
    owner_id, is_common = owner_fields(conn, b)
    for n in nodes:
        conn.execute("UPDATE nodes SET owner_id = ?, is_common = ? WHERE id = ?", (owner_id, is_common, n["id"]))
    tree = Tree(conn, nodes[0]["unit_id"])
    who = "Common" if is_common else (conn.execute("SELECT name FROM users WHERE id = ?", (owner_id,)).fetchone()[0]
                                      if owner_id else "nobody (inherit)")
    log(ctx, "node.edit", f"Assigned {', '.join(tree.label(tree.nodes[n['id']]) for n in nodes)} to {who}",
        unit_id=nodes[0]["unit_id"])
    conn.commit()
    return {"ok": True}


@route("POST", "/api/nodes/to-racks")
def group_to_racks(ctx):
    """Turn loose spaces back into racks: each space becomes one rack per rack-width it was."""
    conn, b = ctx["conn"], ctx["body"]
    nodes, sibs = _group(conn, b.get("ids"), ("space",))
    unit_id, holder = nodes[0]["unit_id"], nodes[0]["parent_id"]
    for n in nodes:
        require(_stored_under(conn, n["id"]) == 0, "Empty the loose spaces first — their items need a new home", 409)
    chain = unit_chain(conn, unit_id)
    inner = next_level("rack", chain)
    layout = clean_layout(b.get("layout") or {}, inner, chain)
    plan = unit_plan(conn, unit_id) if holder is None else None
    tree = Tree(conn, unit_id)
    _freeze_labels(conn, tree, {holder})
    order = list(sibs)
    made = 0
    for n in nodes:
        count = 1 if plan else (n["span"] or 1)
        conn.execute("UPDATE nodes SET level = 'rack', span = 1, name = ? WHERE id = ?",
                     (_free_name(conn, unit_id, holder, "Rack", n["id"]), n["id"]))
        build_nodes(conn, unit_id, n["id"], inner, layout[LAYOUT_KEYS[inner]], layout, 0, chain)
        extra = []
        for _ in range(count - 1):
            nid = build_nodes(conn, unit_id, holder, "rack", 1, layout, 0, chain)[0]
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (_free_name(conn, unit_id, holder, "Rack", nid), nid))
            extra.append(nid)
        at = order.index(n["id"]) + 1
        order[at:at] = extra
        made += count
    if not plan:   # arranged tanks keep slot positions; elsewhere renumber so new racks sit in place
        for i, nid in enumerate(order):
            conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, nid))
    log(ctx, "node.convert", f"Replaced {len(nodes)} loose space(s) with {made} rack(s)", unit_id=unit_id)
    conn.commit()
    return {"ok": True, "racks": made}


@route("POST", "/api/nodes/place-many")
def place_many(ctx):
    """Move a group of racks/loose spaces (or boxes) together: {ids, parent_id, before_id}.
    They keep their order and land side by side."""
    conn, b = ctx["conn"], ctx["body"]
    nodes, _ = _group(conn, b.get("ids"), ("rack", "row", "box", "space"))
    unit_id, old_holder = nodes[0]["unit_id"], nodes[0]["parent_id"]
    # They all share one holder (checked by _group); they move to another holder of the same kind.
    cur = one(conn.execute("SELECT level FROM nodes WHERE id = ?", (old_holder,))) if old_holder else None
    holder_level = cur["level"] if cur else None
    require(not (holder_level is None and unit_plan(conn, unit_id)),
            "Racks in this tank sit in fixed slots — swap them one at a time")
    if holder_level:
        target = as_int(b.get("parent_id"), holder_level.capitalize(), allow_none=False)
        require(conn.execute("SELECT 1 FROM nodes WHERE id = ? AND unit_id = ? AND level = ?",
                             (target, unit_id, holder_level)).fetchone(), f"Drop them on a {holder_level} of this unit")
    else:
        target = None
    before = as_int(b.get("before_id"), "before")
    ids = [n["id"] for n in nodes]
    require(before not in ids, "Drop the group outside itself")
    tree = Tree(conn, unit_id)
    _freeze_labels(conn, tree, {old_holder, target})
    tgt = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS ? ORDER BY position, id",
                                      (unit_id, target)) if r[0] not in ids]
    at = tgt.index(before) if before is not None else len(tgt)
    require(before is None or before in tgt, "Drop target isn't there")
    tgt[at:at] = ids
    renamed = []
    for n in nodes:
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (target, n["id"]))
        if target != old_holder:
            label = tree.label(tree.nodes[n["id"]])
            if conn.execute("SELECT 1 FROM nodes WHERE unit_id = ? AND parent_id IS ? AND id != ? AND name = ?",
                            (unit_id, target, n["id"], label)).fetchone():
                new = _free_name(conn, unit_id, target, label, n["id"])
                conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (new, n["id"]))
                renamed.append(f"{label} → {new}")
    for i, nid in enumerate(tgt):
        conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, nid))
    if target != old_holder:   # close the gap they left
        rest = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS ? ORDER BY position, id",
                                           (unit_id, old_holder))]
        for i, nid in enumerate(rest):
            conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, nid))
    where = tree.label(tree.nodes[target]) if target else tree.units[unit_id]["name"]
    log(ctx, "node.move", f"Moved {', '.join(tree.label(tree.nodes[i]) for i in ids)} together to {where}", unit_id=unit_id)
    conn.commit()
    return {"ok": True, "renamed": renamed}


@route("POST", "/api/nodes/<id>/merge")
def merge_spaces(ctx):
    """Merge two neighbouring loose spaces: {with_id}. The left one stays, its width becomes the sum,
    and the other one's items move into it."""
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    oid = as_int(b.get("with_id"), "Other space", allow_none=False)
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    o = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (oid,)))
    require(n and o and n["level"] == "space" and o["level"] == "space" and nid != oid, "Pick two loose spaces")
    require(n["unit_id"] == o["unit_id"] and n["parent_id"] == o["parent_id"], "Only spaces side by side can be merged")
    require(not (n["parent_id"] is None and unit_plan(conn, n["unit_id"])),
            "Racks in this tank sit in fixed slots, so its loose spaces can't be merged")
    sibs = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS ? ORDER BY position, id",
                                       (n["unit_id"], n["parent_id"]))]
    i, j = sibs.index(nid), sibs.index(oid)
    require(abs(i - j) == 1, "Only spaces right next to each other can be merged")
    keep, gone = (n, o) if i < j else (o, n)
    tree = Tree(conn, n["unit_id"])
    _freeze_labels(conn, tree, {n["parent_id"]})          # neighbours keep their names
    keep_label, gone_label = tree.label(tree.nodes[keep["id"]]), tree.label(tree.nodes[gone["id"]])
    moved = conn.execute("UPDATE items SET node_id = ? WHERE node_id = ?", (keep["id"], gone["id"])).rowcount
    conn.execute("UPDATE nodes SET parent_id = ? WHERE parent_id = ?", (keep["id"], gone["id"]))   # its containers
    notes = "\n".join(x for x in (keep["notes"], gone["notes"]) if x) or None
    conn.execute("UPDATE nodes SET span = ?, notes = ?, name = ? WHERE id = ?",
                 (min((keep["span"] or 1) + (gone["span"] or 1), MAX_SPAN), notes, keep_label, keep["id"]))
    conn.execute("DELETE FROM nodes WHERE id = ?", (gone["id"],))
    log(ctx, "node.merge", f"Merged {gone_label} into {keep_label}" + (f" ({moved} item(s) moved)" if moved else ""),
        unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True, "id": keep["id"]}


@route("POST", "/api/nodes/<id>/place")
def place_node(ctx):
    """Move a rack/loose space onto a shelf, or a box into a row:
    {parent_id, before_id: sibling or null = end}. Items travel with their container."""
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n and n["level"] in ("rack", "row", "box", "space", "bin"), "Only racks, rows, boxes, loose spaces and containers can be moved")
    chain = unit_chain(conn, n["unit_id"])
    # A piece moves between holders of the same kind it's in now: racks/spaces between shelves (or
    # around a tank), rows/spaces between racks, boxes/spaces between rows (tank: between racks).
    cur_parent = one(conn.execute("SELECT level FROM nodes WHERE id = ?", (n["parent_id"],))) if n["parent_id"] else None
    parent_level = cur_parent["level"] if cur_parent else None
    if parent_level:
        shelf_id = as_int(b.get("parent_id"), parent_level.capitalize(), allow_none=False)
        shelf = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ? AND level = ?",
                                 (shelf_id, n["unit_id"], parent_level)))
        require(shelf, f"That can only go into a {parent_level} of the same storage unit")
    else:
        shelf_id = None
    before = as_int(b.get("before_id"), "before")
    tree = Tree(conn, n["unit_id"])
    holder_label = lambda hid: tree.label(tree.nodes[hid]) if hid else tree.units[n["unit_id"]]["name"]
    _freeze_labels(conn, tree, {n["parent_id"], shelf_id})
    plan = unit_plan(conn, n["unit_id"]) if parent_level is None else None
    if plan:
        # Arranged tank: racks sit in fixed slots, so a drop swaps two racks or fills an empty slot.
        label = tree.label(tree.nodes[nid])
        swap = as_int(b.get("swap_with"), "swap")
        if swap is not None:
            other = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ? AND parent_id IS ?",
                                     (swap, n["unit_id"], shelf_id)))
            require(other and other["id"] != nid, "That rack isn't in this tank")
            conn.execute("UPDATE nodes SET parent_id = ?, position = ? WHERE id = ?", (n["parent_id"], n["position"], swap))
            conn.execute("UPDATE nodes SET parent_id = ?, position = ? WHERE id = ?", (shelf_id, other["position"], nid))
            log(ctx, "node.move", f"Swapped {label} and {tree.label(tree.nodes[swap])}", unit_id=n["unit_id"])
        else:
            slot = as_int(b.get("slot"), "slot")
            if slot is None:
                free = _free_slots(conn, n["unit_id"], shelf_id)
                require(free, "No empty slot there — drop it onto a rack to swap them")
                slot = free[0]
            require(0 <= slot < len(plan["slots"]), "No such slot")
            taken = conn.execute("SELECT 1 FROM nodes WHERE unit_id = ? AND parent_id IS ? AND position = ? AND id != ?",
                                 (n["unit_id"], shelf_id, slot, nid)).fetchone()
            require(not taken, "That slot is taken — drop onto the rack to swap them")
            conn.execute("UPDATE nodes SET parent_id = ?, position = ? WHERE id = ?", (shelf_id, slot, nid))
            log(ctx, "node.move", f"Moved {label} to slot {plan['slots'][slot]['name'] or slot + 1}", unit_id=n["unit_id"])
        conn.commit()
        return {"ok": True, "renamed": None}
    sibs = [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE unit_id = ? AND parent_id IS ? AND id != ? ORDER BY position, id",
        (n["unit_id"], shelf_id, nid))]
    if before is not None:
        require(before in sibs, "Drop target isn't there")
        sibs.insert(sibs.index(before), nid)
    else:
        sibs.append(nid)
    conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (shelf_id, nid))
    for i, sid in enumerate(sibs):
        conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, sid))
    label = tree.label(tree.nodes[nid])
    renamed = None
    if shelf_id != n["parent_id"]:
        dup = conn.execute("SELECT 1 FROM nodes WHERE unit_id = ? AND parent_id IS ? AND id != ? AND name = ?",
                           (n["unit_id"], shelf_id, nid, label)).fetchone()
        if dup:   # e.g. "Rack 1" dropped on a shelf that already has a "Rack 1"
            renamed = _free_name(conn, n["unit_id"], shelf_id, label, nid)
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (renamed, nid))
        log(ctx, "node.move", f"Moved {label} from {holder_label(n['parent_id'])} to {holder_label(shelf_id)}",
            unit_id=n["unit_id"])
    else:
        log(ctx, "node.move", f"Reordered {label} in {holder_label(shelf_id)}", unit_id=n["unit_id"])
    if renamed:
        log(ctx, "node.edit", f"Renamed {label} to {renamed} (that name was taken on the new shelf)", unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True, "renamed": renamed}


# --------------------------------------------------------------------------- #
# Presets (ready-made set-ups for common models)
# --------------------------------------------------------------------------- #

def _preset_out(p):
    p = dict(p)
    p["layout"] = json.loads(p["layout"])
    p["plan"] = json.loads(p["plan"]) if p["plan"] else None
    return p


@route("GET", "/api/presets")
def list_presets(ctx):
    return [_preset_out(p) for p in rows(ctx["conn"].execute(
        "SELECT * FROM presets ORDER BY type, name COLLATE NOCASE"))]


@route("POST", "/api/presets")
def create_preset(ctx):
    """Save an existing unit's set-up as a preset: {from_unit, name}."""
    conn, b = ctx["conn"], ctx["body"]
    uid = as_int(b.get("from_unit"), "Storage unit", allow_none=False)
    u = one(conn.execute("SELECT * FROM units WHERE id = ?", (uid,)))
    require(u, "Storage unit not found", 404)
    name = text(b, "name", 120) or " ".join(x for x in (u["manufacturer"], u["device_name"], u["model"]) if x) or u["name"]
    tree = Tree(conn, uid)
    kids = defaultdict(list)
    for nd in sorted(tree.nodes.values(), key=lambda x: (x["position"], x["id"])):
        kids[nd["parent_id"]].append(nd)
    # Describe the layout from the first item at each level (presets are uniform).
    chain = chain_for(u["type"])
    layout = {"grid_rows": 9, "grid_cols": 9, "label_style": "grid"}
    holder = None
    for lvl in chain:
        here = [x for x in kids[holder] if x["level"] == lvl] if holder is not False else []
        layout[LAYOUT_KEYS[lvl]] = len(here)
        holder = here[0]["id"] if here else False
        if lvl == "box" and here:
            layout.update(grid_rows=here[0]["grid_rows"], grid_cols=here[0]["grid_cols"], label_style=here[0]["label_style"])
    cur = conn.execute(
        "INSERT INTO presets (name, type, manufacturer, device_name, model, size, layout, plan) VALUES (?,?,?,?,?,?,?,?)",
        (name, u["type"], u["manufacturer"], u["device_name"], u["model"], u["size"], json.dumps(layout), u["plan"]))
    conn.commit()
    return {"id": cur.lastrowid, "name": name}


@route("PUT", "/api/presets/<id>")
def update_preset(ctx):
    conn, b = ctx["conn"], ctx["body"]
    pid = as_int(ctx["params"]["id"], "id")
    require(conn.execute("SELECT 1 FROM presets WHERE id = ?", (pid,)).fetchone(), "Preset not found", 404)
    sets = {}
    for k in ("name", "manufacturer", "device_name", "model", "size", "notes"):
        if k in b:
            sets[k] = text(b, k, 200)
    if "name" in sets:
        require(sets["name"], "Name is required")
    if "layout" in b:
        chain = chain_for(conn.execute("SELECT type FROM presets WHERE id = ?", (pid,)).fetchone()[0])
        sets["layout"] = json.dumps(clean_layout(b["layout"] or {}, chain[0], chain))
    if sets:
        conn.execute(f"UPDATE presets SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
    conn.commit()
    return {"ok": True}


@route("DELETE", "/api/presets/<id>")
def delete_preset(ctx):
    ctx["conn"].execute("DELETE FROM presets WHERE id = ?", (as_int(ctx["params"]["id"], "id"),))
    ctx["conn"].commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #

def _check_place(conn, unit_id, node_id, pos, exclude_item=None):
    """Validate a destination; returns the node row (or None for unit-level loose)."""
    require(conn.execute("SELECT 1 FROM units WHERE id = ?", (unit_id,)).fetchone(), "Storage unit not found", 404)
    # Everything has a place on the diagram: tubes go in a box, loose items in a loose space.
    require(node_id, "Loose items go in a loose space — pick one, or add a loose space first")
    node = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ?", (node_id, unit_id)))
    require(node, "Location not found", 404)
    if node["level"] == "box":
        require(pos is not None, "Pick a position in the box")
        r, c = pos
        require(0 <= r < node["grid_rows"] and 0 <= c < node["grid_cols"], "Position is outside the box")
        taken = conn.execute(
            "SELECT name FROM items WHERE node_id = ? AND status='stored' AND pos_row = ? AND pos_col = ? AND id IS NOT ?",
            (node_id, r, c, exclude_item)).fetchone()
        require(not taken, f"That position already holds “{taken[0] if taken else ''}”", 409)
    else:
        require(node["level"] in ("space", "bin"), "Loose items go in a loose space (or a container in one) — pick one, or add a loose space first")
        require(pos is None, "Positions only exist inside boxes")
    return node


def _item_values(conn, b, partial=False):
    out = {}
    if not partial or "name" in b:
        out["name"] = text(b, "name", 200)
        require(out["name"], "Item name is required")
    if not partial or "category_id" in b:
        cid = as_int(b.get("category_id"), "Category")
        if cid:
            require(conn.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone(), "Unknown category")
        out["category_id"] = cid
    if not partial or "owner_id" in b or "is_common" in b:
        out["owner_id"], out["is_common"] = owner_fields(conn, b)
    for k, n in (("quantity", 80), ("concentration", 80), ("source", 200), ("lot", 120),
                 ("notes", 4000), ("loose_where", 120)):
        if not partial or k in b:
            out[k] = text(b, k, n)
    if not partial or "extra" in b:
        raw = b.get("extra") or {}
        require(isinstance(raw, dict) and len(raw) <= 40, "Bad details")
        clean = {re.sub(r"[^a-z0-9_]", "", str(k).lower())[:40]: str(v).strip()[:2000] for k, v in raw.items()
                 if str(v or "").strip()}
        out["extra"] = json.dumps(clean) if clean else None
    if not partial or "expiry_date" in b:
        out["expiry_date"] = as_date(b.get("expiry_date"), "Expiry date")
    if not partial or "date_added" in b:
        out["date_added"] = as_date(b.get("date_added"), "Date added") or today()
    return out


def _parse_pos(p):
    if p is None:
        return None
    require(isinstance(p, (list, tuple)) and len(p) == 2, "Bad position")
    return (as_int(p[0], "Row", 0), as_int(p[1], "Column", 0))


def _item_full(conn, item_id, tree=None):
    it = one(conn.execute("""
        SELECT i.*, c.name AS category_name, c.color AS category_color,
               ua.name AS added_by_name, ur.name AS removed_by_name
        FROM items i LEFT JOIN categories c ON c.id = i.category_id
        LEFT JOIN users ua ON ua.id = i.added_by LEFT JOIN users ur ON ur.id = i.removed_by
        WHERE i.id = ?""", (item_id,)))
    require(it, "Item not found", 404)
    if tree is None:
        tree = Tree(conn, it["unit_id"] if it["unit_id"] else -1)
    it["location"] = tree.location(it)
    eo, ec, src = tree.effective_owner(it)
    it["eff_owner_id"], it["eff_common"], it["owner_from"] = eo, ec, src
    return it


@route("POST", "/api/items")
def create_items(ctx):
    conn, b = ctx["conn"], ctx["body"]
    unit_id = as_int(b.get("unit_id"), "Storage unit", allow_none=False)
    node_id = as_int(b.get("node_id"), "Location")
    positions = [_parse_pos(p) for p in (b.get("positions") or [])] or [None]
    require(len(positions) <= 500, "Too many positions at once")
    require(len(set(positions)) == len(positions), "Duplicate positions")
    vals = _item_values(conn, b)
    tree = Tree(conn, unit_id)
    ids = []
    for pos in positions:
        node = _check_place(conn, unit_id, node_id, pos)
        if node is None or node["level"] != "box":
            pos = None
        cols = dict(vals, unit_id=unit_id, node_id=node_id,
                    pos_row=pos[0] if pos else None, pos_col=pos[1] if pos else None,
                    added_by=ctx["user"]["id"], status="stored")
        if pos is None and len(positions) > 1:
            raise ApiError("Positions only exist inside boxes")
        cols["last_location"] = tree.location(cols)["text"]
        cur = conn.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           tuple(cols.values()))
        ids.append(cur.lastrowid)
        log(ctx, "item.add", f"Added “{vals['name']}” to {cols['last_location']}",
            item_id=cur.lastrowid, unit_id=unit_id)
    conn.commit()
    return {"ids": ids}


@route("GET", "/api/items/<id>")
def get_item(ctx):
    conn = ctx["conn"]
    it = _item_full(conn, as_int(ctx["params"]["id"], "id"))
    it["history"] = rows(conn.execute("""
        SELECT a.at, a.action, a.summary, u.name AS user_name FROM activity a
        LEFT JOIN users u ON u.id = a.user_id WHERE a.item_id = ? ORDER BY a.id DESC""", (it["id"],)))
    return it


@route("PUT", "/api/items/<id>")
def update_item(ctx):
    conn = ctx["conn"]
    iid = as_int(ctx["params"]["id"], "id")
    old = one(conn.execute("SELECT * FROM items WHERE id = ?", (iid,)))
    require(old, "Item not found", 404)
    vals = _item_values(conn, ctx["body"], partial=True)
    if "date_removed" in ctx["body"] and old["status"] == "removed":
        vals["date_removed"] = as_date(ctx["body"]["date_removed"], "Date removed") or old["date_removed"]
    if "removal_reason" in ctx["body"] and old["status"] == "removed":
        vals["removal_reason"] = text(ctx["body"], "removal_reason", 300)
    changed = [k for k, v in vals.items() if old.get(k) != v]
    if changed:
        conn.execute(f"UPDATE items SET {', '.join(k + ' = ?' for k in changed)}, updated_at = datetime('now') WHERE id = ?",
                     (*[vals[k] for k in changed], iid))
        nice = ", ".join("details" if k == "extra" else k.replace("_id", "").replace("_", " ") for k in changed)
        log(ctx, "item.edit", f"Edited “{vals.get('name', old['name'])}” ({nice})", item_id=iid, unit_id=old["unit_id"])
    conn.commit()
    return {"ok": True}


def _remove(ctx, it, date, reason, tree):
    loc = tree.location(it)["text"]
    ctx["conn"].execute("""UPDATE items SET status='removed', date_removed=?, removed_by=?, removal_reason=?,
                           last_location=?, updated_at=datetime('now') WHERE id=?""",
                        (date, ctx["user"]["id"], reason, loc, it["id"]))
    log(ctx, "item.remove", f"Removed “{it['name']}” from {loc}" + (f" — {reason}" if reason else ""),
        item_id=it["id"], unit_id=it["unit_id"])


@route("POST", "/api/items/remove")
def remove_items(ctx):
    conn, b = ctx["conn"], ctx["body"]
    ids = [as_int(i, "id") for i in (b.get("ids") or [])]
    require(ids, "Nothing selected")
    date = as_date(b.get("date"), "Date removed") or today()
    reason = text(b, "reason", 300)
    q = ",".join("?" * len(ids))
    items = rows(conn.execute(f"SELECT * FROM items WHERE id IN ({q}) AND status='stored'", ids))
    trees = {}
    for it in items:
        uid = it["unit_id"]
        trees.setdefault(uid, Tree(conn, uid))
        _remove(ctx, it, date, reason, trees[uid])
    conn.commit()
    return {"removed": len(items)}


@route("POST", "/api/items/<id>/restore")
def restore_item(ctx):
    conn = ctx["conn"]
    iid = as_int(ctx["params"]["id"], "id")
    it = one(conn.execute("SELECT * FROM items WHERE id = ?", (iid,)))
    require(it and it["status"] == "removed", "Item isn't removed", 400)
    require(it["unit_id"], "Its storage unit no longer exists — add it again instead.", 409)
    pos = (it["pos_row"], it["pos_col"]) if it["pos_row"] is not None else None
    if it["node_id"] is None and pos is not None:
        raise ApiError("Its box no longer exists — add it again instead.", 409)
    _check_place(conn, it["unit_id"], it["node_id"], pos, exclude_item=iid)
    conn.execute("""UPDATE items SET status='stored', date_removed=NULL, removed_by=NULL,
                    removal_reason=NULL, updated_at=datetime('now') WHERE id=?""", (iid,))
    log(ctx, "item.restore", f"Put “{it['name']}” back ({it['last_location']})", item_id=iid, unit_id=it["unit_id"])
    conn.commit()
    return {"ok": True}


@route("POST", "/api/items/<id>/move")
def move_item(ctx):
    conn, b = ctx["conn"], ctx["body"]
    iid = as_int(ctx["params"]["id"], "id")
    it = one(conn.execute("SELECT * FROM items WHERE id = ?", (iid,)))
    require(it and it["status"] == "stored", "Only stored items can be moved")
    unit_id = as_int(b.get("unit_id"), "Storage unit", allow_none=False)
    node_id = as_int(b.get("node_id"), "Location")
    pos = _parse_pos(b.get("pos"))
    node = _check_place(conn, unit_id, node_id, pos, exclude_item=iid)
    if node is None or node["level"] != "box":
        pos = None
    old_loc = Tree(conn, it["unit_id"]).location(it)["text"]
    new = dict(it, unit_id=unit_id, node_id=node_id, pos_row=pos[0] if pos else None,
               pos_col=pos[1] if pos else None, loose_where=text(b, "loose_where", 120) if not pos else None)
    new_loc = Tree(conn, unit_id).location(new)["text"]
    conn.execute("""UPDATE items SET unit_id=?, node_id=?, pos_row=?, pos_col=?, loose_where=?, last_location=?,
                    updated_at=datetime('now') WHERE id=?""",
                 (unit_id, node_id, new["pos_row"], new["pos_col"], new["loose_where"], new_loc, iid))
    log(ctx, "item.move", f"Moved “{it['name']}” from {old_loc} to {new_loc}", item_id=iid, unit_id=unit_id)
    conn.commit()
    return {"ok": True}


def _search(ctx):
    conn, q = ctx["conn"], ctx["query"]
    where, args = [], []
    st = q.get("status", "stored")
    if st in ("stored", "removed"):
        where.append("i.status = ?")
        args.append(st)
    if q.get("category_id"):
        where.append("i.category_id = ?")
        args.append(as_int(q["category_id"], "category"))
    if q.get("unit_id"):
        where.append("i.unit_id = ?")
        args.append(as_int(q["unit_id"], "unit"))
    if q.get("unit_type") in UNIT_TYPES:
        where.append("u.type = ?")
        args.append(q["unit_type"])
    for key, col in (("added_from", "i.date_added >= ?"), ("added_to", "i.date_added <= ?"),
                     ("removed_from", "i.date_removed >= ?"), ("removed_to", "i.date_removed <= ?")):
        if q.get(key):
            where.append(col)
            args.append(as_date(q[key], key))
    sql = """SELECT i.*, c.name AS category_name, c.color AS category_color, u.type AS unit_type
             FROM items i LEFT JOIN categories c ON c.id = i.category_id
             LEFT JOIN units u ON u.id = i.unit_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.date_added DESC, i.id DESC"
    items = rows(conn.execute(sql, args))
    users = {u["id"]: u["name"] for u in rows(conn.execute("SELECT id, name FROM users"))}
    tree = Tree(conn)
    owner = q.get("owner")
    if owner == "me":
        owner = str(ctx["user"]["id"])
    tokens = [t.lower() for t in re.findall(r'"[^"]+"|\S+', q.get("q", "")) if t]
    tokens = [t.strip('"') for t in tokens]
    out = []
    for it in items:
        it["location"] = tree.location(it)
        eo, ec, _ = tree.effective_owner(it)
        it["eff_owner_id"], it["eff_common"] = eo, ec
        if owner == "common" and not ec:
            continue
        if owner == "none" and (eo or ec):
            continue
        if owner and owner not in ("common", "none") and str(eo) != owner:
            continue
        if tokens:
            hay = " ".join(str(it.get(k) or "") for k in (
                "name", "notes", "source", "lot", "concentration", "quantity", "category_name",
                "loose_where", "removal_reason")).lower()
            if it.get("extra"):
                hay += " " + " ".join(json.loads(it["extra"]).values()).lower()
            hay += " " + it["location"]["text"].lower() + " " + (users.get(eo, "").lower())
            if not all(t in hay for t in tokens):
                continue
        out.append(it)
    return out, users


@route("GET", "/api/items")
def search_items(ctx):
    out, _ = _search(ctx)
    limit = as_int(ctx["query"].get("limit") or 500, "limit", 1, 5000)
    return {"total": len(out), "items": out[:limit]}


@route("GET", "/api/items.csv")
def export_items(ctx):
    out, users = _search(ctx)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Name", "Category", "Owner", "Location", "Position", "Quantity", "Concentration",
                "Source", "Lot", "Expiry", "Date added", "Status", "Date removed", "Removal reason", "Notes", "Details"])
    labels = {}
    for c in rows(ctx["conn"].execute("SELECT id, fields FROM categories")):
        labels[c["id"]] = {f["key"]: f["label"] for f in (json.loads(c["fields"]) if c["fields"] else [])}
    for it in out:
        own = "Common" if it["eff_common"] else users.get(it["eff_owner_id"], "")
        w.writerow([it["id"], it["name"], it.get("category_name") or "", own, it["location"]["text"],
                    it["location"]["pos"] or "", it["quantity"] or "", it["concentration"] or "",
                    it["source"] or "", it["lot"] or "", it["expiry_date"] or "", it["date_added"],
                    it["status"], it["date_removed"] or "", it["removal_reason"] or "", it["notes"] or "",
                    "; ".join(f"{labels.get(it['category_id'], {}).get(k, k)}: {v}"
                              for k, v in (json.loads(it["extra"]) if it.get("extra") else {}).items())])
    return ("text/csv; charset=utf-8", buf.getvalue().encode("utf-8-sig"),
            f"cold-storage-{today()}.csv")


# --------------------------------------------------------------------------- #
# Activity & dashboard
# --------------------------------------------------------------------------- #

@route("GET", "/api/activity")
def activity(ctx):
    q = ctx["query"]
    where, args = [], []
    for key, col in (("unit_id", "a.unit_id"), ("user_id", "a.user_id"), ("item_id", "a.item_id")):
        if q.get(key):
            where.append(f"{col} = ?")
            args.append(as_int(q[key], key))
    if q.get("action"):
        where.append("a.action LIKE ?")
        args.append(q["action"] + "%")
    if q.get("from"):
        where.append("date(a.at) >= ?")
        args.append(as_date(q["from"], "from"))
    if q.get("to"):
        where.append("date(a.at) <= ?")
        args.append(as_date(q["to"], "to"))
    sql = f"""SELECT a.id, a.at, a.user_id, a.action, a.item_id, a.unit_id, a.summary, a.batch, a.undone_at,
                     u.name AS user_name, ub.name AS undone_by_name,
                     (a.undo IS NOT NULL AND a.undone_at IS NULL AND NOT EXISTS ({LATER_CHANGE_SQL})) AS can_undo
              FROM activity a LEFT JOIN users u ON u.id = a.user_id LEFT JOIN users ub ON ub.id = a.undone_by"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id DESC LIMIT ?"
    args.append(as_int(q.get("limit") or 200, "limit", 1, 2000))
    out = rows(ctx["conn"].execute(sql, args))
    # A batch (one action) is undoable only if every row in it is.
    blocked = {r["batch"] for r in out if r["batch"] and not r["can_undo"] and not r["undone_at"]
               and r["action"] not in ("undo",)}
    for r in out:
        r["can_undo"] = bool(r["can_undo"]) and r["batch"] not in blocked
    return out


# A later, still-standing change that touches the same thing (the item, or for layout and unit
# changes the whole unit) blocks undo: undo the newest change first.
LATER_CHANGE_SQL = """
    SELECT 1 FROM activity b
    WHERE b.id > a.id AND b.undone_at IS NULL AND b.action != 'undo' AND b.batch IS NOT a.batch
      AND ((a.action LIKE 'item.%' AND b.item_id = a.item_id)
        OR (a.action NOT LIKE 'item.%' AND b.unit_id = a.unit_id)
        OR (a.action LIKE 'item.%' AND b.action LIKE 'node.%' AND b.unit_id = a.unit_id))"""


@route("POST", "/api/activity/<id>/undo")
def undo_activity(ctx):
    """Undo one action (all log rows from the same request), newest row first."""
    conn = ctx["conn"]
    aid = as_int(ctx["params"]["id"], "id")
    a = one(conn.execute("SELECT * FROM activity WHERE id = ?", (aid,)))
    require(a, "Not found", 404)
    batch = rows(conn.execute("SELECT * FROM activity WHERE batch = ? ORDER BY id DESC", (a["batch"],))) if a["batch"] else [a]
    require(not any(r["undone_at"] for r in batch), "That was already undone")
    require(all(r["undo"] for r in batch if r["action"] not in ("node.edit",)) and any(r["undo"] for r in batch),
            "This kind of change can't be undone")
    for r in batch:
        later = conn.execute(f"SELECT 1 FROM activity a WHERE a.id = ? AND EXISTS ({LATER_CHANGE_SQL})", (r["id"],)).fetchone()
        require(not later, "Something changed there afterwards — undo the newer change first (undo works newest-first)", 409)
    for r in batch:
        if r["undo"]:
            _apply_undo(conn, json.loads(r["undo"]), r)
    for r in batch:
        conn.execute("UPDATE activity SET undone_at = datetime('now'), undone_by = ? WHERE id = ?", (ctx["user"]["id"], r["id"]))
    what = a["summary"] if len(batch) == 1 else f"{len(batch)} changes ({batch[-1]['summary']}…)"
    conn.execute("INSERT INTO activity (user_id, action, item_id, unit_id, summary) VALUES (?,?,?,?,?)",
                 (ctx["user"]["id"], "undo", a["item_id"] if len(batch) == 1 else None, a["unit_id"], f"Undid: {lowerfirst(what)}"))
    conn.commit()
    return {"ok": True, "count": len(batch)}


def lowerfirst(t):
    return t[:1].lower() + t[1:] if t else t


ITEM_RESTORE_COLS = ("unit_id", "node_id", "pos_row", "pos_col", "loose_where", "name", "category_id", "owner_id",
                     "is_common", "quantity", "concentration", "source", "lot", "expiry_date", "notes", "status",
                     "date_added", "date_removed", "removed_by", "removal_reason", "last_location", "extra")
UNIT_RESTORE_COLS = ("name", "location", "size", "manufacturer", "device_name", "model", "serial_number",
                     "inventory_number", "last_defrost", "last_pm", "notes", "owner_id", "is_common", "plan")


def _apply_undo(conn, u, row):
    kind = u["kind"]
    if kind == "item":
        before, iid = u["before"], row["item_id"]
        if before is None:      # it was added → take it out again entirely
            require(iid and conn.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone(), "That item no longer exists")
            conn.execute("UPDATE activity SET item_id = NULL WHERE item_id = ? AND id != ?", (iid, row["id"]))
            conn.execute("DELETE FROM items WHERE id = ?", (iid,))
            return
        if before["status"] == "stored":
            require(before["unit_id"] and conn.execute("SELECT 1 FROM units WHERE id = ?", (before["unit_id"],)).fetchone(),
                    "Its storage unit no longer exists")
            if before["node_id"]:
                require(conn.execute("SELECT 1 FROM nodes WHERE id = ?", (before["node_id"],)).fetchone(),
                        "Where it was no longer exists in the layout")
            if before["pos_row"] is None:
                lvl = conn.execute("SELECT level FROM nodes WHERE id = ?", (before["node_id"],)).fetchone() if before["node_id"] else None
                require(lvl and lvl[0] == "space", "It was a loose item outside a loose space — use Move to put it in one")
            if before["pos_row"] is not None:
                taken = conn.execute("SELECT name FROM items WHERE node_id = ? AND pos_row = ? AND pos_col = ? AND status = 'stored'"
                                     " AND id != ?", (before["node_id"], before["pos_row"], before["pos_col"], iid)).fetchone()
                require(not taken, f"Its old position is now taken by “{taken[0] if taken else ''}”", 409)
        conn.execute(f"UPDATE items SET {', '.join(c + ' = ?' for c in ITEM_RESTORE_COLS)}, updated_at = datetime('now') WHERE id = ?",
                     (*[before.get(c) for c in ITEM_RESTORE_COLS], iid))
    elif kind == "layout":
        uid = u["unit_id"]
        require(conn.execute("SELECT 1 FROM units WHERE id = ?", (uid,)).fetchone(), "That storage unit no longer exists")
        conn.execute("DELETE FROM nodes WHERE unit_id = ?", (uid,))
        # Re-insert parents before children so every parent reference is valid as it's written.
        pending, done = list(u["nodes"]), set()
        while pending:
            ready = [n for n in pending if n["parent_id"] is None or n["parent_id"] in done]
            require(ready, "The saved layout is inconsistent — can't undo this one")
            for n in ready:
                conn.execute(f"INSERT INTO nodes ({', '.join(n)}) VALUES ({', '.join('?' * len(n))})", tuple(n.values()))
                done.add(n["id"])
            pending = [n for n in pending if n["id"] not in done]
        for it in u["items"]:
            conn.execute("UPDATE items SET unit_id = ?, node_id = ?, pos_row = ?, pos_col = ?, status = ? WHERE id = ?",
                         (it["unit_id"], it["node_id"], it["pos_row"], it["pos_col"], it["status"], it["id"]))
    elif kind == "unit":
        b = u["before"]
        require(b, "Nothing to restore")
        conn.execute(f"UPDATE units SET {', '.join(c + ' = ?' for c in UNIT_RESTORE_COLS)} WHERE id = ?",
                     (*[b.get(c) for c in UNIT_RESTORE_COLS], row["unit_id"]))
    elif kind == "unit.add":
        n = conn.execute("SELECT COUNT(*) FROM items WHERE unit_id = ?", (row["unit_id"],)).fetchone()[0]
        require(n == 0, "Items have been added to it since — remove them first", 409)
        conn.execute("DELETE FROM units WHERE id = ?", (row["unit_id"],))


@route("GET", "/api/stats")
def stats(ctx):
    conn = ctx["conn"]
    return {
        "stored": conn.execute("SELECT COUNT(*) FROM items WHERE status='stored'").fetchone()[0],
        "added_30d": conn.execute("SELECT COUNT(*) FROM items WHERE date_added >= date('now','-30 day')").fetchone()[0],
        "removed_30d": conn.execute("SELECT COUNT(*) FROM items WHERE status='removed' AND date_removed >= date('now','-30 day')").fetchone()[0],
        "expiring": conn.execute("SELECT COUNT(*) FROM items WHERE status='stored' AND expiry_date IS NOT NULL AND expiry_date <= date('now','+30 day')").fetchone()[0],
        "mine": None,
    }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ColdStorage/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("COLDSTORAGE_QUIET"):
            return
        super().log_message(fmt, *args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status, body, ctype="application/json", cookie=None, filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if cookie is not None:
            if cookie:
                self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; "
                                               f"Max-Age={SESSION_DAYS * 86400}" + ("; Secure" if SECURE_COOKIE else ""))
            else:
                self.send_header("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            if method != "GET":
                return self._send(405, {"error": "Method not allowed"})
            return self._static(path)
        for m, regex, fn, auth, admin in ROUTES:
            match = regex.match(path)
            if m != method or not match:
                continue
            conn = db.get_conn()
            ctx = {"conn": conn, "params": match.groupdict(),
                   "query": {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()},
                   "token": self._token(), "user": None}
            try:
                if ctx["token"]:
                    ctx["user"] = one(conn.execute("""
                        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                        WHERE s.token = ? AND s.expires_at > ? AND u.active = 1""",
                        (ctx["token"], datetime.datetime.utcnow().isoformat())))
                if auth and not ctx["user"]:
                    raise ApiError("Please sign in", 401)
                if admin and ctx["user"]["role"] != "admin":
                    raise ApiError("Only an admin can do that", 403)
                if method in ("POST", "PUT"):
                    n = int(self.headers.get("Content-Length") or 0)
                    require(n <= MAX_BODY, "Request too large", 413)
                    raw = self.rfile.read(n) if n else b""
                    try:
                        ctx["body"] = json.loads(raw or b"{}")
                    except ValueError:
                        raise ApiError("Bad JSON")
                    require(isinstance(ctx["body"], dict), "Bad JSON")
                else:
                    ctx["body"] = {}
                result = fn(ctx)
            except ApiError as e:
                conn.rollback()
                return self._send(e.status, {"error": str(e)})
            except Exception as e:  # pragma: no cover
                conn.rollback()
                import traceback
                traceback.print_exc()
                return self._send(500, {"error": f"Server error: {e}"})
            if isinstance(result, tuple):
                ctype, data, fname = result
                return self._send(200, data, ctype, filename=fname)
            return self._send(200, result, cookie=ctx.get("set_cookie"))
        return self._send(404, {"error": "Not found"})

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))


def main():
    db.init_db()
    port = int(os.environ.get("PORT", "8100"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan = "your-ip"
    print(f"Cold Storage running on http://localhost:{port}  (LAN: http://{lan}:{port})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
