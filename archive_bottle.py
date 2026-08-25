"""
Archive a bottle (consumed / sold) — owner CLI.

  * archive_bottle(bottle_id, status, user_id, sale_price)  ← importable wrapper,
    delegates to db.set_status() (which enforces that the bottle belongs to user_id).
  * `python archive_bottle.py`  ← interactive CLI: lists the OWNER's active bottles,
    asks status + sale price, and writes through db.set_status().

All reads/writes go through db.py (Postgres). Every operation is scoped to a user
id; the CLI uses the admin/owner account.
"""

import db

VALID_STATUSES = list(db.ARCHIVE_STATUSES)


def archive_bottle(bottle_id, status, user_id, sale_price=None):
    """Mark one of user_id's bottles consumed or sold (delegates to db.set_status).
    Raises ValueError on bad status/sale price, or if the bottle isn't owned by
    user_id."""
    return db.set_status(bottle_id, status, user_id, sale_price)


# --------------------------------------------------------------------------- #
# Interactive CLI
# --------------------------------------------------------------------------- #

def prompt_choice(prompt, options):
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input(prompt).strip()
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("  Enter a number.")
            continue
        if 1 <= idx <= len(options):
            return options[idx - 1]
        print(f"  Choose a number between 1 and {len(options)}.")


def main():
    owner_id = db.get_admin_user_id()
    if owner_id is None:
        print("No account exists yet. Create your account in the app first.")
        return

    active = db.get_active_bottles(owner_id)
    if not active:
        print("No active bottles to archive.")
        return

    print(f"\nActive bottles ({len(active)}):")
    print("-" * 60)
    labels = [f"{b['bottle_id']:50}  {b['name']}" for b in active]
    chosen_label = prompt_choice("\nPick a bottle to archive (number): ", labels)
    chosen = active[labels.index(chosen_label)]

    print(f"\nArchiving: {chosen['name']}")
    print("-" * 60)
    new_status = prompt_choice("Status (number): ", VALID_STATUSES)

    sale_price = None
    if new_status == "sold":
        while True:
            raw = input("Sale price (e.g. 150.00): ").strip()
            if not raw:
                print("  Sale price is required when sold.")
                continue
            try:
                sale_price = float(raw)
                break
            except ValueError:
                print("  Enter a dollar amount.")

    updated = archive_bottle(chosen["bottle_id"], new_status, owner_id, sale_price)

    print()
    print(f"  Archived {updated['bottle_id']} as {updated['status']}")
    print(f"  Resolved date: {updated['date_resolved']}")
    if updated["sale_price"]:
        print(f"  Sale price:    ${updated['sale_price']}")


if __name__ == "__main__":
    main()
