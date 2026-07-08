"""
migrate_sqlite_to_pg.py — copy all data from the SQLite DB (data/bourbon.db) into
Postgres (the DATABASE_URL database), for the Week-1 multi-user foundation.

Storage layer only: same schema, same rows, same values. NULLs are preserved
(blank paid on CYPB, blank sale_price, blank URLs). SQLite already stores these as
typed values / NULLs (from the earlier CSV->SQLite migration), so they pass straight
through to Postgres.

Re-runnable: DROPs and rebuilds both Postgres tables from the current SQLite file
each run. The SQLite file is never modified — it stays as the fallback.

Usage:
  python migrate_sqlite_to_pg.py --schema-only   # create PG schema, verify, stop
  python migrate_sqlite_to_pg.py                 # schema + load + reconcile report
"""

import sqlite3
import sys
from pathlib import Path

import db  # Postgres data layer: owns PG_SCHEMA_SQL and connect()

SQLITE_PATH = Path("data/bourbon.db")

# price_history.id is SERIAL in Postgres — we insert WITHOUT it so the sequence
# assigns 1..N in SQLite id order (nothing references price_history.id; the FK is
# on bottle_id). All other columns copy 1:1.
BOTTLE_COLS = [
    "bottle_id", "product_key", "name", "proof", "batch", "bottle_code",
    "paid", "msrp", "status", "sale_price", "date_acquired", "date_resolved",
    "wooden_cork_url", "bbb_url", "barrel_tap_url", "keg_n_bottle_url",
]
HISTORY_COLS = [
    "timestamp", "bottle_id",
    "wooden_cork_price", "bbb_price", "barrel_tap_price", "keg_n_bottle_price",
    "market_value", "sources_count", "msrp", "paid",
    "gain_loss_dollar", "gain_loss_pct",
]


def read_sqlite():
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"{SQLITE_PATH} not found")
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        bottles = conn.execute("SELECT * FROM bottles ORDER BY rowid").fetchall()
        history = conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()
    finally:
        conn.close()
    return bottles, history


def create_schema(pg):
    with pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS price_history")
        cur.execute("DROP TABLE IF EXISTS bottles")
        cur.execute(db.PG_SCHEMA_SQL)
    pg.commit()


def _quote_cols(cols):
    # "timestamp" is a keyword — quote it; everything else is a plain identifier.
    return ", ".join(f'"{c}"' if c == "timestamp" else c for c in cols)


def load(pg, bottles, history):
    # `bottles` arrives in SQLite rowid order (original acquisition order). Seed the
    # explicit seq surrogate as a dense 1..N in that order, then advance the SERIAL
    # sequence so new intake rows (which omit seq) continue after the last one.
    bottle_cols = ["seq"] + BOTTLE_COLS
    with pg.cursor() as cur:
        cur.executemany(
            f"INSERT INTO bottles ({_quote_cols(bottle_cols)}) "
            f"VALUES ({', '.join(['%s'] * len(bottle_cols))})",
            [(i + 1,) + tuple(r[c] for c in BOTTLE_COLS)
             for i, r in enumerate(bottles)],
        )
        cur.executemany(
            f"INSERT INTO price_history ({_quote_cols(HISTORY_COLS)}) "
            f"VALUES ({', '.join(['%s'] * len(HISTORY_COLS))})",
            [tuple(r[c] for c in HISTORY_COLS) for r in history],
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('bottles','seq'), "
            "(SELECT MAX(seq) FROM bottles))"
        )
    pg.commit()


def report(pg, sqlite_bottles, sqlite_history):
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM bottles")
        pg_bottles = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM price_history")
        pg_history = cur.fetchone()["n"]

        print("Migration complete -> Postgres (bourbon_hunter)\n")
        print(f"  bottles:        SQLite {len(sqlite_bottles):>4}  ->  PG {pg_bottles:>4}  "
              f"{'OK' if pg_bottles == len(sqlite_bottles) else '*** MISMATCH ***'}")
        print(f"  price_history:  SQLite {len(sqlite_history):>4}  ->  PG {pg_history:>4}  "
              f"{'OK' if pg_history == len(sqlite_history) else '*** MISMATCH ***'}")

        cur.execute("SELECT status, COUNT(*) AS n FROM bottles GROUP BY status ORDER BY status")
        breakdown = {r["status"]: r["n"] for r in cur.fetchall()}
        print(f"\n  status breakdown: {breakdown}")

        cur.execute(
            "SELECT status, date_resolved FROM bottles "
            "WHERE bottle_id='colonel_eh_taylor_single_barrel_bottled_in_bond'"
        )
        eht = cur.fetchone()
        if eht:
            print(f"  consumed EHT check: status={eht['status']!r}, date_resolved={eht['date_resolved']!r}")
        else:
            print("  *** consumed EHT row NOT FOUND ***")

        cur.execute(
            "SELECT paid FROM bottles WHERE bottle_id='weller_cypb_craft_your_perfect_bourbon'"
        )
        cypb = cur.fetchone()
        if cypb:
            print(f"  CYPB blank-paid check: paid={cypb['paid']!r} "
                  f"({'NULL OK' if cypb['paid'] is None else '*** expected NULL ***'})")

        # FK integrity: any history row whose bottle_id has no parent bottle?
        cur.execute(
            "SELECT COUNT(*) AS n FROM price_history ph "
            "LEFT JOIN bottles b ON ph.bottle_id = b.bottle_id "
            "WHERE b.bottle_id IS NULL"
        )
        orphans = cur.fetchone()["n"]
        print(f"\n  foreign-key orphans (history without a parent bottle): {orphans} "
              f"{'OK' if orphans == 0 else '*** VIOLATION ***'}")


def print_schema(pg):
    with pg.cursor() as cur:
        for tbl in ("bottles", "price_history"):
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name=%s ORDER BY ordinal_position", (tbl,))
            cols = cur.fetchall()
            print(f"\n  {tbl} ({len(cols)} columns):")
            for c in cols:
                print(f"    {c['column_name']:<20} {c['data_type']}")
        cur.execute(
            "SELECT conname, contype FROM pg_constraint "
            "WHERE conrelid IN ('bottles'::regclass, 'price_history'::regclass) "
            "ORDER BY conname")
        print("\n  constraints (p=PK, f=FK, c=CHECK):")
        for c in cur.fetchall():
            print(f"    {c['contype']}  {c['conname']}")


def main():
    schema_only = "--schema-only" in sys.argv
    pg = db.connect()
    try:
        create_schema(pg)
        if schema_only:
            print("Schema created in Postgres (bottles + price_history).")
            print_schema(pg)
            return
        bottles, history = read_sqlite()
        load(pg, bottles, history)
        report(pg, bottles, history)
    finally:
        pg.close()


if __name__ == "__main__":
    main()
