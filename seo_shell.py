#!/usr/bin/env python3
"""Brand the HTML shell Streamlit serves, so search engines index Scanviction — not Streamlit.

THE PROBLEM
-----------
Streamlit ships a static `streamlit/static/index.html` and serves it for `/` (and as the
SPA fallback for every unmatched path). Out of the box it contains:

    <title>Streamlit</title>
    <noscript>You need to enable JavaScript to run this app.</noscript>

`st.set_page_config(page_title=...)` only rewrites the title in the DOM *after* the React
bundle boots, and every meta tag the app injects is likewise client-side. A crawler that
indexes the raw HTML response therefore files the site as:

    Streamlit
    Streamlit  You need to enable JavaScript to run this app.

...which is exactly what scanviction.com looked like in Google. Nothing in the app can fix
it, because the app has not run yet at the moment that HTML is served.

THE FIX
-------
Rewrite that file on disk at build time. Streamlit's Starlette static handler serves it with
`FileResponse` (read per request) and marks `*.html` no-cache, so the patch takes effect
immediately and is never served stale.

Safe to run repeatedly. The injected block is delimited, and the title/noscript rewrites are
pattern-based rather than content-based, so a re-run — including after a Streamlit upgrade
that changes the bundled asset hashes — always patches whatever index.html is currently on
disk. It never restores an older copy, so it cannot resurrect stale JS bundle references.

USAGE
    python seo_shell.py                              # uses $SITE_URL, else DEFAULT_SITE_URL
    python seo_shell.py --site-url https://scanviction.com
    python seo_shell.py --check                      # report status, change nothing

The origin is deliberately NOT taken from APP_URL. That variable is the deployment URL used
for Stripe redirects and email links, and on Render it is often the *.onrender.com host —
pointing rel=canonical at that host would tell Google the branded domain is the duplicate.
Override only via --site-url or $SITE_URL, both of which mean "the canonical public origin".
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys

# Bump when the injected content changes, so a rebuild visibly re-stamps the shell.
SEO_SHELL_VERSION = "1"
_START = f"<!-- SCANVICTION-SEO:START v{SEO_SHELL_VERSION} (seo_shell.py — do not edit by hand) -->"
_END = "<!-- SCANVICTION-SEO:END -->"
# Matches ANY version of our block so an upgrade replaces rather than stacks.
_BLOCK_RE = re.compile(
    r"[ \t]*<!-- SCANVICTION-SEO:START.*?<!-- SCANVICTION-SEO:END -->\n?",
    re.DOTALL,
)

DEFAULT_SITE_URL = "https://scanviction.com"

# Must match st.set_page_config(page_title=...) in app.py. If the crawler title and the
# rendered title disagree, the tab name changes under the visitor after boot — and a title
# that doesn't match the page it labels is the definition of a misleading snippet.
TITLE = "Scanviction | Spot Market Opportunities First"

# ~150 chars: what Google renders as the snippet. Same claim the landing page makes.
DESCRIPTION = (
    "Scanviction scores ~2,500 liquid U.S. stocks every session — price action, SEC insider "
    "buys and real short interest — into one 0–100 Conviction Score."
)

# Shown to crawlers and to anyone with JavaScript disabled. It must describe the real
# product and nothing more: serving crawlers content the app doesn't back up is cloaking.
NOSCRIPT_HTML = """<noscript>
      <div style="max-width:640px;margin:0 auto;padding:48px 24px;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#d1d9e6;background:#07090f;">
        <h1 style="font-size:28px;line-height:1.2;margin:0 0 16px;color:#f1f5f9;">Scanviction — every setup in the market, ranked by conviction</h1>
        <p style="font-size:15px;line-height:1.7;color:#8fa3bf;margin:0 0 16px;">
          Scanviction scores roughly 2,500 of the most liquid U.S. stocks across 23 signal
          categories every session, blending live price action, SEC insider filings, real
          FINRA short interest and money flow into a single 0–100 Conviction Score. The
          strongest setups surface first.
        </p>
        <p style="font-size:15px;line-height:1.7;color:#8fa3bf;margin:0 0 16px;">
          Signals are logged with a locked entry price and timestamp the moment they fire,
          then measured in their called direction — so the published track record is what
          the model actually did, not a backfill.
        </p>
        <p style="font-size:14px;line-height:1.7;color:#6b7fa0;margin:0 0 24px;">
          Scanviction is an interactive application and needs JavaScript enabled to run.
          Please turn on JavaScript and reload this page.
        </p>
        <p style="font-size:12px;line-height:1.6;color:#4a5e7a;margin:0;">
          Educational only — not financial advice. Past performance does not guarantee future
          results. Market data from Polygon.io, SEC EDGAR, FINRA and FRED.
        </p>
      </div>
    </noscript>"""


def _head_block(site_url: str) -> str:
    """The meta/link/JSON-LD block injected before </head>."""
    site = site_url.rstrip("/")
    og_image = f"{site}/app/static/icon-192.png"
    # Organization + WebSite only. No aggregateRating and no Offer: structured data that
    # asserts ratings or prices we can't evidence is a policy problem, not an SEO win.
    json_ld = (
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Organization","@id":"%(site)s/#org","name":"Scanviction",'
        '"url":"%(site)s/","logo":"%(img)s"},'
        '{"@type":"WebSite","@id":"%(site)s/#website","name":"Scanviction",'
        '"url":"%(site)s/","publisher":{"@id":"%(site)s/#org"},'
        '"description":"%(desc)s"}]}'
    ) % {"site": site, "img": og_image, "desc": DESCRIPTION}

    return f"""{_START}
    <meta name="description" content="{DESCRIPTION}" />
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1" />
    <meta name="application-name" content="Scanviction" />
    <meta name="author" content="Scanviction" />
    <link rel="canonical" href="{site}/" />

    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="Scanviction" />
    <meta property="og:title" content="{TITLE}" />
    <meta property="og:description" content="{DESCRIPTION}" />
    <meta property="og:url" content="{site}/" />
    <meta property="og:image" content="{og_image}" />
    <meta property="og:locale" content="en_US" />

    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{TITLE}" />
    <meta name="twitter:description" content="{DESCRIPTION}" />
    <meta name="twitter:image" content="{og_image}" />

    <script type="application/ld+json">{json_ld}</script>
{_END}"""


def find_index_html() -> str | None:
    """Absolute path to the installed Streamlit's static/index.html, or None."""
    try:
        import streamlit  # noqa: PLC0415  (deliberately lazy: keeps --check usable without it)
    except Exception:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(streamlit.__file__)),
                        "static", "index.html")
    return path if os.path.exists(path) else None


def patch_index_html(path: str, site_url: str) -> tuple[bool, str]:
    """Rewrite title/noscript and inject the SEO head block. Returns (changed, message)."""
    with open(path, encoding="utf-8") as f:
        original = f.read()

    html = _BLOCK_RE.sub("", original)          # drop any previous block (this or older version)

    # Title: match ANY current title so the rewrite survives a Streamlit upgrade.
    html, n_title = re.subn(r"<title>.*?</title>", f"<title>{TITLE}</title>", html,
                            count=1, flags=re.DOTALL)
    if not n_title:
        return False, f"no <title> found in {path} — Streamlit's shell changed shape; not patched"

    html, n_noscript = re.subn(r"<noscript>.*?</noscript>", lambda _m: NOSCRIPT_HTML, html,
                               count=1, flags=re.DOTALL)
    if not n_noscript:
        return False, f"no <noscript> found in {path} — not patched"

    if "</head>" not in html:
        return False, f"no </head> found in {path} — not patched"
    html = html.replace("</head>", _head_block(site_url) + "\n  </head>", 1)

    if html == original:
        return False, "already up to date"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return True, f"patched {path}"


def write_root_files(static_dir: str, site_url: str) -> list[str]:
    """robots.txt + sitemap.xml at the site root.

    Streamlit's static handler serves this directory at `/`, so files dropped here are
    reachable as https://<host>/robots.txt — the app's own ./static/ dir is NOT, since
    enableStaticServing mounts it under /app/static/.
    """
    site = site_url.rstrip("/")
    today = _dt.date.today().isoformat()
    written = []

    robots = (
        "User-agent: *\n"
        "Allow: /\n"
        "# Streamlit's internal transport endpoints — no indexable content.\n"
        "Disallow: /_stcore/\n"
        "Disallow: /vendor/\n"
        f"\nSitemap: {site}/sitemap.xml\n"
    )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url>\n    <loc>{site}/</loc>\n    <lastmod>{today}</lastmod>\n"
        "    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n  </url>\n"
        "</urlset>\n"
    )
    for name, body in (("robots.txt", robots), ("sitemap.xml", sitemap)):
        dest = os.path.join(static_dir, name)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)
        written.append(dest)
    return written


def apply(site_url: str | None = None, quiet: bool = False) -> bool:
    """Patch the shell. Returns True on success. Never raises — callers can fire and forget."""
    try:
        # SITE_URL only — never APP_URL (see the module docstring: it can be the
        # *.onrender.com host, and a canonical pointing there demotes the real domain).
        site = site_url or os.environ.get("SITE_URL") or DEFAULT_SITE_URL
        path = find_index_html()
        if not path:
            if not quiet:
                print("[seo-shell] streamlit static/index.html not found — skipped")
            return False
        changed, msg = patch_index_html(path, site)
        files = write_root_files(os.path.dirname(path), site)
        if not quiet:
            print(f"[seo-shell] {msg}")
            print(f"[seo-shell] wrote {', '.join(os.path.basename(f) for f in files)}"
                  f" (served at {site.rstrip('/')}/robots.txt, /sitemap.xml)")
        return changed or msg == "already up to date"
    except Exception as e:                     # never let SEO cosmetics break a deploy/boot
        if not quiet:
            print(f"[seo-shell] skipped: {type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-url", default=None,
                    help=f"canonical public origin (default: $SITE_URL, else {DEFAULT_SITE_URL})")
    ap.add_argument("--check", action="store_true",
                    help="report whether the shell is patched; change nothing")
    args = ap.parse_args()

    path = find_index_html()
    if not path:
        print("[seo-shell] streamlit static/index.html not found")
        return 1
    if args.check:
        head = open(path, encoding="utf-8").read()
        title = re.search(r"<title>(.*?)</title>", head, re.DOTALL)
        print(f"  index.html : {path}")
        print(f"  title      : {title.group(1) if title else '(none)'}")
        print(f"  seo block  : {'present' if 'SCANVICTION-SEO:START' in head else 'ABSENT'}")
        print(f"  description: {'present' if 'name=\"description\"' in head else 'ABSENT'}")
        static_dir = os.path.dirname(path)
        for f in ("robots.txt", "sitemap.xml"):
            print(f"  {f:11s}: {'present' if os.path.exists(os.path.join(static_dir, f)) else 'ABSENT'}")
        return 0
    return 0 if apply(args.site_url) else 1


if __name__ == "__main__":
    sys.exit(main())
