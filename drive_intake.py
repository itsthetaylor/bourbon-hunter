"""
Google Drive intake.

Pulls bottle photos from the Drive 'Bourbon Hunter/photos_to_process' folder,
then uses the SAME shared logic as the local path (intake_core):
  1. download every intake photo (we read EXIF from the bytes, so local and
     Drive clustering are one identical code path),
  2. cluster photos within 180s into one bottle,
  3. show the proposed grouping and wait for confirmation,
  4. send each bottle's photos to ONE vision call,
  5. write to bottles.csv (atomic) and move that bottle's photos to processed/failed.
"""

import io
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import intake_core as core

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "google_credentials.json"
TOKEN_FILE = "token.json"

DRIVE_FOLDER_NAME = "Bourbon Hunter"
INTAKE_FOLDER_NAME = "photos_to_process"
PROCESSED_FOLDER_NAME = "photos_processed"
FAILED_FOLDER_NAME = "photos_failed"


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
    return {
        "root": root_id,
        "intake": get_or_create_folder(service, INTAKE_FOLDER_NAME, root_id),
        "processed": get_or_create_folder(service, PROCESSED_FOLDER_NAME, root_id),
        "failed": get_or_create_folder(service, FAILED_FOLDER_NAME, root_id),
    }


def list_intake_photos(service, intake_folder_id):
    query = f"'{intake_folder_id}' in parents and trashed=false and (mimeType contains 'image/')"
    results = service.files().list(
        q=query, fields="files(id, name, mimeType, modifiedTime)"
    ).execute()
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


def _parse_drive_time(s):
    """Drive modifiedTime (RFC3339, e.g. 2026-06-03T12:34:56.000Z) -> datetime."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def main():
    print("Authenticating with Google Drive...")
    service = authenticate_drive()

    print("Setting up folder structure...")
    folders = setup_drive_folders(service)
    print(f"  {DRIVE_FOLDER_NAME}/{INTAKE_FOLDER_NAME}/")

    files = list_intake_photos(service, folders["intake"])
    if not files:
        print(f"\nNo photos found in Drive '{INTAKE_FOLDER_NAME}' folder.")
        print("Drop bottle photos in that folder from your phone and re-run.")
        return

    print(f"\nFound {len(files)} photo(s); downloading to read capture time...")
    photos = []
    for f in files:
        try:
            data = download_file(service, f["id"])
        except Exception as e:
            print(f"  Failed to download {f['name']}: {e}")
            print("Stopping — nothing written. Re-run once Drive is reachable.")
            return
        photos.append({
            "key": f["name"],
            "bytes": data,
            "mime": f.get("mimeType") or core.mime_from_name(f["name"]),
            "fallback_time": _parse_drive_time(f.get("modifiedTime")),
            "file_id": f["id"],
        })

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
            for p in group:
                move_file(service, p["file_id"], folders["failed"])
            print(f"  Moved {len(group)} photo(s) to {FAILED_FOLDER_NAME}/")
            continue

        core.print_result(result)

        bottle_id, status = core.add_bottle(result)
        if status == "duplicate":
            print(f"  Already in collection (id: {bottle_id}) — skipped")
        elif status == "no_name":
            print("  Model returned no name — skipped (not written)")
        else:
            print(f"  Added to bottles.csv as: {bottle_id}")

        for p in group:
            move_file(service, p["file_id"], folders["processed"])
        print(f"  Moved {len(group)} photo(s) to {PROCESSED_FOLDER_NAME}/")


if __name__ == "__main__":
    main()
