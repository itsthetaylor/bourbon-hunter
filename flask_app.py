"""
Bourbon Hunter — hosted multi-user app (Flask).

Public landing page at /. The collection editor lives behind login and is scoped
to the logged-in user — a logged-out visitor can see NO bottle data anywhere.

SECURITY CONTRACT (do not break):
  * Every collection route is @login_required and passes current_user.id to db.py,
    which filters every bottle read/write by user_id. One user can never see or
    edit another user's bottles.
  * Passwords are bcrypt-hashed (auth.py); never stored or logged in plaintext.
  * Sessions via Flask-Login (signed cookie); secret from FLASK_SECRET_KEY env var.
  * Signup is invite-gated by the SIGNUP_CODE env var.
"""

import logging
import os
import traceback

from flask import (
    Flask, request, redirect, url_for, render_template_string, abort, jsonify,
    get_flashed_messages,
)
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

import auth
import brands
import db
import pipeline

load_dotenv()

PORT      = int(os.getenv("PORT", "5001"))   # Railway injects $PORT; 5001 locally
BIND_HOST = "0.0.0.0"
SIGNUP_CODE = os.getenv("SIGNUP_CODE")        # invite gate; unset => signup closed
# Secure cookies in production (Railway serves HTTPS); relaxed locally over http.
_IS_PROD = bool(os.getenv("RAILWAY_ENVIRONMENT"))

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Set it in .env (local) and in Railway's "
        "Variables (production). It signs the session cookie — never hardcode it."
    )

# Behind Railway's TLS-terminating proxy: trust X-Forwarded-* so url_for builds
# https URLs and secure cookies are recognized.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",   # blocks the session cookie on cross-site POSTs
    SESSION_COOKIE_SECURE=_IS_PROD,
)

auth.login_manager.init_app(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app.logger.setLevel(logging.INFO)


@app.errorhandler(Exception)
def handle_exception(e):
    # Let HTTP exceptions (404, login redirects, etc.) behave normally.
    if isinstance(e, HTTPException):
        return e
    # Never leak stack traces to users; log server-side only.
    app.logger.error("Unhandled exception:\n%s", traceback.format_exc())
    return ("<p style='padding:24px;font-family:sans-serif'>Something went wrong.</p>", 500)


URL_FIELDS = [
    ("wooden_cork_url",  "Wooden Cork",      pipeline.get_price_wooden_cork),
    ("bbb_url",          "Bottle Blue Book", pipeline.get_price_bbb),
    ("barrel_tap_url",   "The Barrel Tap",   pipeline.get_price_barrel_tap),
    ("keg_n_bottle_url", "Keg N Bottle",     pipeline.get_price_keg_n_bottle),
]
URL_KEYS       = [k for k, _, _ in URL_FIELDS]
URL_LABELS     = [(k, label) for k, label, _ in URL_FIELDS]
SCRAPER_BY_KEY = {k: fn for k, _, fn in URL_FIELDS}


# --------------------------------------------------------------------------- #
# Display helpers (pure)
# --------------------------------------------------------------------------- #

def product_groups(bottles):
    """Group by product_key, preserving order."""
    from collections import OrderedDict
    groups = OrderedDict()
    for b in bottles:
        pk = b.get("product_key") or b.get("bottle_id")
        groups.setdefault(pk, []).append(b)
    return list(groups.values())


def brand_groups(bottles):
    """DISPLAY-ONLY brand grouping (shared with the dashboard via brands.py)."""
    return brands.brand_sections(product_groups(bottles))


def _safe_next(nxt):
    """Only allow same-site relative redirect targets (block open redirects)."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return None


def _render_collection(error=None, status_code=200):
    """Render the current user's collection ONLY (scoped by current_user.id)."""
    try:
        bottles = db.get_active_bottles(current_user.id)
        html = render_template_string(
            INDEX_TEMPLATE,
            brand_groups=brand_groups(bottles),
            market=db.latest_market_values(current_user.id),
            url_labels=URL_LABELS,
            user_email=current_user.email,
            error=error,
        )
    except Exception:
        app.logger.error("collection render crashed:\n%s", traceback.format_exc())
        return ("<p style='padding:24px;font-family:sans-serif'>Couldn't load your "
                "collection.</p>", 500)
    return (html, status_code) if status_code != 200 else html


# --------------------------------------------------------------------------- #
# Public + auth routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    """Public landing page — NO bottle data."""
    return render_template_string(
        LANDING_TEMPLATE,
        authenticated=current_user.is_authenticated,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("collection"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        row = db.get_user_by_email(email)
        if row and auth.verify_password(password, row["password_hash"]):
            login_user(auth.User(row["id"], row["email"], row["is_admin"]))
            return redirect(_safe_next(request.args.get("next")) or url_for("collection"))
        error = "Invalid email or password."
    msgs = get_flashed_messages()
    return render_template_string(LOGIN_TEMPLATE, error=error, flashes=msgs), \
        (401 if error else 200)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("collection"))
    error = None
    if request.method == "POST":
        code = request.form.get("invite_code", "")
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not SIGNUP_CODE or code != SIGNUP_CODE:
            error = "Invalid invite code."
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            try:
                pw_hash = auth.hash_password(password)
                is_admin = (db.count_users() == 0)  # first account is the owner/admin
                uid = db.create_user(email, pw_hash, is_admin=is_admin)
                login_user(auth.User(uid, email, is_admin))
                return redirect(url_for("collection"))
            except ValueError as e:
                error = str(e)
    return render_template_string(SIGNUP_TEMPLATE, error=error), (400 if error else 200)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Collection routes — ALL login-gated and scoped to current_user.id
# --------------------------------------------------------------------------- #

@app.route("/collection")
@login_required
def collection():
    return _render_collection()


@app.route("/edit/<bottle_id>", methods=["POST"])
@login_required
def edit(bottle_id):
    """Update paid and/or the 4 *_url fields for one of the user's bottles."""
    target = db.get_bottle(bottle_id, current_user.id)
    if target is None:
        abort(404)  # not found OR not yours — same response either way

    fields = {}
    if "paid" in request.form:
        raw = request.form.get("paid", "").strip()
        if raw != "":
            try:
                float(raw)
            except ValueError:
                return _render_collection(error=f"'{raw}' is not a valid price.", status_code=400)
        fields["paid"] = raw

    for key in URL_KEYS:
        if key in request.form:
            fields[key] = request.form.get(key, "").strip()

    db.update_bottle_fields(bottle_id, fields, current_user.id)
    return redirect(url_for("collection") + f"#{target.get('product_key') or bottle_id}")


@app.route("/archive/<bottle_id>", methods=["POST"])
@login_required
def archive(bottle_id):
    status     = request.form.get("status", "").strip()
    sale_price = request.form.get("sale_price", "").strip() or None
    try:
        db.set_status(bottle_id, status, current_user.id, sale_price)
    except ValueError as e:
        return _render_collection(error=str(e), status_code=400)
    return redirect(url_for("collection"))


@app.route("/urls")
@login_required
def urls():
    return render_template_string(
        URLS_TEMPLATE,
        bottles=db.get_active_bottles(current_user.id),
        url_labels=URL_LABELS,
    )


@app.route("/update_urls", methods=["POST"])
@login_required
def update_urls():
    by_bottle = {}
    for form_key, value in request.form.items():
        if "__" not in form_key:
            continue
        bid, field = form_key.rsplit("__", 1)
        if field in URL_KEYS:
            by_bottle.setdefault(bid, {})[field] = value.strip()
    for bid, fields in by_bottle.items():
        try:
            db.update_bottle_fields(bid, fields, current_user.id)
        except ValueError:
            pass  # not this user's bottle (or unknown) — skip silently
    return redirect(url_for("urls"))


@app.route("/verify_url", methods=["POST"])
@login_required
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
.brand-header {
  display: flex; align-items: baseline; gap: 8px;
  font-family: Georgia, serif; font-size: 15px; font-weight: 700;
  color: #d4a574; letter-spacing: .5px; text-transform: uppercase;
  margin: 24px 2px 10px; padding-bottom: 6px;
  border-bottom: 1px solid rgba(212,165,116,.18);
}
.brand-header:first-of-type { margin-top: 6px; }
.brand-count {
  font-size: 11px; font-weight: 700; color: #a08770;
  background: rgba(212,165,116,.12); border: 1px solid rgba(212,165,116,.28);
  border-radius: 5px; padding: 1px 7px; letter-spacing: 0;
}
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
.edit-pane { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(212,165,116,.12); }
label.fld { display: block; font-size: 11px; color: #a08770;
  text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin: 12px 0 4px; }
input[type=text], input[type=number], input[type=email], input[type=password], select, textarea {
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


# Premium, invitation-only aesthetic for the PUBLIC pages (landing / login / signup).
# Cormorant Garamond (loaded via <link> in each template head) for the display type.
PREMIUM_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background:
    radial-gradient(1100px 560px at 50% -12%, rgba(201,165,104,.12), transparent 60%),
    radial-gradient(900px 700px at 50% 120%, rgba(201,165,104,.05), transparent 55%),
    #0b0705;
  color: #e8dcc8; min-height: 100vh; line-height: 1.65;
  display: flex; align-items: center; justify-content: center; padding: 40px 22px;
  -webkit-font-smoothing: antialiased;
}
.frame { width: 100%; max-width: 560px; text-align: center; }
.overline { font-size: 12px; letter-spacing: 6px; text-transform: uppercase; color: #c9a568; font-weight: 600; }
.wordmark {
  font-family: "Cormorant Garamond", Georgia, serif; font-weight: 600;
  font-size: 64px; line-height: 1.04; color: #f4e9d4; letter-spacing: .5px; margin-top: 16px;
  text-shadow: 0 2px 24px rgba(0,0,0,.5);
}
.rule { width: 62px; height: 1px; background: linear-gradient(90deg, transparent, #c9a568, transparent);
  margin: 28px auto; position: relative; }
.rule::after { content: "\\25C6"; position: absolute; top: -9px; left: 50%; transform: translateX(-50%);
  color: #c9a568; font-size: 10px; background: #0b0705; padding: 0 9px; }
.lead { font-family: "Cormorant Garamond", Georgia, serif; font-style: italic; font-size: 23px;
  color: #ddc9a6; margin-bottom: 12px; }
.sub { font-size: 15px; color: #9a8a72; max-width: 430px; margin: 0 auto 34px; }
.cta {
  display: inline-block; text-decoration: none; font-size: 12.5px; letter-spacing: 2.5px;
  text-transform: uppercase; font-weight: 700; color: #0b0705;
  background: linear-gradient(180deg, #e9d3a3, #c9a568);
  padding: 17px 36px; border-radius: 2px; box-shadow: 0 10px 34px rgba(201,165,104,.20);
}
.cta:hover { filter: brightness(1.06); }
.secondary { display: block; margin-top: 22px; font-size: 13px; letter-spacing: .4px; color: #9a8a72; }
.secondary a { color: #c9a568; text-decoration: none; border-bottom: 1px solid rgba(201,165,104,.4); padding-bottom: 1px; }
.footnote { margin-top: 48px; font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase; color: #6b5c46; }
.card { margin: 28px auto 0; max-width: 400px; text-align: left; background: rgba(20,14,9,.6);
  border: 1px solid rgba(201,165,104,.18); border-radius: 6px; padding: 26px 24px;
  box-shadow: 0 22px 60px rgba(0,0,0,.5); }
.card .fld { display: block; font-size: 11px; letter-spacing: 1.5px; text-transform: uppercase;
  color: #9a8a72; margin: 14px 0 6px; font-weight: 600; }
.card .fld:first-child { margin-top: 0; }
.card input { width: 100%; background: #0b0705; color: #e8dcc8; border: 1px solid rgba(201,165,104,.25);
  border-radius: 4px; padding: 13px 12px; font-size: 16px; -webkit-appearance: none; }
.card input:focus { outline: none; border-color: #c9a568; }
.btn { width: 100%; margin-top: 24px; border: none; cursor: pointer; font-size: 12.5px; letter-spacing: 2px;
  text-transform: uppercase; font-weight: 700; color: #0b0705;
  background: linear-gradient(180deg, #e9d3a3, #c9a568); padding: 16px; border-radius: 3px; }
.btn:hover { filter: brightness(1.06); }
.banner { background: rgba(90,30,30,.55); border: 1px solid rgba(232,134,134,.5); color: #f3c9c9;
  border-radius: 4px; padding: 11px 13px; margin-bottom: 6px; font-size: 13px; }
.alt { text-align: center; margin-top: 18px; font-size: 13px; color: #9a8a72; }
.alt a { color: #c9a568; }
@media (max-width: 480px) { .wordmark { font-size: 46px; } .lead { font-size: 20px; } }
"""

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

LANDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0705">
<title>Love My Bourbons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&display=swap" rel="stylesheet">
<style>""" + PREMIUM_CSS + """</style>
</head>
<body>
  <div class="frame">
    <div class="overline">Coming Soon</div>
    <h1 class="wordmark">Love My Bourbons</h1>
    <div class="rule"></div>
    <p class="lead">An invitation-only experience for a select few collectors.</p>
    <p class="sub">A private cellar for the bottles you treasure &mdash; and a quiet ledger of what they're worth.</p>
    {% if authenticated %}
      <a class="cta" href="{{ url_for('collection') }}">Enter your collection</a>
      <span class="secondary"><a href="{{ url_for('logout') }}">Sign out</a></span>
    {% else %}
      <a class="cta" href="{{ url_for('signup') }}">For the selected few &mdash; enter your access code</a>
      <span class="secondary">Already invited? <a href="{{ url_for('login') }}">Sign in</a></span>
    {% endif %}
    <div class="footnote">By invitation only</div>
  </div>
</body>
</html>"""

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0705">
<title>Sign in &middot; Love My Bourbons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&display=swap" rel="stylesheet">
<style>""" + PREMIUM_CSS + """</style>
</head>
<body>
  <div class="frame">
    <div class="overline">Members</div>
    <h1 class="wordmark">Love My Bourbons</h1>
    <div class="rule"></div>
    <div class="card">
      {% for m in flashes %}<div class="banner">{{ m }}</div>{% endfor %}
      {% if error %}<div class="banner">{{ error }}</div>{% endif %}
      <form method="post" action="{{ url_for('login') }}">
        <label class="fld">Email</label>
        <input type="email" name="email" autocomplete="username" required autofocus>
        <label class="fld">Password</label>
        <input type="password" name="password" autocomplete="current-password" required>
        <button type="submit" class="btn">Sign in</button>
      </form>
      <div class="alt">Have an access code? <a href="{{ url_for('signup') }}">Claim your invitation</a></div>
    </div>
    <div class="footnote">By invitation only</div>
  </div>
</body>
</html>"""

SIGNUP_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0705">
<title>You're Invited &middot; Love My Bourbons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&display=swap" rel="stylesheet">
<style>""" + PREMIUM_CSS + """</style>
</head>
<body>
  <div class="frame">
    <div class="overline">You've Been Selected</div>
    <h1 class="wordmark">Congratulations</h1>
    <div class="rule"></div>
    <p class="lead">You're among the first collectors invited to Love My Bourbons.</p>
    <p class="sub">A private cellar for your collection &mdash; and what it's worth. Claim your place below.</p>
    <div class="card">
      {% if error %}<div class="banner">{{ error }}</div>{% endif %}
      <form method="post" action="{{ url_for('signup') }}">
        <label class="fld">Access code</label>
        <input type="text" name="invite_code" autocomplete="off" required autofocus>
        <label class="fld">Email</label>
        <input type="email" name="email" autocomplete="username" required>
        <label class="fld">Password (min 8 characters)</label>
        <input type="password" name="password" autocomplete="new-password" required>
        <button type="submit" class="btn">Claim your place</button>
      </form>
      <div class="alt">Already a member? <a href="{{ url_for('login') }}">Sign in</a></div>
    </div>
    <div class="footnote">Love My Bourbons &middot; By invitation only</div>
  </div>
</body>
</html>"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a0f08">
<title>Bourbon Hunter · Collection</title>
<style>""" + BASE_CSS + """</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Love My Bourbons</h1>
    <div class="subtitle">{{ user_email }}</div>
  </header>

  <nav class="nav">
    <a href="{{ url_for('collection') }}" class="active">Bottles</a>
    <a href="{{ url_for('urls') }}">URL Tool</a>
    <a href="{{ url_for('logout') }}">Log out</a>
  </nav>

  {% if error %}<div class="banner">{{ error }}</div>{% endif %}

  {% if not brand_groups %}
    <div class="empty">No active bottles yet.<br>Add via photo intake, then edit here.</div>
  {% endif %}

""" + _ACTION_MACRO + """

  {% for section in brand_groups %}
  <div class="brand-header">{{ section.brand }} <span class="brand-count">{{ section.groups|length }}</span></div>
  {% for grp in section.groups %}
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
  {% endfor %}

  <footer>Your collection &middot; private to your account</footer>
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
    <h1>Love My Bourbons</h1>
    <div class="subtitle">Bulk URL Tool</div>
  </header>
  <nav class="nav">
    <a href="{{ url_for('collection') }}">Bottles</a>
    <a href="{{ url_for('urls') }}" class="active">URL Tool</a>
    <a href="{{ url_for('logout') }}">Log out</a>
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
    print("  Love My Bourbons — dev server")
    print(f"  http://127.0.0.1:{PORT}  (binding 0.0.0.0)")
    print("=" * 56)
    app.run(host=BIND_HOST, port=PORT, debug=False)
