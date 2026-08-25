"""
db.py — single access point for the data layer (PostgreSQL).

BACKEND: PostgreSQL. The connection string comes ONLY from the DATABASE_URL
environment variable (loaded from .env) — it holds a password and must never be
hardcoded or committed. See .env.example.

MULTI-USER ISOLATION (critical):
  Every bottle belongs to exactly one user (bottles.user_id -> users.id). All the
  per-user bottle accessors below take a REQUIRED user_id and filter on it — reads
  with `WHERE user_id=%s`, writes with `WHERE bottle_id=%s AND user_id=%s` so a
  forged bottle_id from another user simply matches zero rows. A user can never
  read or modify another user's bottles through these functions.

  The ONLY unscoped bottle accessors are batch/system helpers, named explicitly:
    * get_all_active_for_pricing()  — the pricing pipeline prices everyone's
      bottles (market value is owner-agnostic).
    * get_bottle_ids()             — global PK-collision check for intake slugs.
  These never return data to a user-facing request path.

COMPATIBILITY CONTRACT (why bottle reads return strings):
  Templates/pricing math were written against CSV rows (all values strings, blanks
  ""). The bottle/history read helpers return that same shape. User rows are NOT
  stringified — they are typed dicts consumed by auth.py.

ATOMICITY:
  Each write is a single Postgres transaction (commit / implicit rollback on error).
"""

import os
from datetime import date

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env and set the Postgres "
        "connection string (it holds the DB password; never hardcode it)."
    )

# Schema. `users` is created before `bottles` because bottles.user_id references it.
PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bottles (
    bottle_id        TEXT PRIMARY KEY,
    seq              SERIAL,
    user_id          INTEGER REFERENCES users(id),
    product_key      TEXT,
    name             TEXT,
    proof            TEXT,
    batch            TEXT,
    bottle_code      TEXT,
    paid             DOUBLE PRECISION,
    msrp             DOUBLE PRECISION,
    status           TEXT CHECK (status IN ('active','consumed','sold')),
    sale_price       DOUBLE PRECISION,
    date_acquired    TEXT,
    date_resolved    TEXT,
    wooden_cork_url  TEXT,
    bbb_url          TEXT,
    barrel_tap_url   TEXT,
    keg_n_bottle_url TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id                 SERIAL PRIMARY KEY,
    "timestamp"        TEXT,
    bottle_id          TEXT REFERENCES bottles(bottle_id),
    wooden_cork_price  DOUBLE PRECISION,
    bbb_price          DOUBLE PRECISION,
    barrel_tap_price   DOUBLE PRECISION,
    keg_n_bottle_price DOUBLE PRECISION,
    market_value       DOUBLE PRECISION,
    sources_count      INTEGER,
    msrp               DOUBLE PRECISION,
    paid               DOUBLE PRECISION,
    gain_loss_dollar   DOUBLE PRECISION,
    gain_loss_pct      DOUBLE PRECISION
);
"""

ARCHIVE_STATUSES = ("consumed", "sold")

_MONEY_COLS = {"paid", "msrp", "sale_price"}
_UPDATABLE = _MONEY_COLS | {
    "wooden_cork_url", "bbb_url", "barrel_tap_url", "keg_n_bottle_url",
    "product_key", "name", "proof", "batch", "bottle_code", "date_acquired",
}


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

def connect():
    """Open a Postgres connection whose cursors yield dict-like rows."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    """Create the tables if they don't exist (idempotent; loads no data)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(PG_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Stringify helpers — reproduce the old CSV dict shape for bottle/history consumers
# --------------------------------------------------------------------------- #

def _money(v):
    return f"{v:.2f}" if v is not None else ""


def _str(v):
    return "" if v is None else str(v)


def _bottle_dict(row):
    return {
        "bottle_id":        _str(row["bottle_id"]),
        "product_key":      _str(row["product_key"]),
        "name":             _str(row["name"]),
        "proof":            _str(row["proof"]),
        "batch":            _str(row["batch"]),
        "bottle_code":      _str(row["bottle_code"]),
        "paid":             _money(row["paid"]),
        "msrp":             _money(row["msrp"]),
        "status":           _str(row["status"]),
        "sale_price":       _money(row["sale_price"]),
        "date_acquired":    _str(row["date_acquired"]),
        "date_resolved":    _str(row["date_resolved"]),
        "wooden_cork_url":  _str(row["wooden_cork_url"]),
        "bbb_url":          _str(row["bbb_url"]),
        "barrel_tap_url":   _str(row["barrel_tap_url"]),
        "keg_n_bottle_url": _str(row["keg_n_bottle_url"]),
    }


def _history_dict(row):
    return {
        "timestamp":          _str(row["timestamp"]),
        "bottle_id":          _str(row["bottle_id"]),
        "wooden_cork_price":  _money(row["wooden_cork_price"]),
        "bbb_price":          _money(row["bbb_price"]),
        "barrel_tap_price":   _money(row["barrel_tap_price"]),
        "keg_n_bottle_price": _money(row["keg_n_bottle_price"]),
        "market_value":       _money(row["market_value"]),
        "sources_count":      _str(row["sources_count"]),
        "msrp":               _money(row["msrp"]),
        "paid":               _money(row["paid"]),
        "gain_loss_dollar":   _money(row["gain_loss_dollar"]),
        "gain_loss_pct":      _money(row["gain_loss_pct"]),
    }


def _to_money(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    return float(s)


def _blank_to_none(v):
    return v if (v is not None and str(v).strip() != "") else None


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def create_user(email, password_hash, is_admin=False):
    """Insert a user. `password_hash` must already be a bcrypt hash (never a
    plaintext password). Returns the new user id. Raises ValueError on a duplicate
    email."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    if not password_hash:
        raise ValueError("password_hash is required")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash, is_admin) "
                "VALUES (%s, %s, %s) RETURNING id",
                (email, password_hash, is_admin),
            )
            new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id
    except psycopg2.IntegrityError:
        raise ValueError(f"an account already exists for {email!r}")
    finally:
        conn.close()


def get_user_by_email(email):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash, is_admin FROM users WHERE email=%s",
                ((email or "").strip().lower(),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash, is_admin FROM users WHERE id=%s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def count_users():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            return cur.fetchone()["n"]
    finally:
        conn.close()


def get_admin_user_id():
    """First admin's id (owner), or None. Used by batch intake/CLI to assign owner."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE is_admin ORDER BY id LIMIT 1")
            r = cur.fetchone()
            return r["id"] if r else None
    finally:
        conn.close()


def assign_orphans_to(user_id):
    """Assign every bottle with no owner (user_id IS NULL) to user_id. One-time,
    for importing the pre-accounts collection. Returns the number assigned."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bottles SET user_id=%s WHERE user_id IS NULL", (user_id,))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Bottle reads — PER-USER (required user_id)
# --------------------------------------------------------------------------- #

def get_all_bottles(user_id):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bottles WHERE user_id=%s ORDER BY seq", (user_id,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_active_bottles(user_id):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM bottles WHERE user_id=%s AND status='active' ORDER BY seq",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_bottle(bottle_id, user_id):
    """One bottle, ONLY if it belongs to user_id (else None)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM bottles WHERE bottle_id=%s AND user_id=%s",
                (bottle_id, user_id),
            )
            r = cur.fetchone()
    finally:
        conn.close()
    return _bottle_dict(r) if r else None


def latest_market_values(user_id):
    """{bottle_id: latest price_history row (CSV-shaped)} for THIS user's bottles."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ph.* FROM price_history ph "
                "JOIN bottles b ON b.bottle_id = ph.bottle_id "
                "WHERE b.user_id = %s ORDER BY ph.id",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    latest = {}
    for row in rows:
        d = _history_dict(row)
        bid = d["bottle_id"]
        if not bid:
            continue
        if bid not in latest or d["timestamp"] > latest[bid]["timestamp"]:
            latest[bid] = d
    return latest


# --------------------------------------------------------------------------- #
# Bottle reads — UNSCOPED batch/system helpers (never a user request path)
# --------------------------------------------------------------------------- #

def get_all_active_for_pricing():
    """ALL users' active bottles — for the pricing pipeline only (market value is
    owner-agnostic). Never use this to answer a logged-in user's request."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bottles WHERE status='active' ORDER BY seq")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_bottle_ids():
    """Set of ALL bottle_ids (global) — intake slug-collision check only. bottle_id
    is the global PK, so uniqueness must be checked across every user."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bottle_id FROM bottles")
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r["bottle_id"] for r in rows}


# --------------------------------------------------------------------------- #
# Bottle writes — PER-USER (required user_id)
# --------------------------------------------------------------------------- #

def append_price_history(*, timestamp, bottle_id,
                         wooden_cork_price=None, bbb_price=None,
                         barrel_tap_price=None, keg_n_bottle_price=None,
                         market_value=None, sources_count=None,
                         msrp=None, paid=None,
                         gain_loss_dollar=None, gain_loss_pct=None):
    """Insert one price snapshot (pipeline; keyed to a bottle, owner-agnostic)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO price_history ('
                '"timestamp", bottle_id, wooden_cork_price, bbb_price, barrel_tap_price, '
                'keg_n_bottle_price, market_value, sources_count, msrp, paid, '
                'gain_loss_dollar, gain_loss_pct) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (timestamp, bottle_id, wooden_cork_price, bbb_price, barrel_tap_price,
                 keg_n_bottle_price, market_value, sources_count, msrp, paid,
                 gain_loss_dollar, gain_loss_pct),
            )
        conn.commit()
    finally:
        conn.close()


def insert_bottle(*, bottle_id, name, user_id, product_key=None, proof=None,
                  batch=None, bottle_code=None, paid=None, msrp=None,
                  status="active", sale_price=None, date_acquired=None,
                  date_resolved=None, wooden_cork_url=None, bbb_url=None,
                  barrel_tap_url=None, keg_n_bottle_url=None):
    """Insert a brand-new bottle owned by user_id (the photo-intake write path)."""
    if not bottle_id:
        raise ValueError("bottle_id is required")
    if not name or str(name).strip() == "":
        raise ValueError("name is required")
    if not user_id:
        raise ValueError("user_id is required — every bottle must have an owner")

    row = {
        "bottle_id":        bottle_id,
        "user_id":          user_id,
        "product_key":      _blank_to_none(product_key),
        "name":             name,
        "proof":            _blank_to_none(proof),
        "batch":            _blank_to_none(batch),
        "bottle_code":      _blank_to_none(bottle_code),
        "paid":             _to_money(paid),
        "msrp":             _to_money(msrp),
        "status":           status,
        "sale_price":       _to_money(sale_price),
        "date_acquired":    _blank_to_none(date_acquired),
        "date_resolved":    _blank_to_none(date_resolved),
        "wooden_cork_url":  _blank_to_none(wooden_cork_url),
        "bbb_url":          _blank_to_none(bbb_url),
        "barrel_tap_url":   _blank_to_none(barrel_tap_url),
        "keg_n_bottle_url": _blank_to_none(keg_n_bottle_url),
    }
    cols = list(row.keys())
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO bottles ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                [row[c] for c in cols],
            )
        conn.commit()
    except psycopg2.IntegrityError as e:
        raise ValueError(f"could not insert bottle {bottle_id!r}: {e}")
    finally:
        conn.close()
    return get_bottle(bottle_id, user_id)


def update_bottle_fields(bottle_id, fields, user_id):
    """Update editable columns for one bottle, ONLY if it belongs to user_id.
    Raises ValueError on an unknown column, a bad money value, or if the bottle is
    not owned by user_id (0 rows matched)."""
    sets, vals = [], []
    for col, v in fields.items():
        if col not in _UPDATABLE:
            raise ValueError(f"field {col!r} is not updatable")
        if col in _MONEY_COLS:
            v = _to_money(v)
        else:
            v = v if (v is not None and str(v).strip() != "") else None
        sets.append(f"{col}=%s")
        vals.append(v)
    if not sets:
        return
    vals.extend([bottle_id, user_id])
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bottles SET {', '.join(sets)} "
                f"WHERE bottle_id=%s AND user_id=%s", vals
            )
            affected = cur.rowcount
        conn.commit()
        if affected == 0:
            raise ValueError(f"no bottle {bottle_id!r} owned by this user")
    finally:
        conn.close()


def set_status(bottle_id, status, user_id, sale_price=None):
    """Archive a bottle as 'consumed'/'sold', ONLY if it belongs to user_id.
    Stamps date_resolved = today. Returns the updated bottle, or raises ValueError
    if the bottle is not owned by user_id."""
    status = (status or "").strip()
    if status not in ARCHIVE_STATUSES:
        raise ValueError(f"status must be one of {ARCHIVE_STATUSES}, got {status!r}")

    sale = None
    if status == "sold":
        if sale_price is None or str(sale_price).strip() == "":
            raise ValueError("sale_price is required when status is 'sold'")
        try:
            sale = _to_money(sale_price)
        except ValueError:
            raise ValueError(f"sale_price must be a number, got {sale_price!r}")

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bottles SET status=%s, date_resolved=%s, sale_price=%s "
                "WHERE bottle_id=%s AND user_id=%s",
                (status, date.today().isoformat(), sale, bottle_id, user_id),
            )
            affected = cur.rowcount
        conn.commit()
        if affected == 0:
            raise ValueError(f"no bottle {bottle_id!r} owned by this user")
    finally:
        conn.close()
    return get_bottle(bottle_id, user_id)
