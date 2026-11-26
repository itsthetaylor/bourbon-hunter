import ast, io, sys
from jinja2 import Environment

src = io.open("/home/user/bourbon-hunter/flask_app.py", encoding="utf-8").read()
tree = ast.parse(src)

# Exec only the pure-string top-level constants (templates + CSS), so we get the
# real template text without importing flask/db.
ns = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        try:
            code = compile(ast.Module(body=[node], type_ignores=[]), "<tpl>", "exec")
            exec(code, ns)
        except Exception:
            pass  # anything needing imports/calls is not a template constant

tpl_src = ns.get("INDEX_TEMPLATE")
assert tpl_src, "INDEX_TEMPLATE not extracted"
print(f"INDEX_TEMPLATE extracted: {len(tpl_src)} chars")

def url_for(ep, **kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items() if v is not None)
    return f"/{ep}" + (f"?{q}" if q else "")

def volume_label(ml):
    if not ml: return ""
    return f"{ml/1000.0:g}L" if ml >= 1000 else f"{ml}ml"

def bottle(bid, name, vol=750, cat="bourbon", paid=100.0, batch="", code=""):
    return {"bottle_id": bid, "name": name, "volume_ml": vol, "category": cat,
            "paid": paid, "proof": "127.4", "batch": batch, "bottle_code": code,
            "msrp": 69.99, "product_key": name.lower().replace(" ", "_"),
            "date_acquired": "2026-01-05",
            "wooden_cork_url": "", "bbb_url": "", "barrel_tap_url": "", "keg_n_bottle_url": ""}

# A bourbon 750, a 1.75L bourbon, a multi-bottle group mixing 750 + 1L,
# a spirit, and a wine.
b_std   = bottle("eagle_rare", "Eagle Rare 10 Year")
b_large = bottle("weller_175", "Weller Antique 107", vol=1750)
b_grp_a = bottle("blantons_a", "Blantons Original", vol=750, code="L2208")
b_grp_b = bottle("blantons_b", "Blantons Original", vol=1000, code="L2301")
b_spirit= bottle("macallan_12", "Macallan 12", cat="spirit", vol=700)
b_wine  = bottle("opus_one", "Opus One 2019", cat="wine", vol=750)

def sections(rows):
    import itertools
    groups = {}
    for r in rows:
        groups.setdefault(r["product_key"], []).append(r)
    gl = list(groups.values())
    out = []
    for g in gl:
        out.append({"brand": g[0]["name"].split()[0], "groups": [g]})
    return out

cats_all = [
    {"key": "bourbon", "label": "Bourbon & American Whiskey", "count": 4,
     "sections": sections([b_std, b_large]) + [{"brand": "Blantons", "groups": [[b_grp_a, b_grp_b]]}]},
    {"key": "spirit", "label": "Other Spirits", "count": 1, "sections": sections([b_spirit])},
    {"key": "wine", "label": "Wine", "count": 1, "sections": sections([b_wine])},
]
counts = [(c["key"], c["label"], c["count"]) for c in cats_all]

env = Environment()
tpl = env.from_string(tpl_src)

def render(label, **over):
    ctx = dict(cat_sections=cats_all, category_counts=counts, active_category=None,
               total_count=6, show_categories=True, categories=("bourbon","spirit","wine"),
               volume_label=volume_label, standard_volume=750,
               market={"eagle_rare": {"market_value": 310.0, "sources_count": 3}},
               url_labels=[("wooden_cork_url","Wooden Cork")], user_email="x@y.com",
               error=None, authenticated=True, url_for=url_for)
    ctx.update(over)
    html = tpl.render(**ctx)
    print(f"\n--- {label}: rendered {len(html)} chars ---")
    return html

h = render("all categories")
checks = [
    ("1.75L badge present",            'vol-badge">1.75L' in h),
    ("1L badge on grouped unit",       'vol-badge">1L' in h),
    ("no 750ml badge anywhere",        'vol-badge">750ml' not in h),
    ("category headers rendered",      h.count('<div class="cat-header">') == 3),
    ("filter row rendered",            'class="cat-filter"' in h),
    ("wine section present",           "Opus One 2019" in h),
    ("category dropdown present",      'name="category"' in h),
    ("volume field present",           'name="volume_ml"' in h),
    ("return_category hidden input",   'name="return_category"' in h),
    ("wine option selected for wine",  '<option value="wine" selected>' in h),
]

h2 = render("filtered to wine", cat_sections=[cats_all[2]], active_category="wine")
checks += [
    ("filtered: only wine shown",      "Eagle Rare" not in h2 and "Opus One 2019" in h2),
    ("filtered: wine tab active",      'category=wine"\n       class="active"' in h2 or 'class="active"' in h2),
    ("filtered: hidden input carries", 'value="wine"' in h2),
]

h3 = render("empty filter result", cat_sections=[], active_category="spirit")
checks += [("empty filtered message", "No active bottles in this category" in h3)]

h4 = render("single category (no furniture)", cat_sections=[cats_all[0]],
            category_counts=[counts[0]], show_categories=False)
checks += [
    ("single cat: no filter row",   'class="cat-filter"' not in h4),
    ("single cat: no cat-header",   '<div class="cat-header">' not in h4),
    ("single cat: brands still on", "brand-header" in h4),
]

h5 = render("empty collection", cat_sections=[], category_counts=[], show_categories=False, total_count=0)
checks += [("empty collection message", "No active bottles yet" in h5)]

print("\n" + "=" * 58)
bad = 0
for name, ok in checks:
    bad += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
print("=" * 58)
print("ALL TEMPLATE CHECKS PASS" if not bad else f"{bad} CHECK(S) FAILED")
sys.exit(1 if bad else 0)
