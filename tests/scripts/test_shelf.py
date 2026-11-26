"""110 real JPEGs / 30 bottles shot 5-15s apart, through the REAL intake.main().
Vision is a stub that behaves like a model obeying the prompt: one entry per
distinct bottle present in the chunk, packaging and obscured labels unassigned."""
import io, os, sys, json, random, shutil, contextlib
from pathlib import Path
from PIL import Image
import intake_core as core, db, intake

random.seed(2026)
F=[]
def ck(n, ok, d=""):
    F.append(n) if not ok else None; print(f"  {'ok  ' if ok else 'FAIL'} {n}" + (f"   {d}" if d and not ok else ""))

# ---- the shelf: 30 bottles -------------------------------------------------
def B(name, proof, **kw): return dict(name=name, proof=proof, volume=kw.pop("volume","750ml"), category=kw.pop("category","bourbon"), **kw)
SHELF = [
 B("Eagle Rare 10 Year","90"), B("Buffalo Trace","90"), B("Blanton's Single Barrel","93",
   dump_date="12-13-25", barrel_no="218", rick_no="9", warehouse="H", bottle_no="014"),
 B("Weller Special Reserve","90"),
 B("E.H. Taylor Barrel Proof","127.4", batch="Batch 13"),          # 4  <- two EHT BPs, different proofs
 B("E.H. Taylor Barrel Proof","127.3", batch="Batch 12"),          # 5
 B("Stagg Jr","130.2", batch="Batch 18"), B("Old Forester 1920","115"), B("Four Roses Single Barrel","100"),
 B("Elijah Craig Barrel Proof","124.8", batch="C923"), B("Knob Creek 12","100"),
 B("Weller 12 Year","90", volume="1L"),                            # 11 <- Weller 12 1L
 B("Weller 12 Year","90", volume="750ml"),                         # 12 <- Weller 12 750ml
 B("Russell's Reserve 13","114.8"), B("Wild Turkey Rare Breed","116.8"), B("Booker's","125.5", batch="2025-01"),
 B("Michter's 10","94.4"), B("Henry McKenna 10","100"), B("1792 Full Proof","125"),
 B("Blanton's Gold","103.0", category="bourbon", notes="proof derived from ABV", msrp="$149.99"),  # 19 Japanese export, ABV label
 B("Macallan 12","86", category="Scotch"),                         # 20 <- bad category value
 B("Old Fitzgerald 11","100", msrp="$199.99"), B("Larceny Barrel Proof","122.6", batch="A125"),
 B("Maker's Mark Cask Strength","111.2"), B("Woodford Double Oaked","90.4"),
 B("William Larue Weller","125.3"),                                # 25 <- TISSUE-WRAPPED: unreadable
 B("George T. Stagg","138.7"), B("Sazerac 18","90"), B("Thomas H. Handy","127.2"), B("Eagle Rare 17","101"),
]
assert len(SHELF) == 30
TISSUE = 25          # label obscured -> stub returns nothing for these photos
BOX_BOTTLE = 2       # Blanton's gets an extra photo that is just its box

# ---- 110 photos: 3-4 per bottle, one extra box shot -----------------------
import tempfile
TMP = Path(tempfile.mkdtemp(prefix="bh_shelf_"))
TO, PROC, FAIL = TMP/"to", TMP/"proc", TMP/"fail"
for d in (TO, PROC, FAIL): d.mkdir(parents=True)
intake.PHOTOS_TO_PROCESS, intake.PHOTOS_PROCESSED, intake.PHOTOS_FAILED = TO, PROC, FAIL

truth = {}          # filename -> (bottle_idx | "box")
t = 1764300000; n = 0
per = [4,3,4,3,4,4,3,4,4,3,4,4,3,4,4,3,4,4,3,4,4,3,4,4,3,3,4,4,3,4]   # sums to 109
assert sum(per) == 109
for bi, k in enumerate(per):
    shots = ["front","back","bottom","tag"][:k]
    if bi == BOX_BOTTLE: shots.append("box")   # the 110th photo
    for shot in shots:
        n += 1; fn = f"IMG_{8000+n:04}.JPG"
        img = Image.new("RGB", (480, 640), (bi*8 % 255, 90, 40))
        # one unique pixel per photo: real shots are never byte-identical, and
        # the content dedup would otherwise (correctly) fold a bottle's photos
        # into one. Only the deliberately copied re-upload below stays identical.
        img.putpixel((n % 480, (n * 7) % 640), (255, 255, 255))
        img.save(TO/fn, "JPEG")
        os.utime(TO/fn, (t, t)); truth[fn] = "box" if shot == "box" else bi
        t += random.randint(5, 15)
assert len(truth) == 110, len(truth)
# a retried upload: byte-identical copy of one shot under a new filename
import shutil as _sh; _sh.copy(TO/"IMG_8050.JPG", TO/"IMG_8111.JPG"); os.utime(TO/"IMG_8111.JPG", (t+60, t+60))
truth["IMG_8111.JPG"] = truth["IMG_8050.JPG"]
print(f"shelf: {len(truth)} photos (110 + 1 re-upload), 30 bottles, span {(t-1764300000)/60:.1f} min, gaps 5-15s")

# ---- stub vision: obeys the prompt -----------------------------------------
calls = []
class Stub:
    def __init__(s): s.messages = s
    def create(s, **kw):
        content = kw["messages"][0]["content"]
        # recover which photos are in this call from the "Photo N:" labels + order
        keys = [p["key"] for p in current_group]
        calls.append(len(keys))
        bybottle = {}
        for i, key in enumerate(keys, 1):
            who = truth[key]
            if who == "box" or who == TISSUE: continue          # unassigned, per prompt
            bybottle.setdefault(who, []).append(i)
        bottles = [dict(SHELF[bi], photo_numbers=nums, confidence="high") for bi, nums in sorted(bybottle.items())]
        payload = {"bottles": bottles} if bottles else {"error": "could not identify"}
        class M:
            content = [type("T", (), {"type": "thinking", "thinking": ""})(),
                       type("T", (), {"type": "text", "text": json.dumps(payload)})()]
            stop_reason = "end_turn"
        return M()
current_group = None
_orig = core.identify_bottle
def tracking_identify(group):
    global current_group; current_group = group; return _orig(group)
core.identify_bottle = tracking_identify; intake.core.identify_bottle = tracking_identify
core._client = Stub()

db.init_db(); u = db.create_user("shelf@x.com","h",is_admin=True); uid = u["id"] if isinstance(u,dict) else u

sys.argv = ["intake.py", "--yes"]; buf = io.StringIO()
with contextlib.redirect_stdout(buf): intake.main()
log = buf.getvalue()

# ---- the confirm screen, verbatim ------------------------------------------
start = log.index("Proposed grouping"); end = log.index("--yes flag set")
screen = log[start:log.index("\n", end)]
print("\n" + "="*78 + "\nCONFIRM SCREEN (verbatim)\n" + "="*78)
print(screen)
print("="*78)

# ---- assertions ------------------------------------------------------------
rows = db.get_all_bottles(uid); names = [r["name"] for r in rows]
ls = lambda d: sorted(p.name for p in d.iterdir())
print(f"\nvision calls: {len(calls)} chunks, sizes {calls}")
print(f"rows written: {len(rows)}   processed: {len(ls(PROC))}   failed: {len(ls(FAIL))}")
ck("run completed without exception", True)
ck("every chunk <= 20 photos", all(c <= 20 for c in calls), str(calls))
ck("intake folder drained (all 111 moved)", ls(TO) == [])
ck("re-upload detected: 'duplicate upload of IMG_8050.JPG'", "duplicate upload of IMG_8050.JPG" in log)
ck("re-upload copy -> failed", "IMG_8111.JPG" in ls(FAIL))
ck("re-upload count on confirm screen", "1 duplicate upload(s) dropped" in screen)
ck("29 bottles written (30 minus tissue-wrapped WLW)", len(rows) == 29, str(len(rows)))
ck("WLW not written", "William Larue Weller" not in names)
wlw_files = sorted(f for f, w in truth.items() if w == TISSUE); box_file = [f for f, w in truth.items() if w == "box"]
ck("WLW's 3 photos -> failed", all(f in ls(FAIL) for f in wlw_files), str(ls(FAIL)))
ck("Blanton's box photo -> failed", box_file[0] in ls(FAIL), str(box_file))
ck("exactly those 4 + the re-upload in failed", len(ls(FAIL)) == 5, str(ls(FAIL)))
ck("106 photos -> processed", len(ls(PROC)) == 106, str(len(ls(PROC))))
eht = [r for r in rows if r["name"] == "E.H. Taylor Barrel Proof"]
ck("two EHT BPs as two rows", len(eht) == 2 and {r["proof"] for r in eht} == {"127.4","127.3"}, str([(r["proof"],r["batch"]) for r in eht]))
ck("EHT BPs have distinct product_keys", len({r["product_key"] for r in eht}) == 2)
w12 = [r for r in rows if r["name"] == "Weller 12 Year"]
ck("Weller 12 1L and 750ml as two rows", len(w12) == 2 and {r["volume_ml"] for r in w12} == {1000, 750}, str([r["volume_ml"] for r in w12]))
ck("Weller 12 distinct product_keys", len({r["product_key"] for r in w12}) == 2)
bl = next(r for r in rows if r["name"] == "Blanton's Single Barrel")
ck("Blanton's hang tag composed into bottle_code", bl["bottle_code"] == "dumped 12-13-25 | barrel 218 | rick 9 | wh H | bottle 014", bl["bottle_code"])
mac = next(r for r in rows if r["name"] == "Macallan 12")
ck("'Scotch' stored as bourbon (normalized)...", mac["category"] == "bourbon")
ck("...but WARNED loudly on the confirm screen", "CATEGORY 'Scotch' is not one of" in screen)
ck("$149.99 / $199.99 parsed", {r["msrp"] for r in rows if r["name"] in ("Blanton's Gold","Old Fitzgerald 11")} == {"149.99","199.99"})
ck("1L badge-worthy volume shown on screen", "Weller 12 Year  (90 proof, 1L)" in screen)
ck("size-split notes on screen", "size-split: part 1/" in screen)
ck("unassigned photos reported on screen", "not assigned to any bottle" in screen and box_file[0] in screen)
merged = screen.count("** MERGED across chunk boundary"); dups = screen.count("** DUPLICATE of")
print(f"\nbottles straddling a forced chunk cut: {merged} MERGED, {dups} left as duplicates")
ck("straddlers MERGED (>=1), none left as in-run duplicates", merged >= 1 and dups == 0, f"merged={merged} dups={dups}")
ck("summary reports the merges", f"{merged} MERGED across chunk cuts" in screen)
ck("still exactly 29 rows (no double-writes)", len(rows) == 29)
# a merged bottle's photos from BOTH chunks are all processed
bt = [f for f, w in truth.items() if w == 1]   # Buffalo Trace straddled groups 1/2 in the seeded layout
ck("straddled bottle's photos all processed", all(f in ls(PROC) for f in bt), str([f for f in bt if f not in ls(PROC)]))
print("\nSHELF E2E:", "ALL PASS" if not F else f"FAILED {F}")
sys.exit(1 if F else 0)
