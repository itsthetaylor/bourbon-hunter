"""
Manually archive a bottle (sold / drank / given_away / lost) from the terminal.
Phase 1 CLI — replaced by web UI in Phase 2.
"""

import csv
from datetime import date
from pathlib import Path

BOTTLES_CSV = Path("data/bottles.csv")

FIELDNAMES = ["bottle_id", "name", "proof", "msrp", "acquisition_date",
              "acquisition_price", "batch", "bottle_code",
              "status", "removed_date", "sale_price", "removal_notes",
              "wooden_cork_url", "bbb_url",
              "barrel_tap_url", "keg_n_bottle_url"]

VALID_STATUSES = ["sold", "drank", "given_away", "lost"]


def load_bottles():
    if not BOTTLES_CSV.exists():
        return []
    with open(BOTTLES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def save_bottles(bottles):
    with open(BOTTLES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(bottles)


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
    bottles = load_bottles()
    if not bottles:
        print(f"No bottles found in {BOTTLES_CSV}")
        return

    active = [b for b in bottles if (b.get("status") or "active") == "active"]
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

    sale_price = ""
    if new_status == "sold":
        while True:
            raw = input("Sale price (e.g. 150.00): ").strip()
            if not raw:
                print("  Sale price is required when sold.")
                continue
            try:
                sale_price = f"{float(raw):.2f}"
                break
            except ValueError:
                print("  Enter a dollar amount.")

    notes = input("Removal notes (optional, enter to skip): ").strip()

    chosen["status"] = new_status
    chosen["removed_date"] = date.today().isoformat()
    chosen["sale_price"] = sale_price
    chosen["removal_notes"] = notes

    save_bottles(bottles)

    print()
    print(f"  Archived {chosen['bottle_id']} as {new_status}")
    print(f"  Removed date: {chosen['removed_date']}")
    if sale_price:
        print(f"  Sale price:   ${sale_price}")
    if notes:
        print(f"  Notes:        {notes}")


if __name__ == "__main__":
    main()
