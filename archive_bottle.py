"""
Archive a bottle (consumed / sold).

Two ways in, ONE code path:
  * archive_bottle(bottle_id, status, sale_price)  ← importable core. Delegates to
    db.set_status(). (The Flask app calls db.set_status() directly; this wrapper
    stays for the CLI below and any other importer.)
  * `python archive_bottle.py`  ← interactive CLI: lists the active bottles from
    the DB, asks status + sale price, and writes through db.set_status().

All reads/writes go through db.py (SQLite at data/bourbon.db). Nothing here
touches bottles.csv anymore — that file lives only as data/bottles.csv.bak.
"""

import db

# Archivable target states. The DB CHECK also allows 'active', but from here you
# only ever transition INTO these two. Kept as a module constant for any importer.
VALID_STATUSES = list(db.ARCHIVE_STATUSES)


def archive_bottle(bottle_id, status, sale_price=None):
    """Mark a bottle consumed or sold (delegates to db.set_status).

    status     'consumed' or 'sold'
    sale_price required when status == 'sold'; ignored otherwise

    Stamps date_resolved = today (in db). Returns the updated bottle dict.
    Raises ValueError on bad status, missing/invalid sale price, or unknown id.
    """
    return db.set_status(bottle_id, status, sale_price)


# --------------------------------------------------------------------------- #
# Interactive CLI — collects input and delegates to archive_bottle() / db.
# --------------------------------------------------------------------------- #

def prompt_choice(prompt, options):
    """Display numbered list, return the chosen item."""
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
    active = db.get_active_bottles()
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

    updated = archive_bottle(chosen["bottle_id"], new_status, sale_price)

    print()
    print(f"  Archived {updated['bottle_id']} as {updated['status']}")
    print(f"  Resolved date: {updated['date_resolved']}")
    if updated["sale_price"]:
        print(f"  Sale price:    ${updated['sale_price']}")


if __name__ == "__main__":
    main()
