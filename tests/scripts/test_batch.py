"""Real-JPEG tests for: 20-image cap + downscale, and failed-vs-processed routing."""
import io, json, os, shutil, sys, base64, contextlib
from pathlib import Path
from PIL import Image

import intake_core as core
import db

FAILS = []
def check(name, ok, detail=""):
    if not ok: FAILS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   {detail}" if detail and not ok else ""))

def make_jpeg(path, size, color, quality=95):
    """A real JPEG on disk with real pixel content (gradient, so it won't over-compress)."""
    img = Image.new("RGB", size, color)
    px = img.load()
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 7):
            px[x, y] = ((x * 3) % 256, (y * 5) % 256, color[2])
    img.save(path, format="JPEG", quality=quality)
    return Path(path)

import tempfile
TMP = Path(tempfile.mkdtemp(prefix="bh_batch_"))

print("=== 1. constants ===")
check("MAX_IMAGES_PER_CALL == 20", core.MAX_IMAGES_PER_CALL == 20, str(core.MAX_IMAGES_PER_CALL))
check("MAX_IMAGE_EDGE_PX == 1568", core.MAX_IMAGE_EDGE_PX == 1568, str(core.MAX_IMAGE_EDGE_PX))
check("JPEG_QUALITY == 85", core.JPEG_QUALITY == 85, str(core.JPEG_QUALITY))
check("CLUSTER_WINDOW_SECONDS still 90", core.CLUSTER_WINDOW_SECONDS == 90)
# A truncated response is invalid JSON and fails the whole group, so the token
# ceiling has to cover one entry per photo in the worst case.
check("MAX_TOKENS covers MAX_IMAGES_PER_CALL bottles",
      core.MAX_TOKENS >= core.MAX_IMAGES_PER_CALL * 100,
      f"MAX_TOKENS={core.MAX_TOKENS} vs {core.MAX_IMAGES_PER_CALL} bottles")

print("\n=== 2. normalize_image downscales REAL jpegs (every image, not just HEIC) ===")
big = make_jpeg(TMP / "big.jpg", (4032, 3024), (120, 60, 20))      # iPhone-sized
raw = big.read_bytes()
out, mime = core.normalize_image(raw, "image/jpeg")
w, h = Image.open(io.BytesIO(out)).size
print(f"      4032x3024 {len(raw)/1024:,.0f} KiB  ->  {w}x{h} {len(out)/1024:,.0f} KiB  ({mime})")
check("long edge capped at 1568", max(w, h) == 1568, f"{w}x{h}")
check("aspect ratio preserved", abs((w / h) - (4032 / 3024)) < 0.01, f"{w}x{h}")
check("output is jpeg", mime == "image/jpeg", mime)
check("already-supported jpeg IS downscaled (no early return)", len(out) < len(raw),
      f"{len(out)} vs {len(raw)}")

small = make_jpeg(TMP / "small.jpg", (800, 600), (20, 60, 120))
sout, _ = core.normalize_image(small.read_bytes(), "image/jpeg")
sw, sh = Image.open(io.BytesIO(sout)).size
print(f"      800x600 -> {sw}x{sh}  (must NOT upscale)")
check("small image not upscaled", (sw, sh) == (800, 600), f"{sw}x{sh}")

png = TMP / "shot.png"; Image.new("RGB", (2000, 1500), (90, 90, 90)).save(png)
pout, pmime = core.normalize_image(png.read_bytes(), "image/png")
pw, ph = Image.open(io.BytesIO(pout)).size
check("png converted to jpeg and downscaled", pmime == "image/jpeg" and max(pw, ph) == 1568,
      f"{pmime} {pw}x{ph}")

try:
    heic = TMP / "iphone.heic"
    Image.new("RGB", (4032, 3024), (200, 150, 90)).save(heic, format="HEIF")
    hout, hmime = core.normalize_image(heic.read_bytes(), "image/heif")
    hw, hh = Image.open(io.BytesIO(hout)).size
    check("real HEIC decoded + downscaled", hmime == "image/jpeg" and max(hw, hh) == 1568,
          f"{hmime} {hw}x{hh}")
except Exception as e:
    print(f"  --   HEIC encode unavailable here, skipped ({type(e).__name__})")

print("\n=== 3. a 20-image request stays a sane size ===")
one_b64 = len(base64.standard_b64encode(out))
print(f"      per image ~{one_b64/1024:,.0f} KiB base64; 20 images ~{one_b64*20/1024/1024:,.2f} MiB")
check("20 downscaled images well under the 32MB request ceiling", one_b64 * 20 < 20 * 1024 * 1024,
      f"{one_b64*20/1024/1024:.1f} MiB")

# ---- vision stub -----------------------------------------------------------
class FakeMsg:
    def __init__(s, t):
        s.content = [type("B", (), {"type": "text", "text": t})()]
        s.stop_reason = "end_turn"
class FakeMessages:
    def __init__(s, p): s.payload = p; s.calls = 0
    def create(s, **kw):
        s.calls += 1
        return FakeMsg(s.payload if isinstance(s.payload, str) else json.dumps(s.payload))
class FakeClient:
    def __init__(s, p): s.messages = FakeMessages(p)

def group_of(n, start=0):
    g = []
    for i in range(n):
        p = make_jpeg(TMP / f"g{start+i}.jpg", (300, 400), (i * 7 % 255, 90, 40))
        g.append({"key": f"g{start+i}.jpg", "bytes": p.read_bytes(), "mime": "image/jpeg"})
    return g

print("\n=== 4. over-limit groups HARD-STOP (no truncation, no API call) ===")
core._client = FakeClient({"bottles": [{"photo_numbers": [1], "name": "Should Never Be Used"}]})
res21 = core.identify_bottle(group_of(21))
check("21 photos -> single error entry", len(res21) == 1 and "error" in res21[0], str(res21)[:120])
check("no vision call was made", core._client.messages.calls == 0,
      f"{core._client.messages.calls} calls")
msg = res21[0].get("error", "")
check("error states the count and the limit", "21 photos" in msg and "20-photo" in msg, msg[:150])
check("error says nothing was dropped", "no photos were dropped" in msg, msg[:200])
check("error lists every filename", all(f"g{i}.jpg" in msg for i in range(21)), msg[-120:])
print(f"      {msg[:150]}...")

core._client = FakeClient({"bottles": [{"photo_numbers": list(range(1, 21)), "name": "Twenty Up"}]})
res20 = core.identify_bottle(group_of(20, start=100))
check("exactly 20 photos proceeds", len(res20) == 1 and "error" not in res20[0], str(res20)[:120])
check("20 photos -> one vision call", core._client.messages.calls == 1)
check("all 20 assigned", res20[0]["photo_indices"] == list(range(20)))

print("\n=== 5. partition_group ===")
g3 = group_of(3, start=200)
two = [{"name": "A", "photo_indices": [0]}, {"name": "B", "photo_indices": [1]}]
a, u = core.partition_group(g3, two)
check("assigned = claimed photos", [p["key"] for p in a] == ["g200.jpg", "g201.jpg"], str([p["key"] for p in a]))
check("unassigned = the leftover", [p["key"] for p in u] == ["g202.jpg"], str([p["key"] for p in u]))
a2, u2 = core.partition_group(g3, [{"error": "could not identify"}])
check("whole-group failure -> everything unassigned", len(a2) == 0 and len(u2) == 3)
a3, u3 = core.partition_group(g3, [{"name": "All", "photo_indices": [0, 1, 2]}])
check("fully assigned -> nothing unassigned", len(a3) == 3 and len(u3) == 0)

print("\n=== 6. END-TO-END intake.main(): real files, real DB, routing by assignment ===")
db.init_db()
u_ = db.create_user("batch@example.com", "h", is_admin=True)
uid = u_["id"] if isinstance(u_, dict) else u_

import intake
TO, PROC, FAIL = TMP / "to", TMP / "proc", TMP / "fail"
intake.PHOTOS_TO_PROCESS, intake.PHOTOS_PROCESSED, intake.PHOTOS_FAILED = TO, PROC, FAIL

def reset_dirs():
    for d in (TO, PROC, FAIL):
        shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)

def seed(names, base_mtime=1764130000):
    for i, n in enumerate(names):
        p = make_jpeg(TO / n, (400, 300), (i * 11 % 255, 70, 30))
        os.utime(p, (base_mtime + i * 20, base_mtime + i * 20))   # 20s apart -> one group

def run_main():
    sys.argv = ["intake.py", "--yes"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        intake.main()
    return buf.getvalue()

def listing(d):
    return sorted(p.name for p in d.iterdir())

# -- 6a: 3 photos, vision assigns only 2 -> 1 unassigned must land in FAILED --
reset_dirs(); seed(["a1.jpg", "a2.jpg", "a3.jpg"])
core._client = FakeClient({"bottles": [
    {"photo_numbers": [1], "name": "Assigned Bottle One", "proof": "90", "volume": "750ml"},
    {"photo_numbers": [2], "name": "Assigned Bottle Two", "proof": "100", "volume": "750ml"},
]})
log = run_main()
print(f"      processed={listing(PROC)}  failed={listing(FAIL)}")
check("assigned photos -> processed", listing(PROC) == ["a1.jpg", "a2.jpg"], str(listing(PROC)))
check("UNASSIGNED photo -> failed (not processed)", listing(FAIL) == ["a3.jpg"], str(listing(FAIL)))
check("intake dir drained", listing(TO) == [], str(listing(TO)))
check("log names the unassigned file", "a3.jpg" in log and "NOT assigned" in log)
check("2 rows written", len(db.get_all_bottles(uid)) == 2, str(len(db.get_all_bottles(uid))))

# -- 6b: all assigned -> nothing in failed --
reset_dirs(); seed(["b1.jpg", "b2.jpg"])
core._client = FakeClient({"bottles": [
    {"photo_numbers": [1, 2], "name": "Fully Assigned", "proof": "95", "volume": "750ml"}]})
run_main()
print(f"      processed={listing(PROC)}  failed={listing(FAIL)}")
check("all assigned -> both processed", listing(PROC) == ["b1.jpg", "b2.jpg"], str(listing(PROC)))
check("failed folder empty", listing(FAIL) == [], str(listing(FAIL)))

# -- 6c: over-limit group -> ALL photos to failed, nothing written --
reset_dirs()
names21 = [f"c{i:02d}.jpg" for i in range(21)]
seed(names21, base_mtime=1764140000)
before = len(db.get_all_bottles(uid))
core._client = FakeClient({"bottles": [{"photo_numbers": [1], "name": "Never Written"}]})
log = run_main()
print(f"      processed={len(listing(PROC))} failed={len(listing(FAIL))}")
check("21 photos chunked into 2 vision calls (not hard-stopped)", core._client.messages.calls == 2,
      f"{core._client.messages.calls} calls")
check("no hard-stop in log", "over the 20-photo limit" not in log)
check("size-split note shown", "size-split: part 1/2 of a 21-photo" in log, log[-400:])
check("intake dir drained", listing(TO) == [], str(listing(TO)))

print("\n" + "=" * 62)
print("ALL CHECKS PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
