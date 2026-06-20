"""
Bourbon Hunter — Phase 2 local editor (Flask).

Mobile-first dark UI for editing the collection, served over Tailscale.

DATA-SAFETY CONTRACT (do not break):
  * This app writes ONLY to data/bottles.csv. It NEVER writes price_history.csv.
  * Every bottles.csv write goes through archive_bottle.save_bottles() (atomic).
  * Schema is read from the file header, never hardcoded here.
"""

import csv
import logging
import os
import traceback
from collections import OrderedDict
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string, abort, jsonify
from dotenv import load_dotenv

import pipeline
from archive_bottle import (
    BOTTLES_CSV,
    VALID_STATUSES,
    load_bottles,
    save_bottles,
    read_header,
    archive_bottle,
)

load_dotenv()

HISTORY_CSV    = Path("data/price_history.csv")
# TAILSCALE_IP is DISPLAY-ONLY — the "open on your phone" hint and the editor_base
# link in the template. Flask BINDS to BIND_HOST (0.0.0.0, all interfaces), so it
# starts even when Tailscale hasn't assigned the IP yet (NoState). Reaching it over
# Tailscale still uses this IP.
TAILSCALE_IP   = os.getenv("FLASK_TAILSCALE_IP", "127.0.0.1")
BIND_HOST      = "0.0.0.0"
PORT           = 5001

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app.logger.setLevel(logging.INFO)


@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled exception:\n%s", traceback.format_exc())
    return (
        f"<pre style='color:red;padding:20px'>Error: {e}\n\n{traceback.format_exc()}</pre>",
        500,
    )


URL_FIELDS = [
    ("wooden_cork_url",  "Wooden Cork",    pipeline.get_price_wooden_cork),
    ("bbb_url",          "Bottle Blue Book", pipeline.get_price_bbb),
    ("barrel_tap_url",   "The Barrel Tap", pipeline.get_price_barrel_tap),
    ("keg_n_bottle_url", "Keg N Bottle",   pipeline.get_price_keg_n_bottle),
]
URL_KEYS       = [k for k, _, _ in URL_FIELDS]
URL_LABELS     = [(k, label) for k, label, _ in URL_FIELDS]
SCRAPER_BY_KEY = {k: fn for k, _, fn in URL_FIELDS}


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #

def latest_market_values():
    if not HISTORY_CSV.exists():
        return {}
    latest = {}
    with open(HISTORY_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            bid = row.get("bottle_id")
            if not bid:
                continue
            ts = row.get("timestamp", "")
            if bid not in latest or ts > latest[bid].get("timestamp", ""):
                latest[bid] = row
    return latest


def active_bottles():
    return [b for b in load_bottles() if (b.get("status") or "active") == "active"]


def product_groups(bottles):
    """Return list-of-lists grouped by product_key, preserving CSV order."""
    groups = OrderedDict()
    for b in bottles:
        pk = b.get("product_key") or b.get("bottle_id")
        groups.setdefault(pk, []).append(b)
    return list(groups.values())


def _render_index(error=None, status_code=200):
    try:
        bottles = active_bottles()
        html = render_template_string(
            INDEX_TEMPLATE,
            product_groups=product_groups(bottles),
            market=latest_market_values(),
            url_labels=URL_LABELS,
            editor_base=f"http://{TAILSCALE_IP}:{PORT}",
            error=error,
        )
    except Exception as e:
        app.logger.error("_render_index crashed:\n%s", traceback.format_exc())
        return (
            f"<pre style='color:red;padding:20px'>Render error: {e}\n\n"
            f"{traceback.format_exc()}</pre>",
            500,
        )
    return (html, status_code) if status_code != 200 else html


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return _render_index()


@app.route("/edit/<bottle_id>", methods=["POST"])
def edit(bottle_id):
    """Update paid and/or the 4 *_url fields for one bottle."""
    fieldnames = read_header(BOTTLES_CSV)
    bottles    = load_bottles()
    target     = next((b for b in bottles if b.get("bottle_id") == bottle_id), None)
    if target is None:
        abort(404)

    if "paid" in request.form:
        raw = request.form.get("paid", "").strip()
        if raw == "":
            target["paid"] = ""
        else:
            try:
                target["paid"] = f"{float(raw):.2f}"
            except ValueError:
                return _render_index(error=f"'{raw}' is not a valid price.", status_code=400)

    for key in URL_KEYS:
        if key in request.form:
            target[key] = request.form.get(key, "").strip()

    save_bottles(bottles, fieldnames)
    return redirect(url_for("index") + f"#{target.get('product_key') or bottle_id}")


@app.route("/archive/<bottle_id>", methods=["POST"])
def archive(bottle_id):
    status     = request.form.get("status", "").strip()
    sale_price = request.form.get("sale_price", "").strip() or None
    try:
        archive_bottle(bottle_id, status, sale_price)
    except ValueError as e:
        return _render_index(error=str(e), status_code=400)
    return redirect(url_for("index"))


@app.route("/urls")
def urls():
    return render_template_string(
        URLS_TEMPLATE,
        bottles=active_bottles(),
        url_labels=URL_LABELS,
    )


@app.route("/update_urls", methods=["POST"])
def update_urls():
    fieldnames = read_header(BOTTLES_CSV)
    bottles    = load_bottles()
    by_id      = {b.get("bottle_id"): b for b in bottles}
    for form_key, value in request.form.items():
        if "__" not in form_key:
            continue
        bid, field = form_key.rsplit("__", 1)
        if field in URL_KEYS and bid in by_id:
            by_id[bid][field] = value.strip()
    save_bottles(bottles, fieldnames)
    return redirect(url_for("urls"))


@app.route("/verify_url", methods=["POST"])
def verify_url():
    source = request.form.get("source", "")
    url    = request.form.get("url", "").strip()
    fn     = SCRAPER_BY_KEY.get(source)
    if not fn or not url:
        return jsonify(ok=False, error="unknown source or empty url"), 400
    try:
        price = fn(url)
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}")
    if price is None:
        return jsonify(ok=False, error="selector found no price on that page")
    return jsonify(ok=True, price=price)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0a0604; color: #f4e4c1; line-height: 1.5;
  min-height: 100vh; padding-bottom: 40px; -webkit-text-size-adjust: 100%;
}
.container { max-width: 640px; margin: 0 auto; padding: 16px 14px 40px; }
header { text-align: center; padding: 22px 0 12px; }
h1 {
  font-family: Georgia, serif; font-size: 30px; font-weight: 700;
  color: #d4a574; letter-spacing: 1px; text-shadow: 0 2px 8px rgba(0,0,0,.6);
}
.subtitle { color: #a08770; font-size: 12px; text-transform: uppercase; letter-spacing: 3px; margin-top: 2px; }
.nav { display: flex; gap: 10px; justify-content: center; margin: 12px 0 18px; }
.nav a {
  flex: 1; max-width: 220px; text-align: center; text-decoration: none;
  color: #f4e4c1; background: rgba(45,26,14,.85);
  border: 1px solid rgba(212,165,116,.3); border-radius: 10px;
  padding: 12px; font-size: 14px; font-weight: 600;
}
.nav a.active { background: #5a3318; border-color: rgba(212,165,116,.6); }
.banner {
  background: rgba(90,30,30,.85); border: 1px solid #e88686; color: #ffd6d6;
  border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; font-size: 14px;
}
.empty { text-align: center; color: #a08770; font-style: italic; font-size: 15px;
  padding: 48px 20px; border: 1px dashed rgba(160,135,112,.35);
  border-radius: 14px; margin-top: 20px; }
.card {
  background: linear-gradient(135deg, rgba(30,18,10,.92), rgba(45,26,14,.88));
  border: 1px solid rgba(212,165,116,.18); border-radius: 14px;
  padding: 18px; margin-bottom: 16px; box-shadow: 0 6px 20px rgba(0,0,0,.4);
}
.card-name { font-family: Georgia, serif; font-size: 19px; font-weight: 600; color: #f4e4c1; }
.qty-badge {
  font-size: 12px; font-weight: 700; color: #d4a574;
  background: rgba(212,165,116,.15); border: 1px solid rgba(212,165,116,.3);
  border-radius: 5px; padding: 2px 8px; vertical-align: middle; margin-left: 6px;
}
.card-sub { color: #a08770; font-size: 12px; margin: 2px 0 12px; letter-spacing: .5px; }
.market-row { display: flex; justify-content: space-between; align-items: baseline;
  padding: 10px 0 14px; border-bottom: 1px solid rgba(212,165,116,.12); margin-bottom: 14px; }
.market-val { font-family: Georgia, serif; font-size: 26px; color: #d4a574; font-weight: 700; }
.market-lbl { font-size: 11px; color: #a08770; text-transform: uppercase; letter-spacing: 1.5px; }
/* Action row: Drank / Sold / Edit */
.action-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
.action-form { display: flex; gap: 4px; align-items: center; }
.btn-action {
  font-size: 13px; font-weight: 600; border-radius: 8px; padding: 10px 14px;
  cursor: pointer; border: none; -webkit-appearance: none; white-space: nowrap;
}
.btn-drank { background: rgba(60,30,10,.8); color: #f4e4c1; border: 1px solid rgba(212,165,116,.35) !important; }
.btn-sold  { background: rgba(20,60,20,.8); color: #b8e986; border: 1px solid rgba(184,233,134,.35) !important; }
.btn-edit  { background: rgba(20,20,60,.7); color: #b8b8f4; border: 1px solid rgba(150,150,220,.35) !important; }
.sale-input {
  width: 70px; background: #1a0f08; color: #f4e4c1;
  border: 1px solid rgba(212,165,116,.3); border-radius: 8px;
  padding: 10px 8px; font-size: 15px; -webkit-appearance: none;
}
/* Edit pane (URL + paid fields) */
.edit-pane { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(212,165,116,.12); }
label.fld { display: block; font-size: 11px; color: #a08770;
  text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin: 12px 0 4px; }
input[type=text], input[type=number], select, textarea {
  width: 100%; background: #1a0f08; color: #f4e4c1;
  border: 1px solid rgba(212,165,116,.3); border-radius: 10px;
  padding: 13px 12px; font-size: 16px; -webkit-appearance: none;
}
.url-row { display: flex; gap: 8px; align-items: stretch; }
.url-row input { flex: 1; }
button {
  font-size: 15px; font-weight: 600; border: none; border-radius: 10px;
  padding: 14px 16px; cursor: pointer; -webkit-appearance: none;
}
.btn-primary { background: #5a3318; color: #f4e4c1;
  border: 1px solid rgba(212,165,116,.45); width: 100%; margin-top: 14px; }
.btn-verify { background: #2a1a0e; color: #d4a574;
  border: 1px solid rgba(212,165,116,.35); padding: 13px 14px; white-space: nowrap; }
.verify-msg { font-size: 12px; margin-top: 4px; min-height: 14px; }
.verify-ok { color: #b8e986; } .verify-bad { color: #e88686; }
/* Multi-bottle group expand */
.grp-details { margin-top: 10px; }
.grp-summary {
  color: #a08770; font-size: 13px; cursor: pointer; list-style: none;
  font-weight: 600; padding: 8px 0; border-top: 1px solid rgba(212,165,116,.1);
}
.grp-summary::-webkit-details-marker { display: none; }
.bottle-unit { padding: 10px 0 4px; border-top: 1px solid rgba(212,165,116,.08); }
.unit-meta { color: #a08770; font-size: 12px; margin-bottom: 8px; }
footer { text-align: center; color: #6b5544; font-size: 11px; padding: 20px 0; }
"""

# Shared action buttons macro (Jinja2 macro inlined as a string to keep template readable)
_ACTION_MACRO = """
{% macro bottle_actions(b) %}
<div class="action-row">
  <form method="post" action="{{ url_for('archive', bottle_id=b.bottle_id) }}" class="action-form">
    <input type="hidden" name="status" value="consumed">
    <button type="submit" class="btn-action btn-drank">Drank</button>
  </form>
  <form method="post" action="{{ url_for('archive', bottle_id=b.bottle_id) }}" class="action-form">
    <input type="hidden" name="status" value="sold">
    <input type="number" step="0.01" inputmode="decimal" name="sale_price"
           placeholder="$" class="sale-input">
    <button type="submit" class="btn-action btn-sold">Sold</button>
  </form>
  <button type="button" class="btn-action btn-edit"
          onclick="toggleEdit('{{ b.bottle_id }}')">Edit &#9660;</button>
</div>
<div id="edit_{{ b.bottle_id }}" class="edit-pane" style="display:none">
  <form method="post" action="{{ url_for('edit', bottle_id=b.bottle_id) }}">
    <label class="fld" for="paid_{{ b.bottle_id }}">Paid ($)</label>
    <input type="number" step="0.01" inputmode="decimal" id="paid_{{ b.bottle_id }}"
           name="paid" value="{{ b.paid }}" placeholder="what you paid">
    {% for key, label in url_labels %}
    <label class="fld">{{ label }} URL</label>
    <div class="url-row">
      <input type="text" name="{{ key }}" value="{{ b[key] }}"
             id="{{ b.bottle_id }}__{{ key }}" placeholder="paste listing URL"
             autocapitalize="off" autocomplete="off" spellcheck="false">
      <button type="button" class="btn-verify"
              onclick="verifyUrl('{{ key }}', '{{ b.bottle_id }}__{{ key }}')">Test</button>
    </div>
    <div class="verify-msg" id="msg_{{ b.bottle_id }}__{{ key }}"></div>
    {% endfor %}
    <button type="submit" class="btn-primary">Save changes</button>
  </form>
</div>
{% endmacro %}
"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a0f08">
<title>Bourbon Hunter · Editor</title>
<style>""" + BASE_CSS + """</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Bourbon Hunter</h1>
    <div class="subtitle">Collection Editor</div>
  </header>

  <nav class="nav">
    <a href="{{ url_for('index') }}" class="active">Bottles</a>
    <a href="{{ url_for('urls') }}">URL Tool</a>
  </nav>

  {% if error %}<div class="banner">{{ error }}</div>{% endif %}

  {% if not product_groups %}
    <div class="empty">No active bottles yet.<br>Add via photo intake, then edit here.</div>
  {% endif %}

""" + _ACTION_MACRO + """

  {% for grp in product_groups %}
  {% set b0 = grp[0] %}
  {% set count = grp|length %}
  {% set snap = market.get(b0.bottle_id) %}

  <div class="card" id="{{ b0.product_key or b0.bottle_id }}">
    <div class="card-name">
      {{ b0.name }}
      {% if count > 1 %}<span class="qty-badge">&times;{{ count }}</span>{% endif %}
    </div>
    <div class="card-sub">
      {{ b0.proof or '&mdash;' }}{% if b0.proof %} proof{% endif %}
      {%- if b0.batch %} &middot; Batch {{ b0.batch }}{% endif %}
      {%- if count == 1 and b0.bottle_code %} &middot; {{ b0.bottle_code }}{% endif %}
    </div>

    <div class="market-row">
      <div>
        <div class="market-lbl">Market value (asking)</div>
        <div class="market-val">
          {%- if snap and snap.market_value %}${{ '%.0f'|format(snap.market_value|float) }}
          {%- else %}&mdash;{% endif %}
        </div>
      </div>
      <div style="text-align:center">
        <div class="market-lbl">MSRP</div>
        <div class="market-val" style="font-size:20px;color:#a08770">
          {%- if b0.msrp %}${{ '%.0f'|format(b0.msrp|float) }}{% else %}&mdash;{% endif %}
        </div>
      </div>
      <div style="text-align:right">
        <div class="market-lbl">Sources</div>
        <div class="market-val" style="font-size:18px">
          {{ snap.sources_count if snap and snap.sources_count else '0' }}
        </div>
      </div>
    </div>

    {% if count == 1 %}
      {{ bottle_actions(b0) }}
    {% else %}
      <details class="grp-details">
        <summary class="grp-summary">{{ count }} bottles &mdash; tap to pick one</summary>
        {% for b in grp %}
        <div class="bottle-unit" id="{{ b.bottle_id }}">
          <div class="unit-meta">
            <strong style="color:#c9b896">{% if b.paid %}${{ '%.0f'|format(b.paid|float) }} paid{% else %}no cost logged{% endif %}</strong>
            {%- if b.date_acquired %} &middot; {{ b.date_acquired }}{% endif %}
            {%- if b.bottle_code %}<br><span style="font-size:11px;color:#6b5544">{{ b.bottle_code }}</span>{% endif %}
          </div>
          {{ bottle_actions(b) }}
        </div>
        {% endfor %}
      </details>
    {% endif %}
  </div>
  {% endfor %}

  <footer>Local editor &middot; writes only to bottles.csv &middot; price_history is read-only</footer>
</div>

<script>
function toggleEdit(id) {
  var p = document.getElementById('edit_' + id);
  p.style.display = p.style.display === 'none' ? '' : 'none';
}
async function verifyUrl(source, inputId) {
  var url = document.getElementById(inputId).value.trim();
  var msg = document.getElementById('msg_' + inputId);
  if (!url) { msg.textContent = ''; return; }
  msg.className = 'verify-msg'; msg.textContent = 'Testing…';
  try {
    var body = new URLSearchParams({source, url});
    var r = await fetch('{{ url_for("verify_url") }}', {method: 'POST', body});
    var d = await r.json();
    if (d.ok) { msg.className = 'verify-msg verify-ok'; msg.textContent = '✓ $' + d.price; }
    else { msg.className = 'verify-msg verify-bad'; msg.textContent = '✗ ' + d.error; }
  } catch (e) { msg.className = 'verify-msg verify-bad'; msg.textContent = '✗ ' + e; }
}
</script>
</body>
</html>"""

URLS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a0f08">
<title>Bourbon Hunter · URL Tool</title>
<style>""" + BASE_CSS + """</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Bourbon Hunter</h1>
    <div class="subtitle">Bulk URL Tool</div>
  </header>
  <nav class="nav">
    <a href="{{ url_for('index') }}">Bottles</a>
    <a href="{{ url_for('urls') }}" class="active">URL Tool</a>
  </nav>
  {% if not bottles %}
    <div class="empty">No active bottles yet.</div>
  {% else %}
  <form method="post" action="{{ url_for('update_urls') }}">
    {% for b in bottles %}
    <div class="card">
      <div class="card-name">{{ b.name }}</div>
      <div class="card-sub">{{ b.proof or '&mdash;' }}{% if b.proof %} proof{% endif %}</div>
      {% for key, label in url_labels %}
      <label class="fld">{{ label }}</label>
      <input type="text" name="{{ b.bottle_id }}__{{ key }}" value="{{ b[key] }}"
             placeholder="paste {{ label }} URL" autocapitalize="off"
             autocomplete="off" spellcheck="false">
      {% endfor %}
    </div>
    {% endfor %}
    <button type="submit" class="btn-primary">Save all URLs</button>
  </form>
  {% endif %}
  <footer>Paste a listing URL per source &middot; the pipeline scrapes these directly</footer>
</div>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 56)
    print("  Bourbon Hunter — Collection Editor")
    print(f"  Open on your phone:  http://{TAILSCALE_IP}:{PORT}")
    print(f"  (binding 0.0.0.0:{PORT} — independent of Tailscale state)")
    print("=" * 56)
    app.run(host=BIND_HOST, port=PORT, debug=False)
