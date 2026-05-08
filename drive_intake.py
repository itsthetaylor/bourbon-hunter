import os
import io
import json
import base64
import csv
import re
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "google_credentials.json"
TOKEN_FILE = "token.json"

DRIVE_FOLDER_NAME = "Bourbon Hunter"
INTAKE_FOLDER_NAME = "photos_to_process"
PROCESSED_FOLDER_NAME = "photos_processed"
FAILED_FOLDER_NAME = "photos_failed"

BOTTLES_CSV = Path("data/bottles.csv")

FIELDNAMES = ["bottle_id", "name", "proof", "msrp", "acquisition_date",
              "acquisition_price", "wooden_cork_url", "bbb_url"]

CLAUDE_SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

claude = Anthropic()


def authenticate_drive():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, name, parent_id=None):
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if items:
        return items[0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def setup_drive_folders(service):
    root_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
    intake_id = get_or_create_folder(service, INTAKE_FOLDER_NAME, root_id)
    processed_id = get_or_create_folder(service, PROCESSED_FOLDER_NAME, root_id)
    failed_id = get_or_create_folder(service, FAILED_FOLDER_NAME, root_id)
    return {
        "root": root_id,
        "intake": intake_id,
        "processed": processed_id,
        "failed": failed_id,
    }


def list_intake_photos(service, intake_folder_id):
    query = f"'{intake_folder_id}' in parents and trashed=false and (mimeType contains 'image/')"
    results = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    return results.get("files", [])


def download_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def move_file(service, file_id, new_parent_id):
    file = service.files().get(fileId=file_id, fields="parents").execute()
    prev_parents = ",".join(file.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=prev_parents,
        fields="id, parents",
    ).execute()


def normalize_image(image_bytes, mime_type):
    """Convert image to a Claude-supported format if needed.
    Returns (bytes, mime_type) tuple."""
    if mime_type in CLAUDE_SUPPORTED_TYPES:
        return image_bytes, mime_type

    # Convert via Pillow (handles HEIC and anything else Pillow can read)
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=90)
    return output.getvalue(), "image/jpeg"


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "_", text)
    return text.strip("_")


def identify_bottle(image_bytes, mime_type):
    image_bytes, mime_type = normalize_image(image_bytes, mime_type)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

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

    message = claude.messages.create(
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
                            "media_type": mime_type,
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


def main():
    print("Authenticating with Google Drive...")
    service = authenticate_drive()

    print("Setting up folder structure...")
    folders = setup_drive_folders(service)
    print(f"  Drive folder: {DRIVE_FOLDER_NAME}/")
    print(f"    {INTAKE_FOLDER_NAME}/")
    print(f"    {PROCESSED_FOLDER_NAME}/")
    print(f"    {FAILED_FOLDER_NAME}/")

    photos = list_intake_photos(service, folders["intake"])

    if not photos:
        print(f"\nNo photos found in Drive '{INTAKE_FOLDER_NAME}' folder.")
        print("Drop bottle photos in that folder from your phone and re-run this script.")
        return

    print(f"\nFound {len(photos)} photo(s) to process\n")

    for photo in photos:
        print(f"Processing: {photo['name']}")
        print("-" * 60)

        try:
            image_bytes = download_file(service, photo["id"])
        except Exception as e:
            print(f"  Failed to download: {e}")
            print()
            continue

        result = identify_bottle(image_bytes, photo["mimeType"])

        if "error" in result:
            print(f"  Failed: {result['error']}")
            if "raw" in result:
                print(f"  Raw response: {result['raw'][:200]}")
            move_file(service, photo["id"], folders["failed"])
            print(f"  Moved to: {FAILED_FOLDER_NAME}/")
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

        move_file(service, photo["id"], folders["processed"])
        print(f"  Moved to: {PROCESSED_FOLDER_NAME}/")
        print()


if __name__ == "__main__":
    main()