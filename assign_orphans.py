"""
assign_orphans.py — one-time: assign every unowned bottle (user_id IS NULL) to a
given account. Use it once after signing up to import your pre-accounts collection.

Runs against whatever DATABASE_URL points to (local by default; set DATABASE_URL to
the Railway public URL to do the hosted DB).

    python assign_orphans.py <your-email>
"""

import sys

import db


def main():
    if len(sys.argv) != 2:
        print("usage: python assign_orphans.py <email>")
        sys.exit(1)
    email = sys.argv[1].strip().lower()
    user = db.get_user_by_email(email)
    if not user:
        print(f"No account found for {email!r}. Sign up in the app first, then re-run.")
        sys.exit(1)
    n = db.assign_orphans_to(user["id"])
    print(f"Assigned {n} unowned bottle(s) to {email} (user id {user['id']}).")
    if n == 0:
        print("  (Nothing was unowned — everything already has an owner.)")


if __name__ == "__main__":
    main()
