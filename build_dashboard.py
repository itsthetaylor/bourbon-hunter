"""
Generate a static HTML dashboard from bottles.csv + price_history.csv.
Output: docs/index.html (GitHub Pages serves from /docs)
"""

import csv
import json
from pathlib import Path
from datetime import datetime

BOTTLES_CSV = Path("data/bottles.csv")
HISTORY_CSV = Path("data/price_history.csv")
OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "index.html"


def load_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_dashboard():
    OUTPUT_DIR.mkdir(exist_ok=True)

    bottles = load_csv(BOTTLES_CSV)
    history = load_csv(HISTORY_CSV)

    # Get latest snapshot per bottle from history
    latest_by_bottle = {}
    for row in history:
        bid = row["bottle_id"]
        if bid not in latest_by_bottle or row["timestamp"] > latest_by_bottle[bid]["timestamp"]:
            latest_by_bottle[bid] = row

    # Build bottle cards data
    cards = []
    total_paid = 0.0
    total_value = 0.0

    for bottle in bottles:
        bid = bottle["bottle_id"]
        snapshot = latest_by_bottle.get(bid)

        market_value = float(snapshot["market_value"]) if snapshot and snapshot.get("market_value") else None
        paid = float(bottle["acquisition_price"]) if bottle.get("acquisition_price") else None
        msrp = float(bottle["msrp"]) if bottle.get("msrp") else None

        gain_dollar = None
        gain_pct = None
        if paid and market_value:
            gain_dollar = market_value - paid
            gain_pct = (gain_dollar / paid) * 100

        if paid and market_value:
            total_paid += paid
            total_value += market_value

        cards.append({
            "name": bottle["name"],
            "proof": bottle.get("proof", ""),
            "market_value": market_value,
            "msrp": msrp,
            "paid": paid,
            "gain_dollar": gain_dollar,
            "gain_pct": gain_pct,
            "sources_count": int(snapshot["sources_count"]) if snapshot and snapshot.get("sources_count") else 0,
            "last_updated": snapshot["timestamp"] if snapshot else None,
        })

    total_gain = total_value - total_paid if total_paid else 0
    total_gain_pct = (total_gain / total_paid * 100) if total_paid else 0

    last_run = max((c["last_updated"] for c in cards if c["last_updated"]), default="never")

    html = render_html(cards, total_paid, total_value, total_gain, total_gain_pct, last_run)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_FILE}")
    print(f"  {len(cards)} bottles, total value ${total_value:,.2f}")


def render_html(cards, total_paid, total_value, total_gain, total_gain_pct, last_run):
    sign = "+" if total_gain >= 0 else ""
    gain_class = "positive" if total_gain >= 0 else "negative"

    cards_html = ""
    for c in cards:
        if c["market_value"] is None:
            value_str = "—"
            gain_str = "no data yet"
            gain_class_card = ""
        else:
            value_str = f"${c['market_value']:,.0f}"
            if c["gain_dollar"] is not None:
                s = "+" if c["gain_dollar"] >= 0 else ""
                gain_str = f"{s}${c['gain_dollar']:,.0f} ({s}{c['gain_pct']:.0f}%)"
                gain_class_card = "positive" if c["gain_dollar"] >= 0 else "negative"
            else:
                gain_str = "no acquisition data"
                gain_class_card = ""

        paid_str = f"${c['paid']:,.0f}" if c["paid"] else "—"
        msrp_str = f"${c['msrp']:,.0f}" if c["msrp"] else "—"

        cards_html += f"""
        <div class="card">
            <div class="card-header">
                <h2>{c['name']}</h2>
                {f'<span class="proof">{c["proof"]} proof</span>' if c["proof"] else ""}
            </div>
            <div class="card-value">{value_str}</div>
            <div class="card-gain {gain_class_card}">{gain_str}</div>
            <div class="card-meta">
                <div><span>Paid</span><strong>{paid_str}</strong></div>
                <div><span>MSRP</span><strong>{msrp_str}</strong></div>
                <div><span>Sources</span><strong>{c['sources_count']}</strong></div>
            </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#1a1a1a">
<title>🥃 Bourbon Hunter</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f0f0f;
    color: #e8e8e8;
    padding: 16px;
    max-width: 700px;
    margin: 0 auto;
    line-height: 1.5;
}}
header {{ text-align: center; padding: 24px 0; }}
h1 {{ font-size: 28px; margin-bottom: 4px; }}
.subtitle {{ color: #888; font-size: 13px; }}
.summary {{
    background: linear-gradient(135deg, #1a1a1a, #252525);
    border-radius: 16px;
    padding: 24px;
    margin: 16px 0 24px 0;
    border: 1px solid #333;
}}
.summary-label {{ color: #888; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }}
.summary-value {{ font-size: 36px; font-weight: 700; margin: 4px 0; }}
.summary-gain {{ font-size: 20px; font-weight: 600; }}
.summary-stats {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-top: 20px;
    padding-top: 20px;
    border-top: 1px solid #333;
}}
.summary-stats div {{ display: flex; flex-direction: column; }}
.summary-stats span {{ color: #888; font-size: 12px; text-transform: uppercase; }}
.summary-stats strong {{ font-size: 18px; margin-top: 4px; }}
.positive {{ color: #4ade80; }}
.negative {{ color: #f87171; }}
.card {{
    background: #1a1a1a;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 12px;
    border: 1px solid #2a2a2a;
}}
.card-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 12px;
}}
.card-header h2 {{ font-size: 17px; font-weight: 600; }}
.proof {{ color: #888; font-size: 12px; }}
.card-value {{ font-size: 28px; font-weight: 700; }}
.card-gain {{ font-size: 14px; font-weight: 500; margin-top: 2px; }}
.card-meta {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 12px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid #2a2a2a;
}}
.card-meta div {{ display: flex; flex-direction: column; }}
.card-meta span {{ color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
.card-meta strong {{ font-size: 14px; margin-top: 2px; font-weight: 600; }}
footer {{ text-align: center; padding: 24px 0; color: #555; font-size: 12px; }}
</style>
</head>
<body>
<header>
    <h1>🥃 Bourbon Hunter</h1>
    <div class="subtitle">Personal Collection Tracker</div>
</header>

<div class="summary">
    <div class="summary-label">Current Value</div>
    <div class="summary-value">${total_value:,.0f}</div>
    <div class="summary-gain {gain_class}">{sign}${total_gain:,.0f} ({sign}{total_gain_pct:.1f}%)</div>
    <div class="summary-stats">
        <div><span>Total Paid</span><strong>${total_paid:,.0f}</strong></div>
        <div><span>Bottles</span><strong>{len(cards)}</strong></div>
    </div>
</div>

<div class="bottles">
    {cards_html}
</div>

<footer>
    Last updated: {last_run}
</footer>
</body>
</html>"""


def main():
    build_dashboard()


if __name__ == "__main__":
    main()