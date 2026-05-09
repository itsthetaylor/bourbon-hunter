"""
Generate a static HTML dashboard from bottles.csv + price_history.csv.
Output: docs/index.html (GitHub Pages serves from /docs)
"""

import csv
from pathlib import Path

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

    latest_by_bottle = {}
    for row in history:
        bid = row["bottle_id"]
        if bid not in latest_by_bottle or row["timestamp"] > latest_by_bottle[bid]["timestamp"]:
            latest_by_bottle[bid] = row

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
            "batch": bottle.get("batch", ""),
            "bottle_code": bottle.get("bottle_code", ""),
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
            gain_str = "tracking soon"
            gain_class_card = "neutral"
        else:
            value_str = f"${c['market_value']:,.0f}"
            if c["gain_dollar"] is not None:
                s = "+" if c["gain_dollar"] >= 0 else ""
                gain_str = f"{s}${c['gain_dollar']:,.0f} · {s}{c['gain_pct']:.0f}%"
                gain_class_card = "positive" if c["gain_dollar"] >= 0 else "negative"
            else:
                gain_str = "no acquisition data"
                gain_class_card = "neutral"

        paid_str = f"${c['paid']:,.0f}" if c["paid"] else "—"
        msrp_str = f"${c['msrp']:,.0f}" if c["msrp"] else "—"

        # Subtitle line: proof, batch, bottle_code
        subtitle_parts = []
        if c["proof"]:
            subtitle_parts.append(f"{c['proof']} proof")
        if c["batch"]:
            subtitle_parts.append(f"Batch {c['batch']}")
        if c["bottle_code"]:
            subtitle_parts.append(c["bottle_code"])
        subtitle = " · ".join(subtitle_parts) if subtitle_parts else "—"

        cards_html += f"""
        <div class="card">
            <div class="card-name">{c['name']}</div>
            <div class="card-subtitle">{subtitle}</div>
            <div class="card-value-row">
                <div class="card-value">{value_str}</div>
                <div class="card-gain {gain_class_card}">{gain_str}</div>
            </div>
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
<meta name="theme-color" content="#1a0f08">
<title>Bourbon Hunter</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #f4e4c1;
    min-height: 100vh;
    line-height: 1.5;
    background: #0a0604;
    background-image:
        linear-gradient(rgba(10, 6, 4, 0.85), rgba(10, 6, 4, 0.95)),
        url('shelf.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
}}

.container {{
    max-width: 700px;
    margin: 0 auto;
    padding: 20px 16px 40px 16px;
}}

header {{
    text-align: center;
    padding: 32px 0 24px 0;
}}

h1 {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 36px;
    font-weight: 700;
    color: #d4a574;
    letter-spacing: 1px;
    text-shadow: 0 2px 8px rgba(0,0,0,0.6);
    margin-bottom: 4px;
}}

.subtitle {{
    color: #a08770;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 3px;
    font-weight: 500;
}}

.summary {{
    background: linear-gradient(135deg, rgba(40, 22, 12, 0.92), rgba(60, 32, 18, 0.88));
    border: 1px solid rgba(212, 165, 116, 0.25);
    border-radius: 16px;
    padding: 28px 24px;
    margin: 16px 0 28px 0;
    box-shadow: 0 12px 40px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.05);
    backdrop-filter: blur(10px);
}}

.summary-label {{
    color: #a08770;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 2px;
    font-weight: 600;
}}

.summary-value {{
    font-family: Georgia, serif;
    font-size: 44px;
    font-weight: 700;
    color: #f4e4c1;
    margin: 6px 0 4px 0;
    text-shadow: 0 2px 8px rgba(0,0,0,0.4);
}}

.summary-gain {{
    font-size: 19px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}

.summary-stats {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-top: 22px;
    padding-top: 22px;
    border-top: 1px solid rgba(212, 165, 116, 0.15);
}}

.summary-stats div {{ display: flex; flex-direction: column; }}
.summary-stats span {{
    color: #a08770;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    font-weight: 600;
}}
.summary-stats strong {{
    font-size: 20px;
    margin-top: 4px;
    color: #f4e4c1;
    font-weight: 600;
}}

.positive {{ color: #b8e986; }}
.negative {{ color: #e88686; }}
.neutral {{ color: #a08770; font-style: italic; }}

.card {{
    background: linear-gradient(135deg, rgba(30, 18, 10, 0.9), rgba(45, 26, 14, 0.85));
    border: 1px solid rgba(212, 165, 116, 0.18);
    border-radius: 12px;
    padding: 20px 22px;
    margin-bottom: 14px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.4);
    backdrop-filter: blur(8px);
    transition: transform 0.2s, border-color 0.2s;
}}

.card:hover {{
    transform: translateY(-2px);
    border-color: rgba(212, 165, 116, 0.4);
}}

.card-name {{
    font-family: Georgia, serif;
    font-size: 18px;
    font-weight: 600;
    color: #f4e4c1;
    margin-bottom: 4px;
}}

.card-subtitle {{
    color: #a08770;
    font-size: 12px;
    margin-bottom: 14px;
    letter-spacing: 0.5px;
}}

.card-value-row {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
}}

.card-value {{
    font-family: Georgia, serif;
    font-size: 30px;
    font-weight: 700;
    color: #d4a574;
}}

.card-gain {{
    font-size: 14px;
    font-weight: 600;
}}

.card-meta {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 14px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid rgba(212, 165, 116, 0.12);
}}

.card-meta div {{ display: flex; flex-direction: column; }}
.card-meta span {{
    color: #a08770;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-weight: 600;
}}
.card-meta strong {{
    font-size: 14px;
    margin-top: 3px;
    color: #f4e4c1;
    font-weight: 600;
}}

footer {{
    text-align: center;
    padding: 28px 0 16px 0;
    color: #6b5544;
    font-size: 11px;
    letter-spacing: 0.5px;
}}
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>Bourbon Hunter</h1>
        <div class="subtitle">Personal Collection</div>
    </header>

    <div class="summary">
        <div class="summary-label">Current Value</div>
        <div class="summary-value">${total_value:,.0f}</div>
        <div class="summary-gain {gain_class}">{sign}${total_gain:,.0f} · {sign}{total_gain_pct:.1f}%</div>
        <div class="summary-stats">
            <div><span>Total Paid</span><strong>${total_paid:,.0f}</strong></div>
            <div><span>Bottles</span><strong>{len(cards)}</strong></div>
        </div>
    </div>

    <div class="bottles">
        {cards_html}
    </div>

    <footer>
        Last updated · {last_run}
    </footer>
</div>
</body>
</html>"""


def main():
    build_dashboard()


if __name__ == "__main__":
    main()