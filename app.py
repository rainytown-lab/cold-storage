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
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_DAYS = 30
COOKIE = "cs_session"
MAX_BODY = 4 * 1024 * 1024

UNIT_TYPES = ("ln2", "-80", "-20", "+4")
UNIT_TYPE_LABEL = {"ln2": "Liquid nitrogen", "-80": "−80 °C freezer",
                   "-20": "−20 °C freezer", "+4": "+4 °C fridge"}
LEVELS = ("shelf", "rack", "row", "box")
LEVEL_LABEL = {"shelf": "Shelf", "rack": "Rack", "row": "Row", "box": "Box", "space": "Loose space"}
SHELF_CHILDREN = ("rack", "space")   # things that sit side by side on a shelf
MAX_SPAN = 30

# Bump when the API changes, so an already-open page can tell the server needs a restart.
API_VERSION = 5
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
    ctx["conn"].execute(
        "INSERT INTO activity (user_id, action, item_id, unit_id, summary) VALUES (?,?,?,?,?)",
        (ctx["user"]["id"] if ctx.get("user") else None, action, item_id, unit_id, summary))


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
            groups[(n["unit_id"], n["parent_id"], n["level"])].append(n)
        for g in groups.values():
            g.sort(key=lambda n: (n["position"], n["id"]))
            for i, n in enumerate(g):
                n["index"] = i

    def label(self, n):
        return n["name"] or f"{LEVEL_LABEL[n['level']]} {n['index'] + 1}"

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
        elif item.get("pos_row") is None and chain and chain[-1]["level"] == "space":
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


def next_level(level):
    i = LEVELS.index(level)
    return LEVELS[i + 1] if i + 1 < len(LEVELS) else None


def clean_layout(layout, from_level):
    """Validate the per-level counts needed below (and including) from_level."""
    out = {}
    start = LEVELS.index(from_level)
    boxes_total = 1
    for lvl in LEVELS[start:]:
        key = LAYOUT_KEYS[lvl]
        out[key] = as_int(layout.get(key), key.capitalize(), 0 if lvl != from_level else 1,
                          LAYOUT_MAX[key], allow_none=False)
        boxes_total *= max(out[key], 0)
    out["grid_rows"] = as_int(layout.get("grid_rows") or 9, "Box rows", 1, MAX_GRID)
    out["grid_cols"] = as_int(layout.get("grid_cols") or 9, "Box columns", 1, MAX_GRID)
    out["label_style"] = "number" if layout.get("label_style") == "number" else "grid"
    require(boxes_total <= MAX_BOXES_PER_UNIT,
            f"That layout makes {boxes_total} boxes — the limit is {MAX_BOXES_PER_UNIT}.")
    return out


def build_nodes(conn, unit_id, parent_id, level, count, layout, start_pos=0):
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
        child = next_level(level)
        if child and layout.get(LAYOUT_KEYS[child]):
            build_nodes(conn, unit_id, cur.lastrowid, child, layout[LAYOUT_KEYS[child]], layout)
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


@route("GET", "/api/categories")
def list_categories(ctx):
    return rows(ctx["conn"].execute("""
        SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.category_id = c.id AND i.status='stored') AS stored
        FROM categories c ORDER BY sort, name COLLATE NOCASE"""))


@route("POST", "/api/categories")
def create_category(ctx):
    conn, b = ctx["conn"], ctx["body"]
    name = text(b, "name", 80)
    require(name, "Name is required")
    require(not conn.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone(),
            "That category already exists")
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories").fetchone()[0]
    cur = conn.execute("INSERT INTO categories (name, color, sort) VALUES (?,?,?)",
                       (name, _color(b), sort))
    conn.commit()
    return {"id": cur.lastrowid}


@route("PUT", "/api/categories/<id>")
def update_category(ctx):
    conn, b = ctx["conn"], ctx["body"]
    cid = as_int(ctx["params"]["id"], "id")
    name = text(b, "name", 80)
    require(name, "Name is required")
    dup = conn.execute("SELECT 1 FROM categories WHERE name = ? AND id != ?", (name, cid)).fetchone()
    require(not dup, "That category already exists")
    conn.execute("UPDATE categories SET name = ?, color = ? WHERE id = ?", (name, _color(b), cid))
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
    return rows(ctx["conn"].execute(UNIT_LIST_SQL + " ORDER BY u.location COLLATE NOCASE, u.name COLLATE NOCASE"))


@route("POST", "/api/units")
def create_unit(ctx):
    conn, b = ctx["conn"], ctx["body"]
    f = _unit_fields(conn, b)
    layout = clean_layout(b.get("layout") or {}, "shelf")
    plan = clean_plan(b.get("plan")) if f["type"] == "ln2" else None
    if plan and layout["racks"] != len(plan["slots"]):
        plan = None   # the arrangement only applies when every slot gets exactly one rack
    f["plan"] = json.dumps(plan) if plan else None
    cur = conn.execute(f"INSERT INTO units ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                       tuple(f.values()))
    uid = cur.lastrowid
    build_nodes(conn, uid, None, "shelf", layout["shelves"], layout)
    if plan:   # racks take the slot names (A1, A2, …) in slot order
        for shelf_id, in conn.execute("SELECT id FROM nodes WHERE unit_id = ? AND level = 'shelf'", (uid,)).fetchall():
            racks = conn.execute("SELECT id FROM nodes WHERE parent_id = ? ORDER BY position, id", (shelf_id,)).fetchall()
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
        "SELECT id, parent_id, level, position, span, name, owner_id, is_common, grid_rows, grid_cols,"
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


def _free_slots(conn, uid, shelf_id):
    """Empty slot indexes on a shelf of an arranged tank ([] if not arranged)."""
    plan = unit_plan(conn, uid)
    if not plan or not shelf_id:
        return []
    lvl = conn.execute("SELECT level FROM nodes WHERE id = ?", (shelf_id,)).fetchone()
    if not lvl or lvl[0] != "shelf":
        return []
    used = {r[0] for r in conn.execute("SELECT position FROM nodes WHERE parent_id = ?", (shelf_id,))}
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


@route("POST", "/api/units/<id>/nodes")
def add_nodes(ctx):
    """Add children: shelves to a unit (parent_id null) or racks/rows/boxes to a node."""
    conn, b = ctx["conn"], ctx["body"]
    uid = as_int(ctx["params"]["id"], "id")
    require(conn.execute("SELECT 1 FROM units WHERE id = ?", (uid,)).fetchone(), "Not found", 404)
    parent_id = as_int(b.get("parent_id"), "parent")
    parent = None
    if parent_id:
        parent = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ?", (parent_id, uid)))
        require(parent, "Parent not found", 404)
        level = next_level(parent["level"]) if parent["level"] != "space" else None
        require(level, "That can't contain other containers")
    else:
        level = "shelf"
    if b.get("level") == "space":
        require(parent and parent["level"] == "shelf", "Loose spaces go on a shelf")
        span = as_int(b.get("span") or 1, "Width", 1, MAX_SPAN)
        start = _next_pos(conn, uid, parent_id)
        cur = conn.execute("INSERT INTO nodes (unit_id, parent_id, level, position, span, name) VALUES (?,?,?,?,?,?)",
                           (uid, parent_id, "space", start, span, text(b, "name", 80)))
        log(ctx, "node.add", f"Added a loose space ({span} rack-width{'s' if span > 1 else ''})", unit_id=uid)
        conn.commit()
        return {"ids": [cur.lastrowid]}
    count = as_int(b.get("count") or 1, "Count", 1, LAYOUT_MAX[LAYOUT_KEYS[level]])
    layout = clean_layout({**(b.get("layout") or {}), LAYOUT_KEYS[level]: count}, level)
    total = conn.execute("SELECT COUNT(*) FROM nodes WHERE unit_id=? AND level='box'", (uid,)).fetchone()[0]
    per = 1
    for lvl in LEVELS[LEVELS.index(level):]:
        per *= layout[LAYOUT_KEYS[lvl]]
    require(total + per <= MAX_BOXES_PER_UNIT, f"That would exceed {MAX_BOXES_PER_UNIT} boxes in this unit")
    if _free_slots(conn, uid, parent_id):
        # Arranged tank: each new rack fills the next empty slot.
        ids = []
        plan = unit_plan(conn, uid)
        for _ in range(count):
            pos = _next_pos(conn, uid, parent_id)
            ids += build_nodes(conn, uid, parent_id, level, 1, layout, pos)
            # Name it after its slot ("C3") unless another rack on this shelf already has that name.
            slot_name = plan["slots"][pos]["name"] if pos < len(plan["slots"]) else None
            taken = conn.execute("SELECT 1 FROM nodes WHERE parent_id = ? AND name = ?", (parent_id, slot_name)).fetchone()
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?",
                         (slot_name if slot_name and not taken else _free_name(conn, parent_id, "Rack", ids[-1]), ids[-1]))
    else:
        ids = build_nodes(conn, uid, parent_id, level, count, layout, _next_pos(conn, uid, parent_id))
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


def _free_name(conn, shelf_id, base, exclude_id):
    """'Rack 4' / 'Loose space 2' — the first '<base> N' not used by another piece on this shelf."""
    taken = {r[0] for r in conn.execute(
        "SELECT name FROM nodes WHERE parent_id = ? AND id != ? AND name IS NOT NULL", (shelf_id, exclude_id))}
    base = re.sub(r"\s*\d+$", "", base).strip() or "Rack"
    i = 1
    while f"{base} {i}" in taken:
        i += 1
    return f"{base} {i}"


@route("POST", "/api/nodes/<id>/convert")
def convert_node(ctx):
    """Swap an empty rack for a loose space (same spot on the shelf), or back."""
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n and n["level"] in SHELF_CHILDREN, "Only racks and loose spaces can be converted")
    cnt = _stored_under(conn, nid)
    tree = Tree(conn, n["unit_id"])
    old_label = tree.label(tree.nodes[nid])
    if n["level"] == "rack":
        require(cnt == 0, f"This rack still holds {cnt} item(s) — move or remove them first.", 409)
    else:
        require(cnt == 0, f"This space still holds {cnt} loose item(s) — move or remove them first.", 409)
    # Pin the other pieces' names so converting this one doesn't renumber its neighbours.
    _freeze_labels(conn, tree, {n["parent_id"]})
    if n["level"] == "rack":
        conn.execute("DELETE FROM nodes WHERE parent_id = ?", (nid,))   # rows → boxes cascade
        name = n["name"] if n["name"] and not re.fullmatch(r"Rack \d+", n["name"]) else None
        name = name or _free_name(conn, n["parent_id"], "Loose space", nid)
        conn.execute("UPDATE nodes SET level='space', span=1, name=? WHERE id=?", (name, nid))
        log(ctx, "node.convert", f"Replaced {old_label} with a loose space", unit_id=n["unit_id"])
    else:
        layout = clean_layout(b.get("layout") or {}, "row")
        name = n["name"] if n["name"] and not re.fullmatch(r"Loose space \d+", n["name"]) else None
        name = name or _free_name(conn, n["parent_id"], "Rack", nid)
        conn.execute("UPDATE nodes SET level='rack', span=1, name=? WHERE id=?", (name, nid))
        build_nodes(conn, n["unit_id"], nid, "row", layout["rows"], layout)
        log(ctx, "node.convert", f"Replaced {old_label} with a rack", unit_id=n["unit_id"])
    conn.commit()
    return {"ok": True}


@route("POST", "/api/nodes/<id>/place")
def place_node(ctx):
    """Move a rack/loose space onto a shelf, or a box into a row:
    {parent_id, before_id: sibling or null = end}. Items travel with their container."""
    conn, b = ctx["conn"], ctx["body"]
    nid = as_int(ctx["params"]["id"], "id")
    n = one(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)))
    require(n and (n["level"] in SHELF_CHILDREN or n["level"] == "box"), "Only racks, loose spaces and boxes can be moved")
    parent_level = "row" if n["level"] == "box" else "shelf"
    shelf_id = as_int(b.get("parent_id"), parent_level.capitalize(), allow_none=False)
    shelf = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND unit_id = ? AND level = ?",
                             (shelf_id, n["unit_id"], parent_level)))
    require(shelf, "Boxes can only go into a row of the same storage unit" if parent_level == "row"
            else "Racks can only go on a shelf of the same storage unit")
    before = as_int(b.get("before_id"), "before")
    tree = Tree(conn, n["unit_id"])
    _freeze_labels(conn, tree, {n["parent_id"], shelf_id})
    plan = unit_plan(conn, n["unit_id"]) if parent_level == "shelf" else None
    if plan:
        # Arranged tank: racks sit in fixed slots, so a drop swaps two racks or fills an empty slot.
        label = tree.label(tree.nodes[nid])
        swap = as_int(b.get("swap_with"), "swap")
        if swap is not None:
            other = one(conn.execute("SELECT * FROM nodes WHERE id = ? AND parent_id = ?", (swap, shelf_id)))
            require(other and other["id"] != nid, "That rack isn't on this shelf")
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
            taken = conn.execute("SELECT 1 FROM nodes WHERE parent_id = ? AND position = ? AND id != ?",
                                 (shelf_id, slot, nid)).fetchone()
            require(not taken, "That slot is taken — drop onto the rack to swap them")
            conn.execute("UPDATE nodes SET parent_id = ?, position = ? WHERE id = ?", (shelf_id, slot, nid))
            log(ctx, "node.move", f"Moved {label} to slot {plan['slots'][slot]['name'] or slot + 1}", unit_id=n["unit_id"])
        conn.commit()
        return {"ok": True, "renamed": None}
    sibs = [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE parent_id = ? AND id != ? ORDER BY position, id", (shelf_id, nid))]
    if before is not None:
        require(before in sibs, "Drop target isn't on that shelf")
        sibs.insert(sibs.index(before), nid)
    else:
        sibs.append(nid)
    conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (shelf_id, nid))
    for i, sid in enumerate(sibs):
        conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, sid))
    label = tree.label(tree.nodes[nid])
    renamed = None
    if shelf_id != n["parent_id"]:
        dup = conn.execute("SELECT 1 FROM nodes WHERE parent_id = ? AND id != ? AND name = ?",
                           (shelf_id, nid, label)).fetchone()
        if dup:   # e.g. "Rack 1" dropped on a shelf that already has a "Rack 1"
            renamed = _free_name(conn, shelf_id, label, nid)
            conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (renamed, nid))
        log(ctx, "node.move", f"Moved {label} from {tree.label(tree.nodes[n['parent_id']])} to {tree.label(tree.nodes[shelf_id])}",
            unit_id=n["unit_id"])
    else:
        log(ctx, "node.move", f"Reordered {label} on {tree.label(tree.nodes[shelf_id])}", unit_id=n["unit_id"])
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
    # Describe the layout from the first shelf / rack / row / box (presets are uniform).
    layout = {"shelves": len([x for x in kids[None] if x["level"] == "shelf"]), "racks": 0, "rows": 0, "boxes": 0,
              "grid_rows": 9, "grid_cols": 9, "label_style": "grid"}
    node = (kids[None] or [None])[0]
    if node:
        racks = [x for x in kids[node["id"]] if x["level"] == "rack"]
        layout["racks"] = len(racks)
        node = racks[0] if racks else None
    if node:
        layout["rows"] = len(kids[node["id"]])
        node = (kids[node["id"]] or [None])[0]
    if node:
        layout["boxes"] = len(kids[node["id"]])
        node = (kids[node["id"]] or [None])[0]
    if node:
        layout.update(grid_rows=node["grid_rows"], grid_cols=node["grid_cols"], label_style=node["label_style"])
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
        sets["layout"] = json.dumps(clean_layout(b["layout"] or {}, "shelf"))
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
    if not node_id:
        require(pos is None, "Positions only exist inside boxes")
        return None
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
        require(node["level"] in ("shelf", "space"), "Loose items go in a unit, on a shelf or in a loose space")
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
        nice = ", ".join(k.replace("_id", "").replace("_", " ") for k in changed)
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
                "Source", "Lot", "Expiry", "Date added", "Status", "Date removed", "Removal reason", "Notes"])
    for it in out:
        own = "Common" if it["eff_common"] else users.get(it["eff_owner_id"], "")
        w.writerow([it["id"], it["name"], it.get("category_name") or "", own, it["location"]["text"],
                    it["location"]["pos"] or "", it["quantity"] or "", it["concentration"] or "",
                    it["source"] or "", it["lot"] or "", it["expiry_date"] or "", it["date_added"],
                    it["status"], it["date_removed"] or "", it["removal_reason"] or "", it["notes"] or ""])
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
    sql = """SELECT a.*, u.name AS user_name FROM activity a LEFT JOIN users u ON u.id = a.user_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id DESC LIMIT ?"
    args.append(as_int(q.get("limit") or 200, "limit", 1, 2000))
    return rows(ctx["conn"].execute(sql, args))


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
                                               f"Max-Age={SESSION_DAYS * 86400}")
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
