"""
Local-folder intake.

Drop bottle photos into data/photos_to_process/ (front label + bottle bottom +
back / hand-written info). This script:
  1. reads each photo's capture time,
  2. clusters photos within CLUSTER_WINDOW_SECONDS (90s) into a candidate group
     (intake_core.cluster_photos),
  3. sends each group to ONE vision call, which may report SEVERAL distinct
     bottles in that group (time proximity alone doesn't mean "same bottle"),
  4. shows you the grouping WITH the names vision read, flagging any group it
     split, and waits for confirmation, and
  5. writes each identified bottle as its own row (atomic, via db.insert_bottle),
     filing each photo under photos_processed only if a bottle claimed it, and
     under photos_failed otherwise.

All identification/clustering/writing logic lives in intake_core.py so this and
drive_intake.py stay identical in behavior.
"""

import sys
import shutil
from datetime import datetime
from pathlib import Path

import intake_core as core

PHOTOS_TO_PROCESS = Path("data/photos_to_process")
PHOTOS_PROCESSED = Path("data/photos_processed")
PHOTOS_FAILED = Path("data/photos_failed")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif",
              ".heic", ".heif", ".tif", ".tiff"}


def _move_all(group, dest):
    dest.mkdir(parents=True, exist_ok=True)
    for p in group:
        shutil.move(str(p["path"]), str(dest / p["path"].name))


def main():
    yes = "--yes" in sys.argv

    paths = sorted(
        p for p in PHOTOS_TO_PROCESS.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not paths:
        print(f"No photos found in {PHOTOS_TO_PROCESS}")
        return

    photos = []
    for p in paths:
        photos.append({
            "key": p.name,
            "bytes": p.read_bytes(),
            "mime": core.mime_from_name(p.name),
            "fallback_time": datetime.fromtimestamp(p.stat().st_mtime),
            "path": p,
        })

    print(f"Found {len(photos)} photo(s); reading capture times...")
    groups, problem = core.cluster_photos(photos)
    if problem:
        print("\n  Cannot group photos:\n  " + problem)
        print("\nNothing was written.")
        return

    # Checked before the vision calls so a missing account costs no API spend.
    owner_id = core.owner_user_id()
    if owner_id is None:
        print("No account exists yet. Create your account in the app first, then re-run intake.")
        return

    # Identify BEFORE confirming: the confirmation has to show the names vision
    # read so a misread or a bad split can be caught before anything is written.
    print(f"\nIdentifying {len(groups)} photo group(s)...")
    identified = []
    for i, group in enumerate(groups, 1):
        print(f"  Group {i} of {len(groups)} ({len(group)} photo(s))...")
        identified.append(core.identify_bottle(group))

    # Flag what add_bottle will skip, so the confirm screen shows it up front.
    core.annotate_duplicates(identified, owner_id)

    if not core.confirm_groups(groups, identified, yes=yes):
        print("Aborted — nothing written.")
        return

    for i, (group, bottles) in enumerate(zip(groups, identified), 1):
        keys = ", ".join(p["key"] for p in group)
        print(f"\nGroup {i}: {keys}")
        print("-" * 60)

        if len(bottles) == 1 and "error" in bottles[0]:
            print(f"  Failed: {bottles[0]['error']}")
            if "raw" in bottles[0]:
                print(f"  Raw response: {bottles[0]['raw'][:200]}")
            _move_all(group, PHOTOS_FAILED)
            print(f"  Moved {len(group)} photo(s) to {PHOTOS_FAILED}")
            continue

        if len(bottles) > 1:
            print(f"  Vision found {len(bottles)} distinct bottles here — "
                  "writing each as its own row.")

        # Bottles whose photos count as done: written, or already in the DB.
        # A bottle that fails to write, or has no name, is NOT in this list, so
        # partition_group below sends its photos to failed. One bad bottle
        # never aborts the group, let alone the run.
        done = []
        for b in bottles:
            if b.get("photo_keys"):
                print(f"\n  From: {', '.join(b['photo_keys'])}")
            core.print_result(b)

            try:
                bottle_id, status = core.add_bottle(b, owner_id)
            except Exception as e:  # noqa: BLE001 — isolate this bottle, keep going
                print(f"  FAILED to write this bottle: {type(e).__name__}: {e}")
                print("  Its photos go to the failed folder; continuing with the rest.")
                continue

            if status == "duplicate":
                print(f"  Already in collection (id: {bottle_id}) — skipped; "
                      "photos filed as processed")
                done.append(b)
            elif status == "no_name":
                print("  Model returned no name — not written; photos go to failed")
            else:
                print(f"  Added to the database as: {bottle_id}")
                done.append(b)

        # Only photos attached to a bottle that is now in the DB are
        # "processed". Everything else had nothing written for it, so it goes
        # to failed where it stays visible instead of looking done.
        assigned, unassigned = core.partition_group(group, done)
        if unassigned:
            names = ", ".join(p["key"] for p in unassigned)
            print(f"  {len(unassigned)} photo(s) NOT assigned to any bottle: {names}")
            _move_all(unassigned, PHOTOS_FAILED)
            print(f"  Moved those to {PHOTOS_FAILED} — nothing was written for them.")
        if assigned:
            _move_all(assigned, PHOTOS_PROCESSED)
            print(f"  Moved {len(assigned)} photo(s) to {PHOTOS_PROCESSED}")


if __name__ == "__main__":
    main()
