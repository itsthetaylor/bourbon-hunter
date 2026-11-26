"""
intake_core.py — shared intake logic for BOTH photo paths.

intake.py (local folder) and drive_intake.py (Google Drive) are thin shells that
gather raw photos from their source, then hand them to the functions here. This
module is the single source of truth for:

  * the vision prompt
  * EXIF capture-time reading (JPEG + HEIC) and 90-second clustering into candidate
    groups (CLUSTER_WINDOW_SECONDS)
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

from PIL import Image, ExifTags, ImageOps
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
MAX_TOKENS = 2000             # must fit ONE JSON entry per bottle for a whole
                              # group. Worst case is every photo being its own
                              # bottle, so this scales with MAX_IMAGES_PER_CALL:
                              # ~100 tokens/entry x 20 + slack. A truncated
                              # response is invalid JSON and fails the whole
                              # group, and unused headroom costs nothing.
CLUSTER_WINDOW_SECONDS = 90   # photos within this gap = same bottle
MAX_IMAGES_PER_CALL = 20      # cap per vision call. cluster_photos sub-chunks any
                              # time group over this at its largest internal gaps,
                              # so identify_bottle's own check is only a safety net.
MAX_IMAGE_EDGE_PX = 1568      # Claude downsizes beyond this itself, so sending more
                              # pixels costs request size with no accuracy gain
JPEG_QUALITY = 85             # re-encode quality for every image we send

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

VISION_PROMPT = """You are identifying bottles of bourbon, other spirits, or wine from a set of photos.

Each photo is labelled "Photo 1:", "Photo 2:" and so on, in the order given.

These photos may show MORE THAN ONE distinct bottle. They were grouped only by the
time they were taken, which is not reliable evidence that they show the same bottle:
two different bottles photographed moments apart land in the same group.

Group the photos by what the LABELS say — name, proof, batch/dump, bottle code — not
by how close together they were taken. Return ONE entry per distinct PHYSICAL bottle,
listing which photos belong to it.

A back label, neck label, bottle bottom, hang tag or hand-written tag shot STAYS
WITH the bottle it belongs to; it is not a separate bottle. Several photos of one
bottle are one entry. Use the readable details to decide: the same name AND the same
proof AND the same batch/code AND the same volume means one bottle; a different
proof, batch, dump code, bottle number, OR VOLUME (750ml vs 1L vs 1.75L) means a
different bottle, even for the same product line.

Two or more bottles visible together in ONE frame are ALWAYS separate entries, one
per bottle, even if they look identical.

Boxes, tubes, bags, tins and gift packaging are NOT bottles. Never create an entry
for packaging. A photo that shows only packaging stays unassigned; when a bottle is
in frame with its box, the entry is for the bottle.

A tissue-wrapped, bagged or otherwise obscured label where the NAME cannot be read
with confidence: leave those photos unassigned. Do not infer a bottle from its
shape, colour, closure or packaging.

Japanese and other export labels often print ABV instead of proof (e.g. "51.5% vol").
If only ABV is shown, report proof as ABV x 2 to one decimal (51.5% -> "103.0") and
write "proof derived from ABV" in notes.

If you cannot tell which bottle a photo belongs to, leave it out of every entry
rather than guessing — an unassigned photo is reported to the user, a wrongly
merged one is not.

Why detail matters: different batches of the same product line (for example, two
E.H. Taylor Barrel Proof bottles at 127.4 vs 127.3 proof) are DIFFERENT bottles that
can be worth different amounts. Capture every distinguishing detail so two similar
bottles can be told apart later.

Where to look:
- Front label: product name, distillery, age statement, MSRP, proof.
- Volume: usually printed small on the front label's lower edge or the back
  label, as "750ml", "1L", "1.75L" or "375ml". Read it if it is legible.
- Neck and back label: proof, batch, release/vintage, bottling info.
- The BOTTLE BOTTOM and BACK: hand-written or stamped codes, store/dusty codes,
  fill or dump dates, lot/barrel numbers. These are often the ONLY way to tell two
  batches apart — read them carefully.
- HANG TAGS and neck tags (Blanton's, Weller Single Barrel, store picks, BTAC):
  dumped-on date, barrel number, rick number, warehouse letter, bottle number.
  Report each in its own field below.

ACCURACY RULE — this is critical:
If a field is not clearly legible, or is not present, return it as an EMPTY string "".
DO NOT guess, infer, or fill in a typical/expected value. A blank the user can fill in
is far better than a wrong value the user has to discover and correct weeks later.

Respond ONLY with valid JSON in this exact shape (no markdown, no extra text).
"bottles" is a LIST — one entry per distinct physical bottle, even if there is only one.
"photo_numbers" is a JSON LIST OF INTEGERS (the photo labels), never a string: every
photo of this bottle — front, back, bottom, tag — goes in that list, and no photo
number appears under two different bottles.

{
 "bottles": [
  {
  "photo_numbers": [1, 3],
  "name": "Full bottle name as commonly referenced",
  "distillery": "Distillery or brand, or \\"\\"",
  "proof": "Proof to ONE DECIMAL PLACE exactly as printed (e.g. \\"127.4\\", not \\"127\\" and not \\"127 proof\\"). \\"\\" if not clearly legible.",
  "batch": "Batch number/code if printed or hand-written (e.g. \\"Batch 25A\\", \\"C920\\", \\"B11\\"). \\"\\" if none.",
  "year": "Release year or vintage if shown, otherwise \\"\\"",
  "bottle_code": "Hand-written or stamped store/dusty code or lot code from the bottle bottom or back label, NOT the hang-tag fields below. \\"\\" if none legible.",
  "dump_date": "Dumped-on / bottled-on date from a hang tag or back label, exactly as printed (e.g. \\"12-13-25\\"). \\"\\" if absent.",
  "barrel_no": "Barrel number from a hang tag or label (e.g. \\"218\\"). \\"\\" if absent.",
  "rick_no": "Rick number from a hang tag (e.g. \\"9\\"). \\"\\" if absent.",
  "warehouse": "Warehouse letter or number from a hang tag (e.g. \\"H\\"). \\"\\" if absent.",
  "bottle_no": "Bottle number from a hang tag or limited-release label (e.g. \\"014\\", or \\"123 of 500\\" as \\"123/500\\"). \\"\\" if absent.",
  "msrp": "MSRP in USD as a number only, or \\"\\" if not shown",
  "category": "EXACTLY one of \\"bourbon\\", \\"spirit\\", or \\"wine\\". Use \\"bourbon\\" for bourbon, rye and other American whiskey; \\"spirit\\" for any other distilled spirit (scotch, Irish, Japanese whisky, tequila, mezcal, rum, gin, vodka, cognac, liqueur); \\"wine\\" for still or sparkling wine, champagne, port, madeira and sherry. If you genuinely cannot tell, use \\"bourbon\\".",
  "volume": "Bottle volume EXACTLY as printed on the label, keeping its unit (e.g. \\"750ml\\", \\"1L\\", \\"1.75L\\", \\"375ml\\"). \\"\\" if not legible -- do NOT assume 750ml.",
  "confidence": "high, medium, or low",
  "notes": "Anything uncertain or only partially legible — including text you saw but could not fully read. Empty string if nothing to flag."
  }
 ]
}

If NONE of the photos show an identifiable bottle, return exactly:
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


def make_product_key(name, proof, batch, volume_ml=None):
    """Stable display-grouping label: name+proof+batch+volume slug. Not a FK.

    Volume is always appended, not just for non-750ml bottles: the key stays
    predictable and does not silently change meaning if DEFAULT_VOLUME_ML ever
    does. Including it means a 750ml and a 1.75L of the same product get
    different keys and so group as separate cards instead of collapsing into
    one."""
    parts = [slugify(name)]
    if proof:
        parts.append(proof.replace(".", "_"))
    if batch:
        parts.append(slugify(batch))
    parts.append(f"{db.normalize_volume_ml(volume_ml)}ml")
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


STANDARD_VOLUME_ML = 750


def parse_volume_ml(raw):
    """Label volume text -> integer millilitres, or None if unreadable.

    Handles "750ml", "1L", "1.75L", "1,75 L", "375 mL", "70cl" and a bare
    number. A bare number <= 10 is read as litres ("1.75" -> 1750), anything
    larger as millilitres ("750" -> 750). Returns None rather than guessing, so
    the caller can fall back to the column default instead of recording a wrong
    volume.
    """
    s = (str(raw) if raw is not None else "").strip().lower()
    if not s:
        return None
    s = s.replace(" ", "").rstrip(".")
    m = re.match(
        r"^([0-9]+(?:[.,][0-9]+)?)"
        r"(ml|milliliters?|millilitres?|cl|centiliters?|centilitres?|l|liters?|litres?)?$",
        s,
    )
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2) or ""
    if unit.startswith("ml") or unit.startswith("milli"):
        ml = num
    elif unit.startswith("cl") or unit.startswith("centi"):
        ml = num * 10
    elif unit:
        ml = num * 1000
    else:
        ml = num * 1000 if num <= 10 else num
    ml = int(round(ml))
    return ml if ml > 0 else None


# Re-exported from db so intake output and the web card format volumes the same.
format_volume = db.format_volume


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


def _split_oversize(group, max_size):
    """Recursively split a time group larger than max_size at its largest
    internal capture-time gap.

    The largest gap is the most likely bottle boundary inside a run of photos
    that time-clustering could not separate. EXIF time has 1-second resolution,
    so ties are common; a tie is broken toward the middle of the group so a
    run of evenly spaced photos halves instead of peeling off one photo at a
    time. Returns a list of chunks, each <= max_size, in time order.
    """
    if len(group) <= max_size:
        return [group]
    gaps = [(group[i + 1]["capture_time"] - group[i]["capture_time"]).total_seconds()
            for i in range(len(group) - 1)]
    best = max(gaps)
    mid = (len(group) - 1) / 2.0
    cut = min((i for i, g in enumerate(gaps) if g == best), key=lambda i: abs(i - mid))
    return (_split_oversize(group[:cut + 1], max_size)
            + _split_oversize(group[cut + 1:], max_size))


def cluster_photos(photos, window_seconds=CLUSTER_WINDOW_SECONDS,
                   max_images=MAX_IMAGES_PER_CALL):
    """Group photos into candidate bottle groups by capture time.

    `photos` is a list of dicts, each with at least:
        key            display name (filename / Drive name)
        bytes          raw image bytes
        fallback_time  datetime or None (file mtime / Drive modifiedTime)

    Returns (groups, problem):
      * groups  — list of lists of photo dicts, in time order, on success.
                  Any time group larger than max_images is sub-chunked at its
                  largest internal gaps (see _split_oversize), so every group
                  fits one vision call. Shooting a whole shelf without 90s
                  pauses produces one huge time group; this is what keeps that
                  from hard-stopping the run.
      * problem — None on success, else a human-readable HARD-STOP message

    Each photo dict is annotated in place with:
        capture_time   datetime used for clustering
        time_source    'exif' | 'file'
        chunk_of       only on photos from a sub-chunked group:
                       {part, of, parent_size, gap_s} — gap_s is the seconds
                       between this chunk and the previous one (None for part 1)

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

    # Sub-chunk anything a single vision call can't take. The chunk boundary is
    # a guess at a bottle boundary, so each chunk is tagged for the confirm
    # screen: the user should know a cut was forced, and how big the gap was.
    final = []
    for g in groups:
        if len(g) <= max_images:
            final.append(g)
            continue
        parts = _split_oversize(g, max_images)
        for k, part in enumerate(parts, 1):
            gap = (None if k == 1 else
                   (part[0]["capture_time"] - parts[k - 2][-1]["capture_time"]).total_seconds())
            for p in part:
                p["chunk_of"] = {"part": k, "of": len(parts),
                                 "parent_size": len(g), "gap_s": gap}
        final.extend(parts)
    return final, None


def _is_error(bottle):
    return isinstance(bottle, dict) and "error" in bottle


def bottle_label(b):
    """One-line label for a bottle vision read, for the confirmation output."""
    name = _clean(b.get("name")) or "(no name read)"
    bits = []
    proof = _clean(b.get("proof"))
    if proof:
        bits.append(f"{proof} proof")
    batch = _clean(b.get("batch"))
    if batch:
        bits.append(f"Batch {batch}")
    code = compose_bottle_code(b)
    if code:
        bits.append(code)
    vol = parse_volume_ml(b.get("volume"))
    if vol and vol != STANDARD_VOLUME_ML:
        bits.append(format_volume(vol))
    cat = db.normalize_category(b.get("category"))
    if cat != db.DEFAULT_CATEGORY:
        bits.append(cat)
    conf = _clean(b.get("confidence")).lower()
    if conf and conf != "high":
        bits.append(f"{conf} confidence")
    label = name + (f"  ({', '.join(bits)})" if bits else "")
    dup = b.get("duplicate_of")
    if dup:
        label += f"\n         ** DUPLICATE of {dup} — will be skipped, not written"
    return label


def format_groups(groups, identified=None):
    """Render the time groups, and -- when `identified` is supplied -- what vision
    read from each group's labels, flagging splits and unassigned photos.

    `identified` is a list parallel to `groups`; each element is the list
    identify_bottle returned for that group.
    """
    lines = []
    for i, g in enumerate(groups, 1):
        items = ", ".join(f"{p['key']} [{p['time_source']}]" for p in g)
        t0 = g[0]["capture_time"].isoformat(timespec="seconds")
        lines.append(f"  Group {i}: [{items}]  (starts {t0})")
        ck = g[0].get("chunk_of")
        if ck:
            gap = f", cut at a {ck['gap_s']:.0f}s gap" if ck["gap_s"] is not None else ""
            lines.append(f"      (size-split: part {ck['part']}/{ck['of']} of a "
                         f"{ck['parent_size']}-photo time cluster{gap} — a bottle may "
                         f"straddle this cut; check its photos are all here)")

        if identified is None:
            continue

        entry = identified[i - 1] if i - 1 < len(identified) else None
        if not entry:
            lines.append("      (not identified)")
            continue

        if len(entry) == 1 and _is_error(entry[0]):
            lines.append(f"      ** VISION FAILED: {entry[0]['error']}")
            lines.append("         Nothing is written for this group; its photos go to "
                         "the failed folder.")
            continue

        real = [b for b in entry if not _is_error(b)]
        if len(real) > 1:
            lines.append(f"      ** SPLIT: time-clustering merged {len(real)} DISTINCT "
                         "bottles into this group.")
            lines.append("         Each is written as its own row. Check the names below "
                         "before continuing.")
        for b in real:
            keys = ", ".join(b.get("photo_keys") or [])
            where = f"   [{keys}]" if keys else "   [NO photos assigned]"
            lines.append(f"      -> {bottle_label(b)}{where}")

        assigned = {i2 for b in real for i2 in (b.get("photo_indices") or [])}
        missing = [p["key"] for n, p in enumerate(g) if n not in assigned]
        if missing:
            lines.append(f"      ** {len(missing)} photo(s) not assigned to any bottle: "
                         f"{', '.join(missing)}")
            lines.append("         Nothing is written for them, so they go to the "
                         "FAILED folder, not processed.")
    return "\n".join(lines)


def partition_group(group, bottles):
    """Split a group's photos by whether vision assigned them to a bottle.

    Returns (assigned, unassigned). A photo is unassigned when no bottle claimed
    it — a misread, or a bottle the model could not place. Nothing is written for
    those, so the caller must file them as FAILED, never processed. If the whole
    group failed identification (including the over-limit hard stop), every
    photo is unassigned.
    """
    if not bottles or (len(bottles) == 1 and _is_error(bottles[0])):
        return [], list(group)
    claimed = {i for b in bottles if not _is_error(b)
               for i in (b.get("photo_indices") or [])}
    assigned = [p for n, p in enumerate(group) if n in claimed]
    unassigned = [p for n, p in enumerate(group) if n not in claimed]
    return assigned, unassigned


def confirm_groups(groups, identified=None, yes=False):
    """Show the grouping -- and what vision read -- then ask before any DB write.

    Called AFTER identification so a misread or a bad split can be caught before
    anything is written. Pass yes=True (or --yes on the CLI) to skip the prompt.
    """
    print("\nProposed grouping — photos clustered by time, then separated by what "
          "vision read from the labels.")
    print("[exif] vs [file] shows the time source.")
    print(format_groups(groups, identified))

    if identified is None:
        summary = f"{len(groups)} group(s)"
    else:
        writable = [b for e in identified for b in e if not _is_error(b)]
        splits = sum(1 for e in identified
                     if len([b for b in e if not _is_error(b)]) > 1)
        failed = sum(1 for e in identified
                     if len(e) == 1 and _is_error(e[0]))
        dups = sum(1 for b in writable if b.get("duplicate_of"))
        summary = (f"{len(writable) - dups} new bottle(s) from {len(groups)} "
                   f"photo group(s)")
        if dups:
            summary += f"; {dups} duplicate(s) will be skipped"
        if splits:
            summary += f"; {splits} group(s) SPLIT by vision"
        if failed:
            summary += f"; {failed} group(s) failed identification"

    if yes:
        print(f"\n--yes flag set: proceeding with {summary}.")
        return True
    ans = input(f"\nProceed to write {summary}? [y/N]: ").strip().lower()
    return ans in ("y", "yes")


# --------------------------------------------------------------------------- #
# Vision call (STEP 3 — all of a bottle's photos in ONE request)
# --------------------------------------------------------------------------- #

def normalize_image(image_bytes, mime_type):
    """Decode, downscale to MAX_IMAGE_EDGE_PX on the long edge, re-encode as JPEG.

    This runs on EVERY image, including ones already in a Claude-supported
    format. A full-resolution iPhone photo is several thousand pixels wide;
    Claude downsizes anything over MAX_IMAGE_EDGE_PX before looking at it, so
    those extra pixels only inflate the request. Normalizing here is what makes
    a 20-photo group fit in one call.

    mime_type is still used to pick the decoder (pillow_heif is registered for
    HEIC); the output is always JPEG.
    """
    img = Image.open(io.BytesIO(image_bytes))
    # Bake the EXIF Orientation into the pixels BEFORE re-encoding. A phone held
    # portrait stores landscape pixels plus Orientation=6; re-encoding drops the
    # tag, so without this the bottle is sent lying on its side, and small
    # label text (the proof decimal, dump codes) is what suffers. No-op when
    # there is no orientation tag.
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    # thumbnail() only ever shrinks, so an already-small photo keeps its size
    # and is never upscaled into fake detail.
    img.thumbnail((MAX_IMAGE_EDGE_PX, MAX_IMAGE_EDGE_PX), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"


def _coerce_photo_indices(raw_numbers, n_sent):
    """Model's 1-based photo_numbers -> sorted, deduped, in-range 0-based indices.

    Anything unparseable or out of range is dropped rather than clamped: a bad
    index means the model is confused about that photo, and silently attaching
    it to a bottle is the exact failure this split is meant to prevent.
    """
    # The model sometimes returns the list as a string ("[1, 12]"). Iterating a
    # string yields characters, so "12" became photos 1 and 2 — the wrong photo
    # attached to the wrong bottle, silently. Parse it as JSON first, then fall
    # back to pulling out the integers.
    if isinstance(raw_numbers, str):
        try:
            parsed = json.loads(raw_numbers)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, str):
            parsed = None            # '"7"' decodes to a string; scan it instead
        raw_numbers = parsed if parsed is not None else re.findall(r"\d+", raw_numbers)
    if isinstance(raw_numbers, (int, float)):
        raw_numbers = [raw_numbers]
    if not isinstance(raw_numbers, (list, tuple)):
        return []

    out = []
    for v in raw_numbers:
        try:
            i = int(float(str(v).strip())) - 1   # tolerate 3.0 / "3.0"
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_sent and i not in out:
            out.append(i)
    return sorted(out)


def _parse_bottles(payload, sent):
    """Normalize a vision response into a list of bottle dicts.

    Accepts the documented {"bottles": [...]} shape and also tolerates a bare
    list or a single bottle object, so a model that regresses to the old
    one-bottle shape still works. Each returned dict carries:
        photo_indices  0-based indices into `sent`
        photo_keys     the matching display names, for the confirmation output
    """
    if isinstance(payload, dict) and "error" in payload:
        return [{"error": payload.get("error") or "could not identify"}]

    if isinstance(payload, dict) and isinstance(payload.get("bottles"), list):
        raw = payload["bottles"]
    elif isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict) and payload.get("name"):
        raw = [payload]          # old single-bottle shape
    else:
        return [{"error": "unrecognized response shape",
                 "raw": json.dumps(payload)[:400]}]

    raw = [b for b in raw if isinstance(b, dict)]
    if not raw:
        return [{"error": "no bottles in response"}]

    bottles = []
    for b in raw:
        b = dict(b)
        idx = _coerce_photo_indices(b.pop("photo_numbers", None), len(sent))
        b["photo_indices"] = idx
        b["photo_keys"] = [sent[i].get("key", f"photo {i + 1}") for i in idx]
        bottles.append(b)

    # Only safe when there is exactly one bottle: give it every photo sent. With
    # two or more, an empty list is left empty and surfaced as unassigned.
    if len(bottles) == 1 and not bottles[0]["photo_indices"]:
        bottles[0]["photo_indices"] = list(range(len(sent)))
        bottles[0]["photo_keys"] = [p.get("key", "") for p in sent]

    return bottles


def identify_bottle(group):
    """Identify every distinct bottle in one time-clustered group of photos.

    `group` is a list of photo dicts (as cluster_photos returns), each with
    'bytes' and 'mime'. All photos go in a single vision call so the model can
    both combine detail across shots of one bottle AND separate two bottles that
    time-clustering merged.

    Returns a LIST of bottle dicts. Each carries the identified label fields
    plus photo_indices / photo_keys naming the photos it was read from. On
    failure, returns a single-item list whose only entry has an 'error' key.
    """
    # Safety net only: cluster_photos sub-chunks oversize groups, so a group
    # this big means a caller bypassed it. Still never truncate — dropping
    # photos silently loses whole bottles.
    if len(group) > MAX_IMAGES_PER_CALL:
        names = ", ".join(p.get("key", "?") for p in group)
        return [{"error": (
            f"{len(group)} photos in this group, over the {MAX_IMAGES_PER_CALL}-photo "
            f"limit for a single vision call. Nothing was sent or identified for it — "
            f"no photos were dropped. Run the group through cluster_photos, which "
            f"splits oversize groups: {names}")}]

    sent = group

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
    for n, p in enumerate(sent, 1):
        norm_bytes, norm_mime = normalize_image(p["bytes"], p["mime"])
        # Label each image inline: the model assigns photo_numbers far more
        # reliably when the number travels with the image than when it has to
        # infer position from ordering alone.
        content.append({"type": "text", "text": f"Photo {n}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": norm_mime,
                "data": base64.standard_b64encode(norm_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": VISION_PROMPT})

    # One group's failure (rate limit, network blip, server error) must not
    # abort the whole run: 29 good groups should not be lost to the 30th. The
    # group is reported as failed and its photos go to the failed folder.
    try:
        message = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # noqa: BLE001 — any API/transport failure is a failed group
        return [{"error": f"vision call failed: {type(e).__name__}: {e}"}]

    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [{"error": "invalid response", "raw": text}]
    return _parse_bottles(payload, sent)


def print_result(result):
    """Print the identified fields (shared by both shells)."""
    print(f"  Name:       {result.get('name')}")
    print(f"  Distillery: {result.get('distillery')}")
    print(f"  Proof:      {result.get('proof')}")
    print(f"  Batch:      {result.get('batch')}")
    print(f"  Year:       {result.get('year')}")
    print(f"  Code:       {compose_bottle_code(result) or ''}")
    print(f"  MSRP:       {result.get('msrp')}")
    print(f"  Category:   {db.normalize_category(result.get('category'))}")

    vol = parse_volume_ml(result.get("volume"))
    if vol is None:
        print(f"  Volume:     not legible -- defaulting to {STANDARD_VOLUME_ML}ml")
    elif vol != STANDARD_VOLUME_ML:
        print(f"  Volume:     {format_volume(vol)}  <-- LARGE FORMAT, not {STANDARD_VOLUME_ML}ml")
        print("              Market comparisons assume 750ml; check pricing for this one.")
    else:
        print(f"  Volume:     {format_volume(vol)}")

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


# Hang-tag fields fold into bottle_code as "dumped X | barrel Y | rick Z | wh W |
# bottle N" until they have their own columns. Order is fixed so two reads of the
# same tag compose identically (the duplicate check compares this string).
_TAG_FIELDS = (("dump_date", "dumped"), ("barrel_no", "barrel"), ("rick_no", "rick"),
               ("warehouse", "wh"), ("bottle_no", "bottle"))


def compose_bottle_code(bottle_data):
    """Free-form bottle_code first, then any hang-tag fields, joined by ' | '."""
    parts = []
    code = _clean(bottle_data.get("bottle_code"))
    if code:
        parts.append(code)
    for key, label in _TAG_FIELDS:
        v = _clean(bottle_data.get(key))
        if v:
            parts.append(f"{label} {v}")
    return " | ".join(parts)


def _identity(bottle_data):
    """The five fields that make two intake results 'the same bottle', normalized
    exactly as add_bottle stores them, so the pre-write duplicate check and the
    stored row agree."""
    return {
        "name":        (bottle_data.get("name") or "").strip(),
        "proof":       _clean(bottle_data.get("proof")),
        "batch":       _clean(bottle_data.get("batch")),
        "bottle_code": compose_bottle_code(bottle_data),
        "volume_ml":   parse_volume_ml(bottle_data.get("volume")),
    }


def annotate_duplicates(identified, user_id):
    """Set duplicate_of on every identified bottle that add_bottle would skip.

    Called after identification and before confirm_groups, so the confirm
    screen shows what WILL happen. Two sources: a matching active bottle
    already in the DB, or an identical bottle earlier in this same run (which
    add_bottle will find in the DB by the time it reaches the later one).
    Mutates the bottle dicts in place; returns the number flagged.
    """
    seen_this_run = {}
    flagged = 0
    for entry in identified:
        for b in entry:
            if _is_error(b) or not (b.get("name") or "").strip():
                continue
            ident = _identity(b)
            key = tuple((ident[k] or "").lower() if k != "volume_ml" else
                        db.normalize_volume_ml(ident[k]) for k in
                        ("name", "proof", "batch", "bottle_code", "volume_ml"))
            dup = db.find_duplicate(user_id, **ident)
            if dup is None and key in seen_this_run:
                dup = f"{seen_this_run[key]} (earlier in this run)"
            if dup:
                b["duplicate_of"] = dup
                flagged += 1
            else:
                seen_this_run.setdefault(key, ident["name"])
    return flagged


def add_bottle(bottle_data, user_id):
    """Insert one identified bottle into the DB, owned by user_id (atomic).

    Returns (bottle_id, status) where status is 'added', 'duplicate' (an
    active bottle with the same name, proof, batch, bottle_code and volume_ml
    already belongs to this user -- nothing written, its id is returned), or
    'no_name'.
    Slug collisions are resolved by _resolve_slug against the DB's existing ids
    (bottle_id is a GLOBAL primary key, so collisions are checked across all users)
    — a discriminator (proof, batch, or UUID fragment) is appended only when the
    base name slug already exists, so unique bottles keep clean ids.
    """
    name = (bottle_data.get("name") or "").strip()
    if not name:
        return None, "no_name"

    ident = _identity(bottle_data)
    dup = db.find_duplicate(user_id, **ident)
    if dup:
        return dup, "duplicate"

    existing_ids = db.get_bottle_ids()

    proof = ident["proof"]
    batch = ident["batch"]
    volume_ml = ident["volume_ml"]
    bottle_id = _resolve_slug(slugify(name), proof, batch, existing_ids)

    db.insert_bottle(
        bottle_id=bottle_id,
        user_id=user_id,
        product_key=make_product_key(name, proof, batch, volume_ml),
        name=name,
        proof=proof,
        batch=batch,
        bottle_code=ident["bottle_code"],
        msrp=_clean(bottle_data.get("msrp")),
        status="active",
        category=_clean(bottle_data.get("category")),
        volume_ml=volume_ml,
    )
    return bottle_id, "added"
