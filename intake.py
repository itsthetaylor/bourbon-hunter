"""
Local-folder intake.

Drop bottle photos into data/photos_to_process/ (front label + bottle bottom +
back / hand-written info). This script:
  1. reads each photo's capture time,
  2. clusters photos within 180s into one bottle (intake_core.cluster_photos),
  3. shows you the proposed grouping and waits for confirmation,
  4. sends each bottle's photos to ONE vision call, and
  5. writes the bottle to bottles.csv (atomic).

All identification/clustering/writing logic lives in intake_core.py so this and
drive_intake.py stay identical in behavior.
"""

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

    if not core.confirm_groups(groups):
        print("Aborted — nothing written.")
        return

    for i, group in enumerate(groups, 1):
        keys = ", ".join(p["key"] for p in group)
        print(f"\nBottle {i}: {keys}")
        print("-" * 60)

        result = core.identify_bottle([(p["bytes"], p["mime"]) for p in group])

        if "error" in result:
            print(f"  Failed: {result['error']}")
            if "raw" in result:
                print(f"  Raw response: {result['raw'][:200]}")
            _move_all(group, PHOTOS_FAILED)
            print(f"  Moved {len(group)} photo(s) to {PHOTOS_FAILED}")
            continue

        core.print_result(result)

        bottle_id, status = core.add_bottle(result)
        if status == "duplicate":
            print(f"  Already in collection (id: {bottle_id}) — skipped")
        elif status == "no_name":
            print("  Model returned no name — skipped (not written)")
        else:
            print(f"  Added to bottles.csv as: {bottle_id}")

        _move_all(group, PHOTOS_PROCESSED)
        print(f"  Moved {len(group)} photo(s) to {PHOTOS_PROCESSED}")


if __name__ == "__main__":
    main()
