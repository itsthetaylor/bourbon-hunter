"""Fake group: 2 different bottles + 1 back-label shot of one of them."""
import io, json, sys
from datetime import datetime, timedelta
from PIL import Image

import intake_core as core
import db

FAILS = []
def check(name, ok, detail=""):
    FAILS.append(name) if not ok else None
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   {detail}" if detail and not ok else ""))

def jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (48, 64), color).save(buf, format="JPEG")  # no EXIF on purpose
    return buf.getvalue()

BASE = datetime(2026, 11, 26, 3, 10, 0)
def photo(key, color, offset_s):
    return {"key": key, "bytes": jpeg(color), "mime": "image/jpeg",
            "fallback_time": BASE + timedelta(seconds=offset_s)}

# Photo 1 = Eagle Rare front, Photo 2 = Weller front, Photo 3 = Eagle Rare BACK label.
# All inside the 180s window, so time-clustering merges them into ONE group --
# which is exactly the bug being fixed.
photos = [
    photo("IMG_01.jpg", (120, 60, 20), 0),
    photo("IMG_02.jpg", (20, 60, 120), 30),
    photo("IMG_03.jpg", (120, 60, 21), 60),
]

print("=== 1. time clustering still merges all three (unchanged, window untouched) ===")
groups, problem = core.cluster_photos(photos)
check("no clustering problem", problem is None, str(problem))
check("CLUSTER_WINDOW_SECONDS unchanged at 90", core.CLUSTER_WINDOW_SECONDS == 90,
      f"got {core.CLUSTER_WINDOW_SECONDS}")
check("exactly ONE time group of 3 photos", len(groups) == 1 and len(groups[0]) == 3,
      f"{[len(g) for g in groups]}")
group = groups[0]

# ---- stub the vision client; everything else is the real code path ----
class FakeMsg:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
class FakeMessages:
    def __init__(self, payload): self.payload = payload; self.seen = None
    def create(self, **kw):
        self.seen = kw
        return FakeMsg(self.payload if isinstance(self.payload, str) else json.dumps(self.payload))
class FakeClient:
    def __init__(self, payload): self.messages = FakeMessages(payload)

def run_vision(payload, g=None):
    core._client = FakeClient(payload)
    out = core.identify_bottle(g if g is not None else group)
    return out, core._client.messages.seen

SPLIT_RESPONSE = {"bottles": [
    {"photo_numbers": [1, 3], "name": "Eagle Rare 10 Year", "distillery": "Buffalo Trace",
     "proof": "90", "batch": "", "year": "", "bottle_code": "L2208",
     "msrp": "39.99", "category": "bourbon", "volume": "750ml",
     "confidence": "high", "notes": ""},
    {"photo_numbers": [2], "name": "Weller Antique 107", "distillery": "Buffalo Trace",
     "proof": "107.0", "batch": "C920", "year": "", "bottle_code": "",
     "msrp": "49.99", "category": "bourbon", "volume": "1.75L",
     "confidence": "high", "notes": ""},
]}

print("\n=== 2. identify_bottle returns a LIST, split into 2 bottles ===")
bottles, sent_kw = run_vision(SPLIT_RESPONSE)
check("returns a list", isinstance(bottles, list), type(bottles).__name__)
check("two bottles", len(bottles) == 2, f"got {len(bottles)}")
a, b = bottles
check("bottle A = Eagle Rare", a["name"] == "Eagle Rare 10 Year", a.get("name"))
check("bottle A owns photos 1+3 (indices 0,2)", a["photo_indices"] == [0, 2], str(a["photo_indices"]))
check("bottle A keys are the front + BACK shot",
      a["photo_keys"] == ["IMG_01.jpg", "IMG_03.jpg"], str(a["photo_keys"]))
check("bottle B = Weller", b["name"] == "Weller Antique 107", b.get("name"))
check("bottle B owns photo 2 only", b["photo_indices"] == [1], str(b["photo_indices"]))
check("back-label shot did NOT become its own bottle", len(bottles) == 2)

print("\n=== 3. request carries inline 'Photo N:' labels before each image ===")
content = sent_kw["messages"][0]["content"]
labels = [c["text"] for c in content if c["type"] == "text" and c["text"].startswith("Photo ")]
images = [c for c in content if c["type"] == "image"]
check("3 images sent", len(images) == 3, str(len(images)))
check("labels are Photo 1..3 in order", labels == ["Photo 1:", "Photo 2:", "Photo 3:"], str(labels))
check("each label precedes its image",
      all(content[i*2]["type"] == "text" and content[i*2+1]["type"] == "image" for i in range(3)))

print("\n=== 4. confirm output shows NAMES per group and flags the split ===")
out = core.format_groups(groups, [bottles])
print("\n".join("      | " + l for l in out.splitlines()))
check("shows bottle A name", "Eagle Rare 10 Year" in out)
check("shows bottle B name", "Weller Antique 107" in out)
check("flags the SPLIT", "** SPLIT" in out)
check("says how many distinct bottles", "2 DISTINCT" in out)
check("still shows filenames", "IMG_01.jpg" in out and "IMG_02.jpg" in out)
check("shows which photos each bottle came from", "[IMG_01.jpg, IMG_03.jpg]" in out)
check("shows the 1.75L large format", "1.75L" in out)
check("shows the batch", "Batch C920" in out)
check("no unassigned warning when all assigned", "not assigned to any bottle" not in out)

print("\n=== 5. a single-bottle group is NOT flagged as split ===")
single = {"bottles": [dict(SPLIT_RESPONSE["bottles"][0], photo_numbers=[1, 2, 3])]}
one, _ = run_vision(single)
out1 = core.format_groups(groups, [one])
check("one bottle returned", len(one) == 1)
check("no SPLIT flag", "** SPLIT" not in out1)
check("no unassigned warning", "not assigned" not in out1)

print("\n=== 6. robustness ===")
noidx, _ = run_vision({"bottles": [{"name": "Lone Bottle"}]})
check("single bottle w/o photo_numbers gets all photos",
      noidx[0]["photo_indices"] == [0, 1, 2], str(noidx[0]["photo_indices"]))

old, _ = run_vision({"name": "Old Shape Bottle", "proof": "100"})
check("old single-object shape still parses", len(old) == 1 and old[0]["name"] == "Old Shape Bottle")

err, _ = run_vision({"error": "could not identify"})
check("error -> single-item list with 'error'",
      len(err) == 1 and err[0].get("error") == "could not identify")

bad, _ = run_vision("not json at all")
check("invalid JSON -> error list", len(bad) == 1 and "error" in bad[0], str(bad))

oob, _ = run_vision({"bottles": [
    {"photo_numbers": [1, 99, "x", None], "name": "A"},
    {"photo_numbers": [2], "name": "B"}]})
check("out-of-range / junk indices dropped", oob[0]["photo_indices"] == [0], str(oob[0]["photo_indices"]))
out_oob = core.format_groups(groups, [oob])
check("unassigned photo is reported", "not assigned to any bottle" in out_oob and "IMG_03.jpg" in out_oob)

empty, _ = run_vision({"bottles": [{"photo_numbers": [], "name": "A"}, {"photo_numbers": [2], "name": "B"}]})
check("multi-bottle: empty index list left empty (not back-filled)",
      empty[0]["photo_indices"] == [], str(empty[0]["photo_indices"]))
out_e = core.format_groups(groups, [empty])
check("'NO photos assigned' surfaced", "NO photos assigned" in out_e)

print("\n=== 7. confirm_groups(yes=True) summary line ===")
import contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ok = core.confirm_groups(groups, [bottles], yes=True)
summary = buf.getvalue()
print("\n".join("      | " + l for l in summary.strip().splitlines()[-1:]))
check("auto-confirms with --yes", ok is True)
check("summary counts 2 bottles from 1 group", "2 new bottle(s) from 1 photo group(s)" in summary, summary)
check("summary flags the split", "1 group(s) SPLIT" in summary, summary)

print("\n=== 8. end-to-end: one group -> TWO separate DB rows ===")
db.init_db()
u = db.create_user("split@example.com", "h")
uid = u["id"] if isinstance(u, dict) else u
written = []
for bt in bottles:
    bid, status = core.add_bottle(bt, uid)
    written.append((bid, status))
    print(f"      wrote {bid!r} ({status})")
rows = db.get_all_bottles(uid)
check("2 rows written from 1 photo group", len(rows) == 2, f"got {len(rows)}")
check("both status 'added'", all(s == "added" for _, s in written), str(written))
check("distinct bottle_ids", len({r["bottle_id"] for r in rows}) == 2)
check("distinct product_keys (different sizes)", len({r["product_key"] for r in rows}) == 2,
      str([r["product_key"] for r in rows]))
for r in rows:
    print(f"      seq={r['seq']} {r['bottle_id']:28} {r['name']:20} "
          f"{r['proof']:6} {r['volume_ml']}ml  key={r['product_key']}")
names = {r["name"] for r in rows}
check("both bottles present by name", names == {"Eagle Rare 10 Year", "Weller Antique 107"}, str(names))
check("Weller stored as 1750ml",
      next(r for r in rows if "Weller" in r["name"])["volume_ml"] == 1750)
check("Eagle Rare stored as 750ml",
      next(r for r in rows if "Eagle" in r["name"])["volume_ml"] == 750)

print("\n" + "=" * 60)
print("ALL CHECKS PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
