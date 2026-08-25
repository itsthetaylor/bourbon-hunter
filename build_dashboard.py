"""
build_dashboard.py — generate the PUBLIC GitHub Pages page: docs/index.html.

PRIVACY: as of the multi-user migration this page shows NO bottle data. It is a
static marketing landing page ("Love My Bourbons") with a link into the hosted app,
where all collections live behind login. It does NOT read the database at all — so
there is no way for collection data to leak onto the public GitHub Pages URL.

The "Open the app" link points at LMB_APP_URL (set it to your Railway app URL); it
falls back to a placeholder if unset.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "index.html"

# The hosted app's public URL (Railway). Set LMB_APP_URL in your env once known.
APP_URL = os.getenv("LMB_APP_URL", "https://your-app.up.railway.app")


def render_landing(app_url):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#1a0f08">
<title>Love My Bourbons</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #f4e4c1; min-height: 100vh; line-height: 1.6; background: #0a0604;
    display: flex; align-items: center; justify-content: center; padding: 24px;
}}
.wrap {{ max-width: 560px; text-align: center; }}
h1 {{
    font-family: Georgia, serif; font-size: 46px; font-weight: 700; color: #d4a574;
    letter-spacing: 1px; text-shadow: 0 2px 10px rgba(0,0,0,.6); margin-bottom: 16px;
}}
.tagline {{ color: #c9b896; font-size: 18px; font-style: italic; margin-bottom: 36px; }}
.cta a {{
    display: inline-block; text-decoration: none; padding: 15px 34px; border-radius: 12px;
    font-weight: 700; font-size: 16px; background: #5a3318; color: #f4e4c1;
    border: 1px solid rgba(212,165,116,.5);
}}
.note {{ color: #6b5544; font-size: 12px; margin-top: 28px; letter-spacing: .5px; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>Love My Bourbons</h1>
    <div class="tagline">Track your whiskey collection &middot; know what it's worth.</div>
    <div class="cta"><a href="{app_url}">Open the app</a></div>
    <div class="note">Private beta &middot; invite only. Collections are visible only after you log in.</div>
  </div>
</body>
</html>"""


def build():
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(render_landing(APP_URL))
    print(f"Landing page generated: {OUTPUT_FILE}")
    print(f"  App link -> {APP_URL}  (set LMB_APP_URL to change)")
    print("  No bottle data is included on this public page.")


def main():
    build()


if __name__ == "__main__":
    main()
