"""
db.py — single access point for the SQLite data layer (data/bourbon.db).

Every other module (flask_app, pipeline, build_dashboard) imports from here
instead of opening the DB or a CSV raw. This is the storage-layer replacement
for the old bottles.csv / price_history.csv pair.

COMPATIBILITY CONTRACT (why reads return strings):
  The migration is "storage only" — templates and pricing math downstream were
  written against CSV rows, where every value is a string and blanks are "".
  So the read helpers here return rows shaped EXACTLY like the old csv.DictReader
  output: all values are strings, SQL NULL becomes "", and REAL money columns are
  formatted "%.2f". Downstream code is therefore unchanged. Storage is typed
  (REAL/INTEGER/NULL); only the presentation back to old consumers is stringified.

ATOMICITY:
  Writes go through a single SQLite transaction (conn.commit()). That replaces the
  old temp-file + os.replace() atomic-write pattern the CSV writers used — SQLite
  gives the same all-or-nothing guarantee at the engine level, so a crash mid-write
  leaves the DB consistent, never half-written.
"""

import sqlite3
from datetime import date

from migrate_to_sqlite import DB_PATH, SCHEMA_SQL

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
    """Open a connection with row access by name and foreign keys enforced."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create the tables if they don't exist (idempotent; loads no data)."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Stringify helpers — reproduce the old CSV dict shape for downstream consumers
# --------------------------------------------------------------------------- #

def _money(v):
    """REAL -> '%.2f' string; NULL -> '' (matches the old CSV cells)."""
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
# Reads (return CSV-shaped dicts; order preserved by insertion rowid/id)
# --------------------------------------------------------------------------- #

def get_all_bottles():
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM bottles ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_active_bottles():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM bottles WHERE status='active' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [_bottle_dict(r) for r in rows]


def get_bottle(bottle_id):
    conn = connect()
    try:
        r = conn.execute(
            "SELECT * FROM bottles WHERE bottle_id=?", (bottle_id,)
        ).fetchone()
    finally:
        conn.close()
    return _bottle_dict(r) if r else None


def get_bottle_ids():
    """Set of all existing bottle_ids — used by intake for slug-collision checks."""
    conn = connect()
    try:
        rows = conn.execute("SELECT bottle_id FROM bottles").fetchall()
    finally:
        conn.close()
    return {r["bottle_id"] for r in rows}


def get_all_history():
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()
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
# Writes (single transaction each = atomic, replacing temp-file+os.replace)
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
        conn.execute(
            "INSERT INTO price_history ("
            "timestamp, bottle_id, wooden_cork_price, bbb_price, barrel_tap_price, "
            "keg_n_bottle_price, market_value, sources_count, msrp, paid, "
            "gain_loss_dollar, gain_loss_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (timestamp, bottle_id, wooden_cork_price, bbb_price, barrel_tap_price,
             keg_n_bottle_price, market_value, sources_count, msrp, paid,
             gain_loss_dollar, gain_loss_pct),
        )
        conn.commit()
    finally:
        conn.close()


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
        sets.append(f"{col}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(bottle_id)
    conn = connect()
    try:
        cur = conn.execute(
            f"UPDATE bottles SET {', '.join(sets)} WHERE bottle_id=?", vals
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no bottle with bottle_id {bottle_id!r}")
    finally:
        conn.close()


def insert_bottle(*, bottle_id, name, product_key=None, proof=None, batch=None,
                  bottle_code=None, paid=None, msrp=None, status="active",
                  sale_price=None, date_acquired=None, date_resolved=None,
                  wooden_cork_url=None, bbb_url=None, barrel_tap_url=None,
                  keg_n_bottle_url=None):
    """Insert a brand-new bottle — the photo-intake write path.

    Money columns (paid, msrp, sale_price) coerce to REAL/NULL; blank text -> NULL.
    A single INSERT is atomic (the transaction replaces the old CSV temp-file +
    os.replace write). Returns the inserted bottle (CSV-shaped). Raises ValueError
    on a missing name, a duplicate bottle_id, a bad status, or a bad money value.
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
        conn.execute(
            f"INSERT INTO bottles ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [row[c] for c in cols],
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        # duplicate PK or CHECK(status) violation
        raise ValueError(f"could not insert bottle {bottle_id!r}: {e}")
    finally:
        conn.close()
    return get_bottle(bottle_id)


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
        cur = conn.execute(
            "UPDATE bottles SET status=?, date_resolved=?, sale_price=? WHERE bottle_id=?",
            (status, date.today().isoformat(), sale, bottle_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no bottle with bottle_id {bottle_id!r}")
    finally:
        conn.close()
    return get_bottle(bottle_id)
