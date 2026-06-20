"""
migrate_to_sqlite.py — one-time (re-runnable) migration of the CSV data layer
into a single SQLite database at data/bourbon.db.

WHAT IT DOES
  * Creates data/bourbon.db with two tables: bottles, price_history (schema below).
  * Loads every row from data/bottles.csv and data/price_history.csv as-is.
  * Blank/empty CSV cells become SQL NULL (blank paid, sale_price, URLs, etc.).
  * REAL/INTEGER columns are coerced from their CSV string form; TEXT kept as-is.
  * price_history.csv's `name` column is intentionally dropped — the target
    schema doesn't carry it (name lives on the bottles row, joined by bottle_id).

RE-RUNNABLE: the CSVs remain the source of truth during the trial period, so this
script DROPs and rebuilds both tables from the current CSVs every time it runs.
Once the DB is proven over a few real runs, the CSVs get renamed to .bak and this
becomes a historical artifact.

No model changes, no pricing changes — storage layer only.
"""

import csv
import sqlite3
from pathlib import Path

DB_PATH     = Path("data/bourbon.db")
BOTTLES_CSV = Path("data/bottles.csv")
HISTORY_CSV = Path("data/price_history.csv")

# CREATE-only DDL (IF NOT EXISTS) so db.py can safely reuse it to self-init.
# The migration DROPs first (below) to rebuild cleanly from the CSVs.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bottles (
    bottle_id        TEXT PRIMARY KEY,
    product_key      TEXT,
    name             TEXT,
    proof            TEXT,
    batch            TEXT,
    bottle_code      TEXT,
    paid             REAL,
    msrp             REAL,
    status           TEXT CHECK(status IN ('active','consumed','sold')),
    sale_price       REAL,
    date_acquired    TEXT,
    date_resolved    TEXT,
    wooden_cork_url  TEXT,
    bbb_url          TEXT,
    barrel_tap_url   TEXT,
    keg_n_bottle_url TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          TEXT,
    bottle_id          TEXT,
    wooden_cork_price  REAL,
    bbb_price          REAL,
    barrel_tap_price   REAL,
    keg_n_bottle_price REAL,
    market_value       REAL,
    sources_count      INTEGER,
    msrp               REAL,
    paid               REAL,
    gain_loss_dollar   REAL,
    gain_loss_pct      REAL,
    FOREIGN KEY(bottle_id) REFERENCES bottles(bottle_id)
);
"""

# Column order for inserts (must match the CREATE TABLE order, minus auto id).
BOTTLE_COLS = [
    "bottle_id", "product_key", "name", "proof", "batch", "bottle_code",
    "paid", "msrp", "status", "sale_price", "date_acquired", "date_resolved",
    "wooden_cork_url", "bbb_url", "barrel_tap_url", "keg_n_bottle_url",
]
BOTTLE_REAL = {"paid", "msrp", "sale_price"}

HISTORY_COLS = [
    "timestamp", "bottle_id",
    "wooden_cork_price", "bbb_price", "barrel_tap_price", "keg_n_bottle_price",
    "market_value", "sources_count", "msrp", "paid",
    "gain_loss_dollar", "gain_loss_pct",
]
HISTORY_REAL = {
    "wooden_cork_price", "bbb_price", "barrel_tap_price", "keg_n_bottle_price",
    "market_value", "msrp", "paid", "gain_loss_dollar", "gain_loss_pct",
}
HISTORY_INT = {"sources_count"}


def _num(v):
    """CSV string -> float, or None for blank/missing."""
    if v is None:
        return None
    v = v.strip()
    return float(v) if v != "" else None


def _int(v):
    if v is None:
        return None
    v = v.strip()
    return int(float(v)) if v != "" else None


def _text(v):
    """Keep text as-is, but blank/whitespace-only -> None (NULL)."""
    if v is None or v.strip() == "":
        return None
    return v


def _read_csv(path):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _coerce(row, cols, real_cols, int_cols=frozenset()):
    out = []
    for c in cols:
        v = row.get(c)
        if c in real_cols:
            out.append(_num(v))
        elif c in int_cols:
            out.append(_int(v))
        else:
            out.append(_text(v))
    return tuple(out)


def migrate():
    bottle_rows  = _read_csv(BOTTLES_CSV)
    history_rows = _read_csv(HISTORY_CSV)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Rebuild from scratch (child table first for the FK).
        conn.execute("DROP TABLE IF EXISTS price_history")
        conn.execute("DROP TABLE IF EXISTS bottles")
        conn.executescript(SCHEMA_SQL)

        conn.executemany(
            f"INSERT INTO bottles ({','.join(BOTTLE_COLS)}) "
            f"VALUES ({','.join('?' * len(BOTTLE_COLS))})",
            [_coerce(r, BOTTLE_COLS, BOTTLE_REAL) for r in bottle_rows],
        )
        conn.executemany(
            f"INSERT INTO price_history ({','.join(HISTORY_COLS)}) "
            f"VALUES ({','.join('?' * len(HISTORY_COLS))})",
            [_coerce(r, HISTORY_COLS, HISTORY_REAL, HISTORY_INT) for r in history_rows],
        )
        conn.commit()

        # ---- Verification ----
        db_bottles = conn.execute("SELECT COUNT(*) FROM bottles").fetchone()[0]
        db_history = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]

        print("Migration complete -> data/bourbon.db\n")
        print(f"  bottles:        CSV {len(bottle_rows):>4}  ->  DB {db_bottles:>4}  "
              f"{'OK' if db_bottles == len(bottle_rows) else '*** MISMATCH ***'}")
        print(f"  price_history:  CSV {len(history_rows):>4}  ->  DB {db_history:>4}  "
              f"{'OK' if db_history == len(history_rows) else '*** MISMATCH ***'}")

        active   = conn.execute("SELECT COUNT(*) FROM bottles WHERE status='active'").fetchone()[0]
        consumed = conn.execute("SELECT COUNT(*) FROM bottles WHERE status='consumed'").fetchone()[0]
        sold     = conn.execute("SELECT COUNT(*) FROM bottles WHERE status='sold'").fetchone()[0]
        print(f"\n  status breakdown: active={active}  consumed={consumed}  sold={sold}")

        eht = conn.execute(
            "SELECT bottle_id, status, date_resolved FROM bottles "
            "WHERE bottle_id='colonel_eh_taylor_single_barrel_bottled_in_bond'"
        ).fetchone()
        if eht:
            print(f"  consumed EHT check: {eht[0]} -> status={eht[1]!r}, date_resolved={eht[2]!r}")
        else:
            print("  *** consumed EHT row NOT FOUND ***")

        # CYPB blank-paid -> NULL check
        cypb = conn.execute(
            "SELECT bottle_id, paid FROM bottles "
            "WHERE bottle_id='weller_cypb_craft_your_perfect_bourbon'"
        ).fetchone()
        if cypb:
            print(f"  CYPB blank-paid check: paid={cypb[1]!r} "
                  f"({'NULL OK' if cypb[1] is None else '*** expected NULL ***'})")

        # Foreign-key integrity (orphan history rows)
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"\n  foreign_key_check: {len(orphans)} violation(s) "
              f"{'OK' if not orphans else orphans}")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
