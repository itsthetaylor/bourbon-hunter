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
              "acquisition_price", "wooden_cork_url", "bbb_url"]

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

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):

{
  "name": "Full bottle name as commonly referenced",
  "distillery": "Distillery or brand",
  "proof": "Proof as a number, or null if not visible",
  "year": "Release year if visible, or null",
  "msrp": "MSRP in USD as a number, or null if unknown",
  "confidence": "high, medium, or low",
  "notes": "Any uncertainty or details to flag for the user"
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

    new_row = {
        "bottle_id": bottle_id,
        "name": bottle_data["name"],
        "proof": str(proof) if proof else "",
        "msrp": str(msrp) if msrp else "",
        "acquisition_date": "",
        "acquisition_price": "",
        "wooden_cork_url": "",
        "bbb_url": "",
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
        print(f"  Year:       {result.get('year')}")
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