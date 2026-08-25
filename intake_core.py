"""
intake_core.py — shared intake logic for BOTH photo paths.

intake.py (local folder) and drive_intake.py (Google Drive) are thin shells that
gather raw photos from their source, then hand them to the functions here. This
module is the single source of truth for:

  * the vision prompt
  * EXIF capture-time reading (JPEG + HEIC) and 180-second clustering into bottles
  * the multi-image vision call (all photos of one bottle in a single request)
  * response parsing + atomic add_bottle() (writes to the DB via db.insert_bottle)

Keeping it in one place means the local and Drive paths can never drift apart.
"""

import io
import os
import re
import sys
import json
import base64
import uuid
from datetime import datetime

# Windows cmd defaults to cp1252; bottle notes often contain Unicode (apostrophes,
# em-dashes, etc.). Reconfigure stdout/stderr to UTF-8 so print never throws
# UnicodeEncodeError — worst case a character is replaced, not a crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ExifTags
import pillow_heif
from dotenv import load_dotenv

# load_dotenv MUST run before importing anthropic — the SDK reads
# ANTHROPIC_API_KEY at import time and caches it. override=True ensures
# the .env value wins over a stale empty system/user environment variable.
load_dotenv(override=True)

from anthropic import Anthropic

import db

pillow_heif.register_heif_opener()  # lets Pillow open .heic/.heif (iPhone photos)

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 600
CLUSTER_WINDOW_SECONDS = 90   # photos within this gap = same bottle
MAX_IMAGES_PER_CALL = 6       # cap images per vision call — large HEIC batches hit the 413 limit

CLAUDE_SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
    ".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff",
}

# EXIF tag ids
_TAG_DATETIME = 306             # DateTime (base IFD) — fallback
_TAG_DATETIME_ORIGINAL = 36867  # DateTimeOriginal (Exif sub-IFD) — preferred

_client = None  # lazily constructed so importing this module never needs an API key


# --------------------------------------------------------------------------- #
# Vision prompt (STEP 4 — granular detail, empty-not-guessed)
# --------------------------------------------------------------------------- #

VISION_PROMPT = """You are identifying a single bourbon/whiskey bottle from one or more photos.

The images you receive (there may be several) are ALL of the SAME bottle — typically
the front label plus the bottle bottom and/or the back/neck label and any hand-written
tag. Read across ALL of the images and combine what you see into ONE description.

Why detail matters: different batches of the same product line (for example, two
E.H. Taylor Barrel Proof bottles at 127.4 vs 127.3 proof) are DIFFERENT bottles that
can be worth different amounts. Capture every distinguishing detail so two similar
bottles can be told apart later.

Where to look:
- Front label: product name, distillery, age statement, MSRP, proof.
- Neck and back label: proof, batch, release/vintage, bottling info.
- The BOTTLE BOTTOM and BACK: hand-written or stamped codes, store/dusty codes,
  fill or dump dates, lot/barrel numbers. These are often the ONLY way to tell two
  batches apart — read them carefully.

ACCURACY RULE — this is critical:
If a field is not clearly legible, or is not present, return it as an EMPTY string "".
DO NOT guess, infer, or fill in a typical/expected value. A blank the user can fill in
is far better than a wrong value the user has to discover and correct weeks later.

Respond ONLY with valid JSON in this exact shape (no markdown, no extra text):

{
  "name": "Full bottle name as commonly referenced",
  "distillery": "Distillery or brand, or \\"\\"",
  "proof": "Proof to ONE DECIMAL PLACE exactly as printed (e.g. \\"127.4\\", not \\"127\\" and not \\"127 proof\\"). \\"\\" if not clearly legible.",
  "batch": "Batch number/code if printed or hand-written (e.g. \\"Batch 25A\\", \\"C920\\", \\"B11\\"). \\"\\" if none.",
  "year": "Release year or vintage if shown, otherwise \\"\\"",
  "bottle_code": "Hand-written or stamped code, ESPECIALLY from the bottle bottom or back label — store/dusty code, fill/dump date, lot or barrel number, any stamped annotation. \\"\\" if none legible.",
  "msrp": "MSRP in USD as a number only, or \\"\\" if not shown",
  "confidence": "high, medium, or low",
  "notes": "Anything uncertain or only partially legible — including text you saw but could not fully read. Empty string if nothing to flag."
}

If the images do not show an identifiable bottle at all, return exactly:
{"error": "could not identify"}
"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def slugify(text):
    text = (text or "").lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "_", text)
    return text.strip("_")


def make_product_key(name, proof, batch):
    """Stable display-grouping label: name+proof+batch slug. Not a FK."""
    parts = [slugify(name)]
    if proof:
        parts.append(proof.replace(".", "_"))
    if batch:
        parts.append(slugify(batch))
    return "_".join(p for p in parts if p)


def mime_from_name(name):
    ext = os.path.splitext(name or "")[1].lower()
    return EXT_MIME.get(ext, "image/jpeg")


def _clean(value):
    """Normalize a model field to a stored string. Treats null/None/'n/a' as blank."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("null", "none", "n/a", "na", "unknown"):
        return ""
    return s


# --------------------------------------------------------------------------- #
# EXIF capture time + clustering (STEP 2)
# --------------------------------------------------------------------------- #

def _parse_exif_dt(val):
    if not isinstance(val, str):
        return None
    val = val.strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def read_capture_time(image_bytes):
    """Return the photo's capture datetime from EXIF, or None.

    Prefers DateTimeOriginal (when the shutter fired); falls back to the base
    DateTime tag. Works for JPEG and HEIC (pillow_heif is registered).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None
    # DateTimeOriginal lives in the Exif sub-IFD
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        dt = _parse_exif_dt(sub.get(_TAG_DATETIME_ORIGINAL))
        if dt:
            return dt
    except Exception:
        pass
    return _parse_exif_dt(exif.get(_TAG_DATETIME))


def cluster_photos(photos, window_seconds=CLUSTER_WINDOW_SECONDS):
    """Group photos into bottles by capture time.

    `photos` is a list of dicts, each with at least:
        key            display name (filename / Drive name)
        bytes          raw image bytes
        fallback_time  datetime or None (file mtime / Drive modifiedTime)

    Returns (groups, problem):
      * groups  — list of lists of photo dicts, in time order, on success
      * problem — None on success, else a human-readable HARD-STOP message

    Each photo dict is annotated in place with:
        capture_time   datetime used for clustering
        time_source    'exif' | 'file'

    HARD STOPS (no partial writes):
      * any photo with neither EXIF nor a usable fallback time
      * 2+ photos that all share the exact same timestamp (clustering impossible —
        usually means EXIF was stripped in transfer)
    """
    if not photos:
        return [], None

    missing = []
    for p in photos:
        ct = read_capture_time(p["bytes"])
        src = "exif"
        if ct is None:
            ct = p.get("fallback_time")
            src = "file"
        if ct is None:
            src = "none"
            missing.append(p["key"])
        p["capture_time"] = ct
        p["time_source"] = src

    if missing:
        return None, (
            "No usable timestamp (neither EXIF nor file time) for: "
            + ", ".join(missing)
            + ".\n  These can't be grouped safely. Re-transfer them preserving "
              "EXIF, or group/process them manually, then re-run."
        )

    if len(photos) > 1 and len({p["capture_time"] for p in photos}) == 1:
        stamp = photos[0]["capture_time"].isoformat(timespec="seconds")
        return None, (
            f"All {len(photos)} photos share the exact same timestamp ({stamp}).\n"
            "  EXIF was probably stripped in transfer, so I can't tell where one "
            "bottle ends and the next begins. Re-transfer preserving original EXIF "
            "(e.g. Drive/AirDrop 'original'), or group them manually, then re-run.\n"
            "  Affected: " + ", ".join(p["key"] for p in photos)
        )

    ordered = sorted(photos, key=lambda p: p["capture_time"])
    groups = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        gap = (cur["capture_time"] - prev["capture_time"]).total_seconds()
        if gap > window_seconds:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups, None


def format_groups(groups):
    lines = []
    for i, g in enumerate(groups, 1):
        items = ", ".join(f"{p['key']} [{p['time_source']}]" for p in g)
        t0 = g[0]["capture_time"].isoformat(timespec="seconds")
        lines.append(f"  Bottle {i}: [{items}]  (starts {t0})")
    return "\n".join(lines)


def confirm_groups(groups, yes=False):
    """Show the proposed grouping and ask before any CSV write. Returns bool.

    Pass yes=True (or --yes on the CLI) to skip the interactive prompt and
    proceed automatically — useful when stdin is not a terminal.
    """
    print("\nProposed grouping — one bottle per group, [exif] vs [file] shows the "
          "time source:")
    print(format_groups(groups))
    if yes:
        print(f"\n--yes flag set: proceeding with {len(groups)} bottle(s).")
        return True
    ans = input(
        f"\nProceed to identify & write {len(groups)} bottle(s)? [y/N]: "
    ).strip().lower()
    return ans in ("y", "yes")


# --------------------------------------------------------------------------- #
# Vision call (STEP 3 — all of a bottle's photos in ONE request)
# --------------------------------------------------------------------------- #

def normalize_image(image_bytes, mime_type):
    """Convert to a Claude-supported format if needed (e.g. HEIC -> JPEG)."""
    if mime_type in CLAUDE_SUPPORTED_TYPES:
        return image_bytes, mime_type
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def identify_bottle(images):
    """Identify one bottle from a list of (image_bytes, mime_type) tuples.

    All images are sent in a single vision call so the model can combine detail
    from the front label, bottle bottom, and back. Capped at MAX_IMAGES_PER_CALL
    to avoid hitting the API request-size limit.
    """
    if len(images) > MAX_IMAGES_PER_CALL:
        print(f"  Note: {len(images)} images in group; sending first {MAX_IMAGES_PER_CALL} to stay under API size limit.")
        images = images[:MAX_IMAGES_PER_CALL]
    global _client
    if _client is None:
        _api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not _api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is empty. Check your .env file and ensure "
                "it is set before importing anthropic."
            )
        _client = Anthropic(api_key=_api_key)

    content = []
    for image_bytes, mime_type in images:
        norm_bytes, norm_mime = normalize_image(image_bytes, mime_type)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": norm_mime,
                "data": base64.standard_b64encode(norm_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": VISION_PROMPT})

    message = _client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": content}],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "invalid response", "raw": text}


def print_result(result):
    """Print the identified fields (shared by both shells)."""
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


# --------------------------------------------------------------------------- #
# Write (atomic, via db.insert_bottle — single SQLite transaction)
# --------------------------------------------------------------------------- #

def _resolve_slug(base_slug, proof, batch, existing_ids):
    """Return a collision-free bottle_id.

    Builds progressively more specific candidates and returns the first one
    that doesn't already exist. A discriminator is added ONLY when needed, so
    single bottles with unique names always get clean slugs.

    Progression:
      1. base_slug                         "eh_taylor_barrel_proof"
      2. base_slug + proof                 "eh_taylor_barrel_proof_127_4"
      3. base_slug + proof + batch         "eh_taylor_barrel_proof_127_4_25a"
         (or base + batch if proof absent) "eh_taylor_barrel_proof_25a"
      4. most-specific-candidate + 8-char UUID fragment (last resort)
    """
    if base_slug not in existing_ids:
        return base_slug

    proof_str = _clean(proof)
    batch_str = slugify(_clean(batch))

    # Build the candidate list in increasing specificity
    candidates = []
    if proof_str:
        proof_slug = base_slug + "_" + proof_str.replace(".", "_")
        candidates.append(proof_slug)
        if batch_str:
            candidates.append(proof_slug + "_" + batch_str)
    elif batch_str:
        candidates.append(base_slug + "_" + batch_str)

    for candidate in candidates:
        if candidate not in existing_ids:
            return candidate

    # Last resort: UUID fragment appended to the most specific candidate
    # (or base_slug when neither proof nor batch were available)
    prefix = candidates[-1] if candidates else base_slug
    return prefix + "_" + uuid.uuid4().hex[:8]


def owner_user_id():
    """The account new intake bottles are assigned to — the admin/owner. Returns
    None if no account exists yet (caller should tell the user to sign up first).
    Multi-user note: batch intake currently assigns to the owner; per-user intake
    would pass an explicit user_id here later."""
    return db.get_admin_user_id()


def add_bottle(bottle_data, user_id):
    """Insert one identified bottle into the DB, owned by user_id (atomic).

    Returns (bottle_id, status) where status is 'added' or 'no_name'.
    Slug collisions are resolved by _resolve_slug against the DB's existing ids
    (bottle_id is a GLOBAL primary key, so collisions are checked across all users)
    — a discriminator (proof, batch, or UUID fragment) is appended only when the
    base name slug already exists, so unique bottles keep clean ids.
    """
    name = (bottle_data.get("name") or "").strip()
    if not name:
        return None, "no_name"

    existing_ids = db.get_bottle_ids()

    proof = _clean(bottle_data.get("proof"))
    batch = _clean(bottle_data.get("batch"))
    bottle_id = _resolve_slug(slugify(name), proof, batch, existing_ids)

    db.insert_bottle(
        bottle_id=bottle_id,
        user_id=user_id,
        product_key=make_product_key(name, proof, batch),
        name=name,
        proof=proof,
        batch=batch,
        bottle_code=_clean(bottle_data.get("bottle_code")),
        msrp=_clean(bottle_data.get("msrp")),
        status="active",
    )
    return bottle_id, "added"
