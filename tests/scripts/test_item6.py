import io, os, sys, json, shutil, contextlib
from pathlib import Path
from PIL import Image
import intake_core as core, db
F=[]
def ck(n, ok, d=""):
    F.append(n) if not ok else None; print(f"  {'ok  ' if ok else 'FAIL'} {n}" + (f"  {d}" if d and not ok else ""))

db.init_db()
u = db.create_user("a@x.com", "h", is_admin=True); uid = u["id"] if isinstance(u, dict) else u
u2 = db.create_user("b@x.com", "h"); uid2 = u2["id"] if isinstance(u2, dict) else u2

print("=== find_duplicate ===")
db.insert_bottle(bottle_id="eagle", name="Eagle Rare 10", user_id=uid, proof="90", volume_ml=750)
ck("exact match -> id", db.find_duplicate(uid, "Eagle Rare 10", "90", "", "", 750) == "eagle")
ck("case/space-insensitive", db.find_duplicate(uid, "  eagle rare 10 ", "90", None, None, "750") == "eagle")
ck("different proof -> None", db.find_duplicate(uid, "Eagle Rare 10", "101", "", "", 750) is None)
ck("different volume -> None", db.find_duplicate(uid, "Eagle Rare 10", "90", "", "", 1750) is None)
ck("other user -> None", db.find_duplicate(uid2, "Eagle Rare 10", "90", "", "", 750) is None)
db.set_status("eagle", "consumed", uid)
ck("consumed bottle no longer matches", db.find_duplicate(uid, "Eagle Rare 10", "90", "", "", 750) is None)

print("\n=== add_bottle dedup ===")
r1 = core.add_bottle({"name": "Weller 12", "proof": "90", "volume": "750ml"}, uid)
r2 = core.add_bottle({"name": "Weller 12", "proof": "90", "volume": "750ml"}, uid)
ck("first -> added", r1[1] == "added")
ck("second -> duplicate with first's id (branch no longer dead)", r2 == (r1[0], "duplicate"), str(r2))
ck("row count unchanged", sum(1 for b in db.get_all_bottles(uid) if b["name"] == "Weller 12") == 1)

print("\n=== annotate_duplicates + confirm screen ===")
ident = [[{"name": "Weller 12", "proof": "90", "volume": "750ml", "photo_indices": [0], "photo_keys": ["a.jpg"]}],
         [{"name": "Blantons", "proof": "93", "volume": "750ml", "photo_indices": [0], "photo_keys": ["b.jpg"]}],
         [{"name": "Blantons", "proof": "93", "volume": "750ml", "photo_indices": [0], "photo_keys": ["c.jpg"]}],
         [{"error": "x"}]]
n = core.annotate_duplicates(ident, uid)
ck("2 flagged (1 DB, 1 in-run)", n == 2, str(n))
ck("DB dup names the existing id", ident[0][0].get("duplicate_of") == r1[0], str(ident[0][0].get("duplicate_of")))
ck("in-run dup flagged", "earlier in this run" in (ident[2][0].get("duplicate_of") or ""))
ck("first Blantons NOT flagged", "duplicate_of" not in ident[1][0])
lbl = core.bottle_label(ident[0][0])
ck("label shows DUPLICATE", "** DUPLICATE of" in lbl and "will be skipped" in lbl)

print("\n=== identify_bottle survives a failing API call ===")
class Boom:
    class messages:
        @staticmethod
        def create(**kw): raise ConnectionError("simulated 529 overloaded")
core._client = Boom()
_b = io.BytesIO(); Image.new("RGB", (50, 50)).save(_b, "JPEG")
res = core.identify_bottle([{"key": "x.jpg", "bytes": _b.getvalue(), "mime": "image/jpeg"}])
ck("returns error list, no exception", len(res) == 1 and "vision call failed" in res[0].get("error", ""), str(res)[:120])

print("\n=== END-TO-END intake.main(): one bad bottle must not take the group down ===")
import intake
import tempfile
TMP = Path(tempfile.mkdtemp(prefix="bh_item6_"))
TO, PROC, FAIL = TMP/"to", TMP/"proc", TMP/"fail"
for d in (TO, PROC, FAIL): d.mkdir(parents=True)
intake.PHOTOS_TO_PROCESS, intake.PHOTOS_PROCESSED, intake.PHOTOS_FAILED = TO, PROC, FAIL
def seed(names, t0=1764200000):
    for i, n in enumerate(names):
        Image.new("RGB", (300, 400), (i*40 % 255, 80, 30)).save(TO/n, "JPEG"); os.utime(TO/n, (t0+i*20, t0+i*20))
class FakeClient:
    def __init__(s, payload): s.messages = s; s.payload = payload
    def create(s, **kw):
        class M:
            content = [type("B", (), {"type": "text", "text": json.dumps(s.payload)})()]
            stop_reason = "end_turn"
        return M()
def run():
    sys.argv = ["intake.py", "--yes"]; buf = io.StringIO()
    with contextlib.redirect_stdout(buf): intake.main()
    return buf.getvalue()
ls = lambda d: sorted(p.name for p in d.iterdir())

seed(["g1.jpg", "g2.jpg", "g3.jpg"])
core._client = FakeClient({"bottles": [
    {"photo_numbers": [1], "name": "Good One",  "proof": "100", "msrp": "$59.99", "volume": "750ml"},
    {"photo_numbers": [2], "name": "Bad MSRP",  "proof": "100", "msrp": "fifty bucks", "volume": "750ml"},
    {"photo_numbers": [3], "name": "Good Two",  "proof": "107", "msrp": "", "volume": "1L"},
]})
before = len(db.get_all_bottles(uid))
log = run()
print(f"      processed={ls(PROC)} failed={ls(FAIL)}")
ck("run completed (no exception)", True)
ck("2 of 3 rows written", len(db.get_all_bottles(uid)) == before + 2, str(len(db.get_all_bottles(uid)) - before))
ck("good bottles' photos -> processed", ls(PROC) == ["g1.jpg", "g3.jpg"], str(ls(PROC)))
ck("bad bottle's photo -> failed", ls(FAIL) == ["g2.jpg"], str(ls(FAIL)))
ck("log says FAILED to write + continuing", "FAILED to write this bottle" in log and "continuing" in log)
ck("$59.99 parsed (item 5)", any(b["name"]=="Good One" and b["msrp"]=="59.99" for b in db.get_all_bottles(uid)))

print("\n=== re-run the same photos: duplicates skipped, nothing re-written ===")
for n in ("g1.jpg", "g3.jpg"): shutil.move(str(PROC/n), str(TO/n))
before = len(db.get_all_bottles(uid))
core._client = FakeClient({"bottles": [
    {"photo_numbers": [1], "name": "Good One", "proof": "100", "volume": "750ml"},
    {"photo_numbers": [2], "name": "Good Two", "proof": "107", "volume": "1L"},
]})
log2 = run()
ck("0 new rows on re-run", len(db.get_all_bottles(uid)) == before, str(len(db.get_all_bottles(uid)) - before))
ck("confirm screen flagged both as DUPLICATE", log2.count("** DUPLICATE of") == 2, str(log2.count("** DUPLICATE of")))
ck("summary: 0 new, 2 duplicates", "0 new bottle(s)" in log2 and "2 duplicate(s) will be skipped" in log2)
ck("duplicate photos filed as processed", ls(PROC) == ["g1.jpg", "g3.jpg"], str(ls(PROC)))
ck("write log: Already in collection", log2.count("Already in collection") == 2)

print("\nITEM 6:", "ALL PASS" if not F else f"FAILED {F}")
sys.exit(1 if F else 0)
