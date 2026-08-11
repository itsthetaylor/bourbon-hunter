"""
brands.py — shared, DISPLAY-ONLY brand derivation for the Flask editor and the
GitHub Pages dashboard. There is no brand field in the schema; brand is derived
from the leading word of the bottle name. Keeping this in one module means the
editor and the dashboard group and label brands identically (no drift).

No schema or data changes — this only affects how the two views organize their
already-loaded rows.
"""

from collections import OrderedDict

# Display-only aliases: brand key (the leading word, lowercased) -> nice label.
# For single-bottle brands whose name doesn't start with the full brand, so the
# heading reads correctly (e.g. "Old Fitzgerald..." should show under "Old
# Fitzgerald", not "Old"). Takes precedence over the derived label.
BRAND_ALIASES = {
    "old": "Old Fitzgerald",
    "thomas": "Thomas H. Handy",
}


def brand_key(name):
    """Grouping key: the leading word of the bottle name, normalized."""
    parts = (name or "").strip().split()
    return parts[0].lower() if parts else ""


def brand_label(names):
    """Heading label for a brand group. An alias wins if the leading word has one;
    otherwise it's the leading run of words shared by every name in the group (so
    the two Eagle Rares -> 'Eagle Rare', the Wellers -> 'Weller'), falling back to
    a single bottle's leading word."""
    splits = [n.split() for n in names if n and n.split()]
    if not splits:
        return ""
    key = splits[0][0].lower()
    if key in BRAND_ALIASES:
        return BRAND_ALIASES[key]
    if len(splits) == 1:
        return splits[0][0]
    prefix = []
    for col in zip(*splits):
        if all(w == col[0] for w in col):
            prefix.append(col[0])
        else:
            break
    return " ".join(prefix) if prefix else splits[0][0]


def brand_sections(groups):
    """Bucket already-formed product groups by derived brand.

    `groups` is a list of product groups (each a list of dicts that carry a 'name'
    key — bottle rows in the editor, card dicts in the dashboard). Returns a list
    of {'brand': label, 'groups': [product groups]}, with product groups sorted by
    name within each brand and brands sorted A->Z. Grouping is by the name's
    leading word, so two different brands sharing a first word would merge (none do
    in the current collection)."""
    buckets = OrderedDict()
    for g in groups:
        buckets.setdefault(brand_key(g[0].get("name", "")), []).append(g)

    sections = []
    for gs in buckets.values():
        gs_sorted = sorted(gs, key=lambda g: g[0].get("name", "").lower())
        label = brand_label([g[0].get("name", "") for g in gs_sorted])
        sections.append({"brand": label, "groups": gs_sorted})
    sections.sort(key=lambda s: s["brand"].lower())
    return sections
