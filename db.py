"""
db.py — single access point for the data layer.

BACKEND: PostgreSQL (Week 1 multi-user foundation). The connection string comes
ONLY from the DATABASE_URL environment variable (loaded from .env) — it contains
a password and must never be hardcoded or committed. See .env.example.

The previous SQLite file (data/bourbon.db) is kept as a fallback until Postgres
is proven over several runs; migrate_sqlite_to_pg.py loads it into Postgres.

COMPATIBILITY CONTRACT (why reads return strings):
  Templates and pricing math downstream were written against CSV rows, where every
  value is a string and blanks are "". So the read helpers here return rows shaped
  EXACTLY like the old csv.DictReader output: all values are strings, SQL NULL
  becomes "", and money columns are formatted "%.2f". Downstream code is unchanged.
  Storage is typed (DOUBLE PRECISION / INTEGER / NULL); only the presentation back
  to old consumers is stringified.

ATOMICITY:
  Each write is a single Postgres transaction (commit()/implicit rollback on error)
  — the engine guarantees all-or-nothing, replacing the old temp-file + os.replace
  pattern the CSV layer used.

ORDERING NOTE:
  SQLite had an implicit rowid we ordered bottles by (original acquisition order).
  Postgres has no rowid, so bottles carry an explicit `seq SERIAL` surrogate that
  preserves that order: the migration seeds it from SQLite's rowid order, and new
  intake rows auto-increment onto the end. Reads ORDER BY seq. `seq` is internal
  ordering only — it is not exposed in the CSV-shaped dicts returned to consumers.
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

# Postgres schema — matches the SQLite schema exactly: same columns, TEXT for text,
# DOUBLE PRECISION for the money/float columns (bit-identical to SQLite's REAL, so
# values don't drift), INTEGER for the count, SERIAL for the auto-increment id, the
# status CHECK constraint, and the price_history.bottle_id -> bottles FK.
PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bottles (
    bottle_id        TEXT PRIMARY KEY,
    seq              SERIAL,
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

# Targets the Flask archive buttons may set. The DB CHECK also allows 'active',
# but you only ever transition INTO these two from the UI.
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
    """Open a Postgres connection whose cursors yield dict-like rows.

    Foreign keys are enforced natively by Postgres (no PRAGMA needed).
    """
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
# Stringify helpers — reproduce the old CSV dict shape for downstream consumers
# --------------------------------------------------------------------------- #

def _money(v):
    """number -> '%.2f' string; NULL -> '' (matches the old CSV cells)."""
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
    """Form/string value -> float, or None for blank. Raises ValueError on junk."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    return float(s)


def _blank_to_none(v):
    """Text value -> itself, or None for blank/whitespace-only (stores NULL)."""
    return v if (v is not None and str(v).strip() != "") else None


# --------------------------------------------------------------------------- #
# Reads (return CSV-shaped dicts)
# --------------------------------------------------------------------------- #

def get_all_bottles():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bottles ORDER BY seq")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_active_bottles():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bottles WHERE status='active' ORDER BY seq")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_bottle(bottle_id):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bottles WHERE bottle_id=%s", (bottle_id,))
            r = cur.fetchone()
    finally:
        conn.close()
    return _bottle_dict(r) if r else None


def get_bottle_ids():
    """Set of all existing bottle_ids — used by intake for slug-collision checks."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bottle_id FROM bottles")
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r["bottle_id"] for r in rows}


def get_all_history():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM price_history ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_history_dict(r) for r in rows]


def latest_market_values():
    """{bottle_id: latest price_history row (CSV-shaped)} by newest timestamp."""
    latest = {}
    for row in get_all_history():
        bid = row["bottle_id"]
        if not bid:
            continue
        if bid not in latest or row["timestamp"] > latest[bid]["timestamp"]:
            latest[bid] = row
    return latest


# --------------------------------------------------------------------------- #
# Writes (single transaction each = atomic)
# --------------------------------------------------------------------------- #

def append_price_history(*, timestamp, bottle_id,
                         wooden_cork_price=None, bbb_price=None,
                         barrel_tap_price=None, keg_n_bottle_price=None,
                         market_value=None, sources_count=None,
                         msrp=None, paid=None,
                         gain_loss_dollar=None, gain_loss_pct=None):
    """Insert one price snapshot. Values are native types (float/int/None)."""
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


def insert_bottle(*, bottle_id, name, product_key=None, proof=None, batch=None,
                  bottle_code=None, paid=None, msrp=None, status="active",
                  sale_price=None, date_acquired=None, date_resolved=None,
                  wooden_cork_url=None, bbb_url=None, barrel_tap_url=None,
                  keg_n_bottle_url=None):
    """Insert a brand-new bottle — the photo-intake write path.

    Money columns (paid, msrp, sale_price) coerce to float/NULL; blank text -> NULL.
    Single INSERT (atomic). Returns the inserted bottle (CSV-shaped). Raises
    ValueError on a missing name, a duplicate bottle_id, a bad status, or a bad
    money value.
    """
    if not bottle_id:
        raise ValueError("bottle_id is required")
    if not name or str(name).strip() == "":
        raise ValueError("name is required")

    row = {
        "bottle_id":        bottle_id,
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
        # duplicate PK or CHECK(status) violation
        raise ValueError(f"could not insert bottle {bottle_id!r}: {e}")
    finally:
        conn.close()
    return get_bottle(bottle_id)


def update_bottle_fields(bottle_id, fields):
    """Update arbitrary editable columns for one bottle (the Flask Edit pane).

    `fields` maps column -> raw value (form string ok). Money columns are coerced
    to float/None; text/url columns store None for blank. Raises ValueError on an
    unknown column, a bad money value, or an unknown bottle_id.
    """
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
    vals.append(bottle_id)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bottles SET {', '.join(sets)} WHERE bottle_id=%s", vals
            )
            affected = cur.rowcount
        conn.commit()
        if affected == 0:
            raise ValueError(f"no bottle with bottle_id {bottle_id!r}")
    finally:
        conn.close()


def set_status(bottle_id, status, sale_price=None):
    """Archive a bottle as 'consumed' or 'sold' (mirrors the old archive_bottle()).

    Stamps date_resolved = today. sale_price required (and stored) only for 'sold';
    a consumed bottle gets sale_price = NULL. Returns the updated bottle (CSV-shaped).
    """
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
                "WHERE bottle_id=%s",
                (status, date.today().isoformat(), sale, bottle_id),
            )
            affected = cur.rowcount
        conn.commit()
        if affected == 0:
            raise ValueError(f"no bottle with bottle_id {bottle_id!r}")
    finally:
        conn.close()
    return get_bottle(bottle_id)
