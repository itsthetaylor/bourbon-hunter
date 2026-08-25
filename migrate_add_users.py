"""
migrate_add_users.py — idempotent migration that adds multi-user support to an
EXISTING database: creates the `users` table and adds `bottles.user_id`.

Run it against local (default DATABASE_URL) AND against Railway (set DATABASE_URL
to the Railway public URL for that run) so the hosted DB gets the column too.

Safe to run repeatedly (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
It does NOT assign owners — after you sign up, run assign_orphans.py <email>.
"""

import db

DDL = [
    """CREATE TABLE IF NOT EXISTS users (
        id            SERIAL PRIMARY KEY,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE bottles ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
]


def main():
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            users = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM bottles")
            bottles = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM bottles WHERE user_id IS NULL")
            orphans = cur.fetchone()["n"]
    finally:
        conn.close()

    print("Migration applied (idempotent).")
    print(f"  users table rows: {users}")
    print(f"  bottles: {bottles}  (unowned / user_id NULL: {orphans})")
    print("  Next: sign up your account in the app, then run:")
    print("        python assign_orphans.py <your-email>")


if __name__ == "__main__":
    main()
