import csv
import requests
from bs4 import BeautifulSoup
import re
from pathlib import Path
from datetime import datetime

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

BOTTLES_CSV = Path("data/bottles.csv")
HISTORY_CSV = Path("data/price_history.csv")

HISTORY_FIELDNAMES = [
    "timestamp", "bottle_id", "name",
    "wooden_cork_price", "bbb_price",
    "market_value", "sources_count",
    "msrp", "acquisition_price",
    "gain_loss_dollar", "gain_loss_pct"
]


def get_price_wooden_cork(url):
    if not url:
        return None
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    sale_div = soup.find("div", class_="block-price__sale")
    if not sale_div:
        return None
    matches = re.findall(r"\$[\d,]+\.\d{2}", sale_div.get_text())
    if not matches:
        return None
    return float(matches[-1].replace("$", "").replace(",", ""))


def get_price_bbb(url, target_proof=None):
    if not url:
        return None
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    bottle_boxes = soup.find_all("a", class_="bottle_listings_box")

    for box in bottle_boxes:
        text = box.get_text()
        if target_proof and (f"Proof:" not in text or target_proof not in text):
            continue
        info_divs = box.find_all("div", class_="listings_box_data_info_subject")
        for div in info_divs:
            if "Market Estimate" in div.get_text():
                matches = re.findall(r"\$[\d,]+", div.get_text())
                if len(matches) >= 2:
                    low = float(matches[0].replace("$", "").replace(",", ""))
                    high = float(matches[1].replace("$", "").replace(",", ""))
                    return (low + high) / 2
                elif len(matches) == 1:
                    return float(matches[0].replace("$", "").replace(",", ""))
    return None


def append_history_row(row):
    file_exists = HISTORY_CSV.exists()
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def process_bottle(bottle, timestamp):
    name = bottle["name"]
    proof = bottle.get("proof") or None

    print(f"\n{name}")
    print("=" * 60)

    wc_price = get_price_wooden_cork(bottle.get("wooden_cork_url"))
    bbb_price = get_price_bbb(bottle.get("bbb_url"), target_proof=proof)

    prices = []
    if wc_price:
        print(f"  Wooden Cork (retail):       ${wc_price:,.2f}")
        prices.append(wc_price)
    elif bottle.get("wooden_cork_url"):
        print(f"  Wooden Cork (retail):       FAILED")

    if bbb_price:
        print(f"  Bottle Blue Book (auction): ${bbb_price:,.2f}")
        prices.append(bbb_price)
    elif bottle.get("bbb_url"):
        print(f"  Bottle Blue Book (auction): FAILED")

    if not prices:
        print("  No prices available")
        return None

    market_value = sum(prices) / len(prices)
    print("-" * 60)
    print(f"  Market Value:               ${market_value:,.2f}  (avg of {len(prices)})")

    msrp = float(bottle["msrp"]) if bottle.get("msrp") else None
    paid = float(bottle["acquisition_price"]) if bottle.get("acquisition_price") else None

    if msrp:
        print(f"  MSRP:                       ${msrp:,.2f}")
    if paid:
        print(f"  You Paid:                   ${paid:,.2f}")

    delta_dollar = None
    delta_pct = None
    if paid:
        delta_dollar = market_value - paid
        delta_pct = (delta_dollar / paid) * 100
        sign = "+" if delta_dollar >= 0 else ""
        print(f"  Gain/Loss vs. Paid:         {sign}${delta_dollar:,.2f}  ({sign}{delta_pct:.1f}%)")

    # Log to history
    append_history_row({
        "timestamp": timestamp,
        "bottle_id": bottle["bottle_id"],
        "name": name,
        "wooden_cork_price": f"{wc_price:.2f}" if wc_price else "",
        "bbb_price": f"{bbb_price:.2f}" if bbb_price else "",
        "market_value": f"{market_value:.2f}",
        "sources_count": len(prices),
        "msrp": f"{msrp:.2f}" if msrp else "",
        "acquisition_price": f"{paid:.2f}" if paid else "",
        "gain_loss_dollar": f"{delta_dollar:.2f}" if delta_dollar is not None else "",
        "gain_loss_pct": f"{delta_pct:.2f}" if delta_pct is not None else "",
    })

    return {"market_value": market_value, "paid": paid}


def main():
    if not BOTTLES_CSV.exists():
        print(f"ERROR: {BOTTLES_CSV} not found")
        return

    with open(BOTTLES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        bottles = list(reader)

    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"Loaded {len(bottles)} bottles from {BOTTLES_CSV}")
    print(f"Run timestamp: {timestamp}")

    total_value = 0
    total_paid = 0

    for bottle in bottles:
        result = process_bottle(bottle, timestamp)
        if result and result["paid"]:
            total_value += result["market_value"]
            total_paid += result["paid"]

    if total_paid > 0:
        print("\n" + "=" * 60)
        print("COLLECTION SUMMARY")
        print("=" * 60)
        print(f"  Total Paid:        ${total_paid:,.2f}")
        print(f"  Current Value:     ${total_value:,.2f}")
        delta = total_value - total_paid
        pct = (delta / total_paid) * 100
        sign = "+" if delta >= 0 else ""
        print(f"  Total Gain/Loss:   {sign}${delta:,.2f}  ({sign}{pct:.1f}%)")
        print(f"\nHistory logged to: {HISTORY_CSV}")


if __name__ == "__main__":
    main()