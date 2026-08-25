"""
build_dashboard.py — generate the PUBLIC GitHub Pages page: docs/index.html.

PRIVACY: this page shows NO bottle data and never reads the database. It is a
premium, invitation-only "coming soon" landing for Love My Bourbons. All
collections live behind login in the hosted app; there is no way for collection
data to reach this public URL.

The access-code / sign-in links point at LMB_APP_URL (your Railway app URL / the
app's custom domain). Set LMB_APP_URL in your env; it falls back to a placeholder.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "index.html"

APP_URL = os.getenv("LMB_APP_URL", "https://your-app.up.railway.app").rstrip("/")

# Plain (non-f) template; %%APP_URL%% is substituted below so CSS braces stay clean.
LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0b0705">
<title>Love My Bourbons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&display=swap" rel="stylesheet">
<style>
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
  font-size: 68px; line-height: 1.02; color: #f4e9d4; letter-spacing: .5px; margin-top: 16px;
  text-shadow: 0 2px 24px rgba(0,0,0,.5);
}
.rule { width: 62px; height: 1px; background: linear-gradient(90deg, transparent, #c9a568, transparent);
  margin: 30px auto; position: relative; }
.rule::after { content: "\\25C6"; position: absolute; top: -9px; left: 50%; transform: translateX(-50%);
  color: #c9a568; font-size: 10px; background: #0b0705; padding: 0 9px; }
.lead { font-family: "Cormorant Garamond", Georgia, serif; font-style: italic; font-size: 24px;
  color: #ddc9a6; margin-bottom: 12px; }
.sub { font-size: 15px; color: #9a8a72; max-width: 430px; margin: 0 auto 38px; }
.cta {
  display: inline-block; text-decoration: none; font-size: 12.5px; letter-spacing: 2.5px;
  text-transform: uppercase; font-weight: 700; color: #0b0705;
  background: linear-gradient(180deg, #e9d3a3, #c9a568);
  padding: 17px 36px; border-radius: 2px; box-shadow: 0 10px 34px rgba(201,165,104,.20);
}
.cta:hover { filter: brightness(1.06); }
.secondary { display: block; margin-top: 22px; font-size: 13px; letter-spacing: .4px; color: #9a8a72; }
.secondary a { color: #c9a568; text-decoration: none; border-bottom: 1px solid rgba(201,165,104,.4); padding-bottom: 1px; }
.footnote { margin-top: 50px; font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase; color: #6b5c46; }
@media (max-width: 480px) { .wordmark { font-size: 48px; } .lead { font-size: 21px; } }
</style>
</head>
<body>
  <div class="frame">
    <div class="overline">Coming Soon</div>
    <h1 class="wordmark">Love My Bourbons</h1>
    <div class="rule"></div>
    <p class="lead">An invitation-only experience for a select few collectors.</p>
    <p class="sub">A private cellar for the bottles you treasure &mdash; and a quiet ledger of what they're worth.</p>
    <a class="cta" href="%%APP_URL%%/signup">For the selected few &mdash; enter your access code</a>
    <span class="secondary">Already invited? <a href="%%APP_URL%%/login">Sign in</a></span>
    <div class="footnote">By invitation only</div>
  </div>
</body>
</html>"""


def render_landing(app_url):
    return LANDING_HTML.replace("%%APP_URL%%", app_url)


def build():
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(render_landing(APP_URL))
    print(f"Landing page generated: {OUTPUT_FILE}")
    print(f"  Access-code / sign-in links -> {APP_URL}  (set LMB_APP_URL to change)")
    print("  No bottle data is included on this public page.")


def main():
    build()


if __name__ == "__main__":
    main()
