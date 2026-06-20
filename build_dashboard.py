"""
Generate a static HTML dashboard from the SQLite DB (data/bourbon.db).
Output: docs/index.html (GitHub Pages serves from /docs)

Value math:
  - Per-bottle market estimate = median of available asking prices (outlier-resistant).
  - Collection value = sum of market estimates over all status=active rows.
  - Unrealized gain = collection value − sum(paid) for active.
  - Realized gain = sum(sale_price − paid) for status=sold.
  - All market values are asking-based (retail + auction listings). eBay sold-price
    data is not included — see pipeline.py for the reasoning and fast-follow hook.
"""

import os
from pathlib import Path
from collections import OrderedDict

from dotenv import load_dotenv

import db

load_dotenv()

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "index.html"

EDITOR_BASE = f"http://{os.getenv('FLASK_TAILSCALE_IP', '100.111.112.8')}:5001"


def build_active_card(bottle, snapshot):
    market_value = float(snapshot["market_value"]) if snapshot and snapshot.get("market_value") else None
    paid = float(bottle["paid"]) if bottle.get("paid") else None
    msrp = float(bottle["msrp"]) if bottle.get("msrp") else None

    gain_dollar = (market_value - paid) if (paid and market_value) else None
    gain_pct    = (gain_dollar / paid * 100) if gain_dollar is not None else None

    return {
        "bottle_id":   bottle["bottle_id"],
        "product_key": bottle.get("product_key") or bottle["bottle_id"],
        "name":        bottle["name"],
        "proof":       bottle.get("proof", ""),
        "batch":       bottle.get("batch", ""),
        "bottle_code": bottle.get("bottle_code", ""),
        "market_value": market_value,
        "msrp":        msrp,
        "paid":        paid,
        "gain_dollar": gain_dollar,
        "gain_pct":    gain_pct,
        "sources_count": int(snapshot["sources_count"]) if snapshot and snapshot.get("sources_count") else 0,
        "last_updated":  snapshot["timestamp"] if snapshot else None,
    }


def build_archive_card(bottle):
    paid       = float(bottle["paid"]) if bottle.get("paid") else None
    sale_price = float(bottle["sale_price"]) if bottle.get("sale_price") else None
    status     = bottle.get("status") or ""

    realized_gain = (sale_price - paid) if (status == "sold" and paid is not None and sale_price is not None) else None

    return {
        "name":          bottle["name"],
        "status":        status,
        "date_resolved": bottle.get("date_resolved", ""),
        "paid":          paid,
        "sale_price":    sale_price,
        "realized_gain": realized_gain,
    }


def build_dashboard():
    OUTPUT_DIR.mkdir(exist_ok=True)

    bottles = db.get_all_bottles()
    history = db.get_all_history()

    latest_by_bottle = {}
    for row in history:
        bid = row["bottle_id"]
        if bid not in latest_by_bottle or row["timestamp"] > latest_by_bottle[bid]["timestamp"]:
            latest_by_bottle[bid] = row

    active_cards  = []
    archive_cards = []
    total_paid    = 0.0
    total_value   = 0.0
    archive_realized_gain = 0.0

    for bottle in bottles:
        status = (bottle.get("status") or "active")
        if status == "active":
            snap = latest_by_bottle.get(bottle["bottle_id"])
            card = build_active_card(bottle, snap)
            if card["paid"]:
                total_paid += card["paid"]
            if card["market_value"]:
                total_value += card["market_value"]
            active_cards.append(card)
        else:
            card = build_archive_card(bottle)
            if card["realized_gain"] is not None:
                archive_realized_gain += card["realized_gain"]
            archive_cards.append(card)

    # Group active cards by product_key for display (order preserved)
    groups_map = OrderedDict()
    for card in active_cards:
        pk = card["product_key"]
        groups_map.setdefault(pk, []).append(card)
    active_groups = list(groups_map.values())

    total_gain     = total_value - total_paid if total_paid else 0
    total_gain_pct = (total_gain / total_paid * 100) if total_paid else 0
    last_run = max(
        (c["last_updated"] for c in active_cards if c["last_updated"]),
        default="never",
    )

    html = render_html(
        active_groups, archive_cards,
        total_paid, total_value, total_gain, total_gain_pct,
        archive_realized_gain, last_run,
        len(active_cards),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_FILE}")
    print(f"  Active: {len(active_cards)} bottles ({len(active_groups)} products), total value ${total_value:,.2f}")
    print(f"  Archive: {len(archive_cards)} bottles, realized gain ${archive_realized_gain:,.2f}")


def render_active_group(group):
    """Render one product group — single card or stacked card with ×N badge."""
    c0    = group[0]
    count = len(group)

    if c0["market_value"] is None:
        value_str      = "—"
        gain_str       = "tracking soon"
        gain_class_card = "neutral"
    else:
        group_value = sum(c["market_value"] for c in group if c["market_value"])
        value_str   = f"${c0['market_value']:,.0f}"
        if c0["gain_dollar"] is not None:
            s = "+" if c0["gain_dollar"] >= 0 else ""
            gain_str = f"{s}${c0['gain_dollar']:,.0f} · {s}{c0['gain_pct']:.0f}% per bottle"
            gain_class_card = "positive" if c0["gain_dollar"] >= 0 else "negative"
        else:
            gain_str = "no acquisition data"
            gain_class_card = "neutral"

    paid_str = f"${c0['paid']:,.0f}" if c0["paid"] else "—"
    msrp_str = f"${c0['msrp']:,.0f}" if c0["msrp"] else "—"

    subtitle_parts = []
    if c0["proof"]:
        subtitle_parts.append(f"{c0['proof']} proof")
    if c0["batch"]:
        subtitle_parts.append(f"Batch {c0['batch']}")
    if count == 1 and c0["bottle_code"]:
        subtitle_parts.append(c0["bottle_code"])
    subtitle = " · ".join(subtitle_parts) if subtitle_parts else "—"

    badge = (
        f' <span style="font-size:12px;font-weight:700;color:#d4a574;'
        f'background:rgba(212,165,116,.15);border:1px solid rgba(212,165,116,.3);'
        f'border-radius:5px;padding:2px 8px;vertical-align:middle">\xd7{count}</span>'
        if count > 1 else ""
    )

    # Per-bottle rows for multi-bottle groups
    unit_rows = ""
    if count > 1:
        for c in group:
            unit_paid = f"${c['paid']:,.0f}" if c["paid"] else "—"
            unit_rows += f"""
            <div style="font-size:12px;color:#a08770;padding:4px 0 2px;
                        border-top:1px solid rgba(212,165,116,.08)">
              <strong style="color:#c9b896">{unit_paid}</strong>
              {f"· {c['bottle_code']}" if c['bottle_code'] else ""}
            </div>"""

    group_total = ""
    if count > 1 and c0["market_value"]:
        gt = sum(c["market_value"] for c in group if c["market_value"])
        group_total = f'<div style="font-size:11px;color:#a08770;margin-top:6px">Group total (asking): <strong style="color:#d4a574">${gt:,.0f}</strong></div>'

    return f"""
        <div class="card">
            <div class="card-name">{c0['name']}{badge}</div>
            <div class="card-subtitle">{subtitle}</div>
            <div class="card-value-row">
                <div class="card-value">{value_str}</div>
                <div class="card-gain {gain_class_card}">{gain_str}</div>
            </div>
            <div class="card-meta">
                <div><span>Paid</span><strong>{paid_str}</strong></div>
                <div><span>MSRP</span><strong>{msrp_str}</strong></div>
                <div><span>Sources</span><strong>{c0['sources_count']}</strong></div>
            </div>
            {group_total}
            {unit_rows}
            <div class="card-actions">
                <a class="card-btn edit" href="{EDITOR_BASE}/#{c0['bottle_id']}">Edit / Archive</a>
            </div>
        </div>
        """


def render_archive_card(c):
    paid_str = f"${c['paid']:,.0f}" if c["paid"] else "—"
    sale_str = f"${c['sale_price']:,.0f}" if c["sale_price"] else "—"

    if c["realized_gain"] is not None:
        s = "+" if c["realized_gain"] >= 0 else ""
        gain_str = f"{s}${c['realized_gain']:,.0f}"
        gain_class_card = "positive" if c["realized_gain"] >= 0 else "negative"
    else:
        gain_str = "—"
        gain_class_card = "neutral"

    status_label  = (c["status"] or "archived").upper()
    resolved_str  = c["date_resolved"] or "—"

    return f"""
        <div class="card archived">
            <div class="archive-badge">{status_label}</div>
            <div class="card-name">{c['name']}</div>
            <div class="card-subtitle">Resolved {resolved_str}</div>
            <div class="card-value-row">
                <div class="card-value">{sale_str}</div>
                <div class="card-gain {gain_class_card}">{gain_str}</div>
            </div>
            <div class="card-meta">
                <div><span>Paid</span><strong>{paid_str}</strong></div>
                <div><span>Sale</span><strong>{sale_str}</strong></div>
                <div><span>Realized</span><strong>{gain_str}</strong></div>
            </div>
        </div>
        """


def render_html(active_groups, archive_cards, total_paid, total_value,
                total_gain, total_gain_pct, archive_realized_gain, last_run,
                bottle_count):
    sign       = "+" if total_gain >= 0 else ""
    gain_class = "positive" if total_gain >= 0 else "negative"
    arch_sign  = "+" if archive_realized_gain >= 0 else ""

    active_html  = "".join(render_active_group(g) for g in active_groups)
    archive_html = (
        "".join(render_archive_card(c) for c in archive_cards)
        if archive_cards else
        '<div class="empty-archive">No archived bottles yet.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#1a0f08">
<title>Bourbon Hunter</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #f4e4c1; min-height: 100vh; line-height: 1.5; background: #0a0604;
    background-image: linear-gradient(rgba(10,6,4,.85), rgba(10,6,4,.95)), url('shelf.jpg');
    background-size: cover; background-position: center; background-attachment: fixed;
}}
.container {{ max-width: 700px; margin: 0 auto; padding: 20px 16px 40px; }}
header {{ text-align: center; padding: 32px 0 24px; }}
h1 {{
    font-family: Georgia, serif; font-size: 36px; font-weight: 700;
    color: #d4a574; letter-spacing: 1px; text-shadow: 0 2px 8px rgba(0,0,0,.6);
    margin-bottom: 4px;
}}
.subtitle {{ color: #a08770; font-size: 13px; text-transform: uppercase; letter-spacing: 3px; }}
.summary {{
    background: linear-gradient(135deg, rgba(40,22,12,.92), rgba(60,32,18,.88));
    border: 1px solid rgba(212,165,116,.25); border-radius: 16px;
    padding: 28px 24px; margin: 16px 0 28px;
    box-shadow: 0 12px 40px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.05);
}}
.summary-label {{ color: #a08770; font-size: 11px; text-transform: uppercase; letter-spacing: 2px; font-weight: 600; }}
.summary-value {{ font-family: Georgia, serif; font-size: 44px; font-weight: 700; color: #f4e4c1; margin: 6px 0 4px; }}
.summary-gain {{ font-size: 19px; font-weight: 600; }}
.summary-note {{ font-size: 10px; color: #6b5544; margin-top: 4px; letter-spacing: .5px; }}
.summary-stats {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
    margin-top: 22px; padding-top: 22px;
    border-top: 1px solid rgba(212,165,116,.15);
}}
.summary-stats div {{ display: flex; flex-direction: column; }}
.summary-stats span {{ color: #a08770; font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600; }}
.summary-stats strong {{ font-size: 20px; margin-top: 4px; color: #f4e4c1; font-weight: 600; }}
.positive {{ color: #b8e986; }} .negative {{ color: #e88686; }} .neutral {{ color: #a08770; font-style: italic; }}
.section-header {{
    font-family: Georgia, serif; font-size: 16px; color: #d4a574;
    letter-spacing: 1px; margin: 28px 0 12px;
    padding-bottom: 6px; border-bottom: 1px solid rgba(212,165,116,.18);
}}
.card {{
    position: relative;
    background: linear-gradient(135deg, rgba(30,18,10,.9), rgba(45,26,14,.85));
    border: 1px solid rgba(212,165,116,.18); border-radius: 12px;
    padding: 20px 22px; margin-bottom: 14px;
    box-shadow: 0 6px 20px rgba(0,0,0,.4); transition: transform .2s, border-color .2s;
}}
.card:hover {{ transform: translateY(-2px); border-color: rgba(212,165,116,.4); }}
.card.archived {{
    opacity: .78;
    background: linear-gradient(135deg, rgba(22,14,8,.85), rgba(32,20,12,.8));
    border-color: rgba(160,135,112,.18);
}}
.card.archived .card-name, .card.archived .card-value {{ color: #c9b896; }}
.archive-badge {{
    position: absolute; top: 14px; right: 16px; font-size: 10px; font-weight: 700;
    letter-spacing: 1.5px; color: #a08770; background: rgba(0,0,0,.35);
    border: 1px solid rgba(160,135,112,.35); padding: 3px 8px; border-radius: 4px;
}}
.empty-archive {{ color: #6b5544; font-style: italic; font-size: 13px; text-align: center; padding: 20px 0; }}
.card-name {{ font-family: Georgia, serif; font-size: 18px; font-weight: 600; color: #f4e4c1; margin-bottom: 4px; }}
.card-subtitle {{ color: #a08770; font-size: 12px; margin-bottom: 14px; }}
.card-value-row {{ display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
.card-value {{ font-family: Georgia, serif; font-size: 30px; font-weight: 700; color: #d4a574; }}
.card-gain {{ font-size: 14px; font-weight: 600; }}
.card-meta {{
    display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px;
    margin-top: 16px; padding-top: 16px; border-top: 1px solid rgba(212,165,116,.12);
}}
.card-meta div {{ display: flex; flex-direction: column; }}
.card-meta span {{ color: #a08770; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }}
.card-meta strong {{ font-size: 14px; margin-top: 3px; color: #f4e4c1; font-weight: 600; }}
.card-actions {{ display: flex; gap: 10px; margin-top: 16px; }}
.card-btn {{
    flex: 1; text-align: center; text-decoration: none; font-size: 13px;
    font-weight: 600; letter-spacing: .5px; padding: 11px 10px;
    border-radius: 8px; transition: border-color .2s, background .2s;
}}
.card-btn.edit {{ color: #f4e4c1; background: rgba(90,51,24,.55); border: 1px solid rgba(212,165,116,.4); }}
.card-btn:hover {{ border-color: rgba(212,165,116,.7); }}
footer {{ text-align: center; padding: 28px 0 16px; color: #6b5544; font-size: 11px; }}
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>Bourbon Hunter</h1>
        <div class="subtitle">Personal Collection</div>
    </header>

    <div class="summary">
        <div class="summary-label">Current Value (asking-based)</div>
        <div class="summary-value">${total_value:,.0f}</div>
        <div class="summary-gain {gain_class}">{sign}${total_gain:,.0f} · {sign}{total_gain_pct:.1f}%</div>
        <div class="summary-note">Market estimates = median of retail + auction asking prices. eBay sold prices not included.</div>
        <div class="summary-stats">
            <div><span>Total Paid</span><strong>${total_paid:,.0f}</strong></div>
            <div><span>Bottles</span><strong>{bottle_count}</strong></div>
        </div>
    </div>

    <div class="section-header">Active Collection — {bottle_count} bottles, ${total_value:,.0f} current value</div>
    <div class="bottles">{active_html}</div>

    <div class="section-header">Archive — {len(archive_cards)} bottles, {arch_sign}${archive_realized_gain:,.0f} realized gain</div>
    <div class="bottles">{archive_html}</div>

    <footer>Last updated · {last_run}</footer>
</div>
</body>
</html>"""


def main():
    build_dashboard()


if __name__ == "__main__":
    main()
