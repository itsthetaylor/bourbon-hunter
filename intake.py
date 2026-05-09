import os
import json
import shutil
import base64
import csv
import re
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

PHOTOS_TO_PROCESS = Path("data/photos_to_process")
PHOTOS_PROCESSED = Path("data/photos_processed")
PHOTOS_FAILED = Path("data/photos_failed")
BOTTLES_CSV = Path("data/bottles.csv")

FIELDNAMES = ["bottle_id", "name", "proof", "msrp", "acquisition_date",
              "acquisition_price", "batch", "bottle_code",
              "wooden_cork_url", "bbb_url",
              "barrel_tap_url", "keg_n_bottle_url"]

client = Anthropic()


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "_", text)
    return text.strip("_")


def identify_bottle(image_path):
    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    suffix = image_path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")

    prompt = """You are identifying a bourbon/whiskey bottle from a photo.

IMPORTANT: Different batches of the same product (e.g., two EH Taylor Barrel
Proof bottles at 127.4 vs 127.3 proof) are DIFFERENT bottles. Capture every
distinguishing detail you can see so they can be told apart later.

Look carefully at:
- The front label (product name, distillery, age statement, MSRP)
- The neck label / back label
- Hand-written or stamped text anywhere on the label
- The BOTTOM of the bottle and the BACK of the bottle if visible
  (batch numbers, bottle codes, dump dates, and exact proofs are often there)

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):

{
  "name": "Full bottle name as commonly referenced",
  "distillery": "Distillery or brand",
  "proof": "Exact proof to one decimal place if visible (e.g. \\"127.4\\", not \\"127\\"). Null if not visible.",
  "batch": "Batch identifier if visible (e.g. \\"Batch 11\\", \\"B11\\", \\"Batch C923\\", or any hand-written batch text). Null if not visible.",
  "year": "Release year or vintage if visible, or null",
  "bottle_code": "Any other distinguishing identifier on the bottle — bottling/dump date, lot code, barrel number, hand-written annotation, stamped code, etc. Null if none visible.",
  "msrp": "MSRP in USD as a number, or null if unknown",
  "confidence": "high, medium, or low",
  "notes": "Any uncertainty or details to flag for the user, including anything you saw but couldn't fully read"
}

If you cannot identify the bottle at all, return:
{"error": "could not identify"}
"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "invalid response", "raw": text}


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


def add_bottle(bottle_data):
    bottle_id = slugify(bottle_data["name"])
    proof = bottle_data.get("proof") or ""
    msrp = bottle_data.get("msrp") or ""
    batch = bottle_data.get("batch") or ""
    bottle_code = bottle_data.get("bottle_code") or ""

    new_row = {
        "bottle_id": bottle_id,
        "name": bottle_data["name"],
        "proof": str(proof) if proof else "",
        "msrp": str(msrp) if msrp else "",
        "acquisition_date": "",
        "acquisition_price": "",
        "batch": str(batch) if batch else "",
        "bottle_code": str(bottle_code) if bottle_code else "",
        "wooden_cork_url": "",
        "bbb_url": "",
        "barrel_tap_url": "",
        "keg_n_bottle_url": "",
    }

    bottles = load_bottles()

    for b in bottles:
        if b["bottle_id"] == bottle_id:
            return bottle_id, "duplicate"

    bottles.append(new_row)
    save_bottles(bottles)
    return bottle_id, "added"


def process_photos():
    images = [p for p in PHOTOS_TO_PROCESS.iterdir()
              if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}]

    if not images:
        print(f"No photos found in {PHOTOS_TO_PROCESS}")
        return

    print(f"Found {len(images)} photo(s) to process\n")

    for image_path in images:
        print(f"Processing: {image_path.name}")
        print("-" * 60)

        result = identify_bottle(image_path)

        if "error" in result:
            print(f"  Failed: {result['error']}")
            if "raw" in result:
                print(f"  Raw response: {result['raw'][:200]}")
            shutil.move(str(image_path), str(PHOTOS_FAILED / image_path.name))
            print(f"  Moved to: {PHOTOS_FAILED}")
            print()
            continue

        print(f"  Name:       {result.get('name')}")
        print(f"  Distillery: {result.get('distillery')}")
        print(f"  Proof:      {result.get('proof')}")
        print(f"  Batch:      {result.get('batch')}")
        print(f"  Year:       {result.get('year')}")
        print(f"  Code:       {result.get('bottle_code')}")
        print(f"  MSRP:       {result.get('msrp')}")
        print(f"  Confidence: {result.get('confidence')}")
        if result.get("notes"):
            print(f"  Notes:      {result.get('notes')}")

        bottle_id, status = add_bottle(result)
        if status == "duplicate":
            print(f"  Already in collection (id: {bottle_id}) — skipped")
        else:
            print(f"  Added to bottles.csv as: {bottle_id}")

        shutil.move(str(image_path), str(PHOTOS_PROCESSED / image_path.name))
        print(f"  Moved to: {PHOTOS_PROCESSED}")
        print()


if __name__ == "__main__":
    process_photos()