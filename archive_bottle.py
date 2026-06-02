"""
Archive a bottle (sold / drank / given_away / lost).

Two ways in, ONE code path:
  * archive_bottle(bottle_id, status, sale_price, notes)  ← importable core.
    The Flask app (Phase 2) calls this directly — no shelling out to the
    interactive prompts below (that would hang waiting on stdin).
  * `python archive_bottle.py`  ← the original interactive CLI, now a thin
    wrapper that gathers input and calls archive_bottle().

All writes to bottles.csv are atomic (temp file + os.replace) and preserve the
exact column order found in the file's header.
"""

import csv
import os
import tempfile
from datetime import date
from pathlib import Path

BOTTLES_CSV = Path("data/bottles.csv")

# Canonical 16-column schema. Used only as a fallback if the file has no header
# yet; normal writes reuse the order read from the existing header.
FIELDNAMES = ["bottle_id", "name", "proof", "msrp", "acquisition_date",
              "acquisition_price", "batch", "bottle_code",
              "status", "removed_date", "sale_price", "removal_notes",
              "wooden_cork_url", "bbb_url",
              "barrel_tap_url", "keg_n_bottle_url"]

VALID_STATUSES = ["sold", "drank", "given_away", "lost"]


def read_header(path=BOTTLES_CSV):
    """Return the column order from the CSV header, or FIELDNAMES if absent."""
    path = Path(path)
    if not path.exists():
        return list(FIELDNAMES)
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), None)
    return header if header else list(FIELDNAMES)


def load_bottles():
    if not BOTTLES_CSV.exists():
        return []
    with open(BOTTLES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_bottles(bottles, fieldnames=None):
    """Atomically rewrite bottles.csv.

    Writes a temp file in the same directory, fsyncs, then os.replace()s it over
    the original so a crash or a concurrent run_all.py can never observe a
    half-written file. Column order comes from the existing header.
    """
    if fieldnames is None:
        fieldnames = read_header(BOTTLES_CSV)
    BOTTLES_CSV.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(BOTTLES_CSV.parent), prefix=".bottles_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(bottles)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, BOTTLES_CSV)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def archive_bottle(bottle_id, status, sale_price=None, notes=None):
    """Mark a bottle archived. Shared by the CLI and the Flask app.

    status   one of VALID_STATUSES (sold / drank / given_away / lost)
    sale_price  required when status == 'sold'; ignored otherwise
    notes    optional free text

    Stamps removed_date = today. Returns the updated bottle row dict.
    Raises ValueError on bad status, missing/invalid sale price, or unknown id.
    """
    status = (status or "").strip()
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")

    sale_price_str = ""
    if status == "sold":
        if sale_price is None or str(sale_price).strip() == "":
            raise ValueError("sale_price is required when status is 'sold'")
        try:
            sale_price_str = f"{float(sale_price):.2f}"
        except (TypeError, ValueError):
            raise ValueError(f"sale_price must be a number, got {sale_price!r}")

    fieldnames = read_header(BOTTLES_CSV)
    bottles = load_bottles()
    target = next((b for b in bottles if b.get("bottle_id") == bottle_id), None)
    if target is None:
        raise ValueError(f"no bottle with bottle_id {bottle_id!r}")

    target["status"] = status
    target["removed_date"] = date.today().isoformat()
    target["sale_price"] = sale_price_str
    target["removal_notes"] = (notes or "").strip()

    save_bottles(bottles, fieldnames)
    return target


# --------------------------------------------------------------------------- #
# Interactive CLI (unchanged behavior) — now just collects input and delegates
# to archive_bottle() above.
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

    notes = input("Removal notes (optional, enter to skip): ").strip()

    updated = archive_bottle(chosen["bottle_id"], new_status, sale_price, notes)

    print()
    print(f"  Archived {updated['bottle_id']} as {updated['status']}")
    print(f"  Removed date: {updated['removed_date']}")
    if updated["sale_price"]:
        print(f"  Sale price:   ${updated['sale_price']}")
    if updated["removal_notes"]:
        print(f"  Notes:        {updated['removal_notes']}")


if __name__ == "__main__":
    main()
