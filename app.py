# ═══════════════════════════════════════════════════════════════
# Scanviction v7.0 — Premium Fintech SaaS
# "I trust this. I understand this. I want more."
# ═══════════════════════════════════════════════════════════════
# This file is the Streamlit UI + routing + warm-universe worker. Most logic now
# lives in focused modules imported below (and kept under the SAME names here):
#   scoring · signal_engine · kvstore · msp_store · recs_store · auth_store
#   perf_eval · advice · security · theme · market_utils · {polygon,edgar,fred}_adapter
# See ARCHITECTURE.md for the module map, data flow, storage model, and how to test.

import streamlit as st
import streamlit.components.v1 as components
import requests, pandas as pd, ta
import hashlib, time, random, math, sys, os, threading
import secrets as _secrets
from datetime import datetime, timedelta

# ── Secrets → environment bridge (MUST run before any storage import below) ──
# On Render every credential lives in the secrets.toml Secret File (st.secrets) —
# but kvstore / msp_store / analytics_store read DATABASE_URL (and friends) from
# os.environ AT IMPORT TIME. Streamlit does NOT export secrets as env vars, so
# unless DATABASE_URL was ALSO set as a dashboard env var, the DB URL silently
# never reached the storage layer: it fell back to JSON files on the container's
# EPHEMERAL disk, and every deploy wiped all accounts, sessions and history
# ("the site stopped saving my account"). Env vars set in the dashboard still
# win — this only fills the gap when the value exists solely in the secret file.
for _bridge_key in ("DATABASE_URL", "STORAGE_DUAL_WRITE", "MSP_DATA_DIR"):
    if not os.environ.get(_bridge_key):
        try:
            _bridge_val = st.secrets.get(_bridge_key, "") or ""
        except Exception:
            _bridge_val = ""
        if _bridge_val:
            os.environ[_bridge_key] = str(_bridge_val)

# Security primitives (password hashing + HTML escaping) live in security.py so the
# auth-critical code is isolated and unit-testable without importing this monolith.
# (verify_pw handles bcrypt + legacy sha256; _esc escapes user text for HTML sinks.)
from security import _esc, _hp, hp, _is_bcrypt_hash, verify_pw, HAS_BCRYPT
import secguard   # abuse guards: signup honeypot + rate limits, login lockout, reset throttling
import seo_export  # writes the slim universe snapshot the standalone SEO site generator reads
import analytics_store  # durable Postgres-backed funnel events (survive Cloud reboots); JSONL fallback

# Cookie manager DISABLED. stx.CookieManager mounts an iframe that re-syncs to
# Python on nearly every run, causing a continuous rerun loop — each rerun marks
# the prior frame data-stale (hidden by our CSS), so the whole page (navbar
# included) blanks and "falls in from the top" repeatedly. Session persistence
# is fully handled by the ?sid= URL token (survives F5/hard-refresh/in-app nav),
# so we drop the cookie path entirely.
HAS_COOKIES = False
try:
    import extra_streamlit_components as stx
except Exception:
    stx = None

# Polygon.io (Massive) bulk market-data adapter — powers the whole-market scan
# (grouped-daily history + snapshot quotes for thousands of liquid tickers in a
# handful of calls). Optional: if the module or POLYGON_API_KEY is absent, the
# universe builder transparently falls back to the legacy yfinance/FMP path so
# the app always works.
try:
    import polygon_adapter as _poly
    HAS_POLYGON = True
except Exception:
    _poly = None
    HAS_POLYGON = False

# SEC EDGAR adapter (FREE, keyless) — adds two signals Polygon's Starter plan can't
# give us: fresh 8-K material-event catalysts and open-market insider PURCHASES
# (Form 4, code P) that power the Insider Cluster category. Optional + fully
# graceful: if it's missing or SEC is unreachable, those signals just stay empty.
try:
    import edgar_adapter as _edgar
    HAS_EDGAR = True
except Exception:
    _edgar = None
    HAS_EDGAR = False

# FRED macro adapter (FREE, keyless) — distills VIX + high-yield credit spread + the
# yield curve into a plain-English MARKET REGIME (Risk-On / Neutral / Risk-Off) shown
# as backdrop on Discover, so a signal is read in context. Optional + graceful.
try:
    import fred_adapter as _fred
    HAS_FRED = True
except Exception:
    _fred = None
    HAS_FRED = False

# Shared scoring & categorization engine (pure pandas/ta logic). Defined ONCE in
# scoring.py and imported by BOTH this app and the alerts worker so Discover and
# the notifications agree on exactly how stocks are scored and categorized.
from scoring import (
    compute_scores, compute_factors, precompute_indicators, assign_categories, category_for_feat,
    _feat_from_row, _category_why, _cl, conviction_score, COMPOSITE_FIT, COMPOSITE_CATS,
    CATEGORY_MIN_FIT, SENT_MIN_MSGS, SENT_FULL_MSGS,
    COMPOSITE_DIR, category_dir, bear_conviction, category_edge,
)

# Optional page auto-refresh helper. NO LONGER used on Discover — its periodic
# refresh is now a fragment-scoped run_every (updates the body in place, no
# full-page rerun, no flash). Kept as a guarded import for any page that still
# wants a timed full reload.
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False
    def st_autorefresh(*a, **k): return 0

def _cookie_manager():
    """Return a per-session CookieManager. We must NOT create it inside an
    st.cache_* function (the manager registers a widget, which Streamlit forbids
    in cached functions), so we instantiate once per session and stash it in
    session_state."""
    if not HAS_COOKIES:
        return None
    cm = st.session_state.get("_cookie_mgr")
    if cm is None:
        try:
            cm = stx.CookieManager(key="msp_cookie_mgr")
            st.session_state["_cookie_mgr"] = cm
        except Exception:
            return None
    return cm

MSP_COOKIE = "msp_sid"

# Signal Performance Engine
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from signal_engine import (
        record_signal_event, record_signal_events_bulk, update_signal_outcomes,
        get_ticker_signal_history,
        get_recent_signal_events, get_category_performance_stats,
        calculate_pnl, estimate_options_pnl, compute_confidence,
        detect_market_regime, seed_demo_signal_history, purge_demo_signal_history
    )
    HAS_SIGNAL_ENGINE = True
except Exception as _se:
    HAS_SIGNAL_ENGINE = False
    def record_signal_event(*a, **kw): return {}
    def record_signal_events_bulk(*a, **kw): return []
    def get_ticker_signal_history(t, **kw): return []
    def get_recent_signal_events(**kw): return []
    def get_category_performance_stats(): return {}
    def calculate_pnl(inv, tp, cp, direction="long"):
        if tp <= 0: return {}
        pct = ((cp - tp) / tp) * 100 * (1 if direction=="long" else -1)
        pnl = inv * (pct/100)
        return {"pnl_usd": round(pnl,2), "pnl_pct": round(pct,2), "current_value": round(inv+pnl,2),
                "shares": round(inv/tp,4), "price_change": round(cp-tp,2), "pct_change": round((cp-tp)/tp*100,2)}
    def estimate_options_pnl(*a, **kw): return {}
    def compute_confidence(*a, **kw): return {"confidence":"N/A","risk":"Unknown","factors":[],"score":50}
    def detect_market_regime(*a, **kw): return {"regime":"mixed","label":"⚖️ Mixed","description":"","best_strategies":[]}
    def seed_demo_signal_history(*a, **k): pass
    def purge_demo_signal_history(*a, **k): return 0

# ── Native Web Push (VAPID, self-hosted — replaces OneSignal) ──
# webpush.py is pure/env-driven so the worker thread can use it; mirror the VAPID
# secrets into env here (same pattern as the Polygon/Telegram key capture).
try:
    import webpush as _webpush
    import os as _os_wp
    for _k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_CLAIMS_EMAIL"):
        try:
            _v = st.secrets.get(_k, "")
            if _v and not _os_wp.environ.get(_k):
                _os_wp.environ[_k] = str(_v)
        except Exception:
            pass
    HAS_WEBPUSH_NATIVE = _webpush.configured()
except Exception:
    _webpush = None
    HAS_WEBPUSH_NATIVE = False

try:
    import io as _io
    import xlsxwriter as _xlsxwriter
    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False

try:
    import plotly.graph_objects as go
    import plotly.express as px
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# Brand favicon (the indigo→violet signal mark) instead of a generic emoji. Falls back
# to the emoji if the asset is missing (e.g. a partial deploy).
_FAVICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.svg")
st.set_page_config(
    page_title="Scanviction | Spot Market Opportunities First",
    page_icon=(_FAVICON if os.path.exists(_FAVICON) else "📈"), layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Crawler-visible branding (belt-and-braces) ────────────────────────────────
# set_page_config above only renames the tab AFTER the JS bundle boots. The HTML a crawler
# is served for "/" comes from Streamlit's own static/index.html, which says
# "<title>Streamlit</title>" and "You need to enable JavaScript to run this app." — and that
# is exactly what Google indexed us as. seo_shell.py rewrites that file; render.yaml runs it
# at build time, which is the right place. This call is the fallback for deployments that
# don't use the blueprint's buildCommand (Render ignores render.yaml for services created by
# hand — see the warning at the top of that file). cache_resource makes it once per process,
# not once per script rerun, and the patch itself is idempotent and never raises.
@st.cache_resource(show_spinner=False)
def _brand_html_shell():
    try:
        import seo_shell
        return seo_shell.apply(quiet=False)
    except Exception:
        return False
_brand_html_shell()

_BASE_CSS = None
def _inject_base_css():
    """Inject styles.css (the static global base stylesheet) once. Read is cached.
    Uses string concat, NOT an f-string — the CSS contains literal { } braces."""
    global _BASE_CSS
    if _BASE_CSS is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles.css"), encoding="utf-8") as _f:
                _BASE_CSS = _f.read()
        except Exception:
            _BASE_CSS = ""
    st.markdown("<style>" + _BASE_CSS + "</style>", unsafe_allow_html=True)
_inject_base_css()

# ─────────────────────────────────────────────────────────────
# PROGRESSIVE WEB APP (PWA) — Native app experience
# ─────────────────────────────────────────────────────────────
# Embedded SVG icon (no external hosting needed) — Scanviction logo as SVG → base64
_SW_ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#4f46e5"/><stop offset="1" stop-color="#6366f1"/>
</linearGradient></defs>
<rect width="512" height="512" rx="96" fill="url(#g)"/>
<path d="M120 340 L120 380 L392 380 L392 340 L120 340 Z" fill="#fff" opacity=".3"/>
<path d="M140 320 L200 240 L240 280 L320 160 L380 220 L380 240 L320 200 L240 320 L200 280 L160 340 Z" fill="#fff"/>
<circle cx="380" cy="220" r="14" fill="#f59e0b"/>
<text x="256" y="450" text-anchor="middle" fill="#fff" font-family="Inter,sans-serif" font-size="32" font-weight="900">MSP</text>
</svg>'''

import base64 as _b64
_icon_b64 = _b64.b64encode(_SW_ICON_SVG.encode()).decode()
_icon_data_uri = f"data:image/svg+xml;base64,{_icon_b64}"

PWA_MANIFEST_JSON = (
    '{"name":"Scanviction — Premium Stock Intelligence",'
    '"short_name":"Scanviction",'
    '"description":"Proprietary stock signals, composite categories, and signal track record.",'
    '"start_url":"/",'
    '"display":"standalone",'
    '"orientation":"portrait",'
    '"background_color":"#07090f",'
    '"theme_color":"#6366f1",'
    '"categories":["finance","business","productivity"],'
    '"icons":[{"src":"' + _icon_data_uri + '","sizes":"512x512","type":"image/svg+xml","purpose":"any maskable"}]'
    '}'
)
_manifest_data_uri = "data:application/json;base64," + _b64.b64encode(PWA_MANIFEST_JSON.encode()).decode()

# ── Inject PWA head tags + native-feeling polish ──
st.markdown(f"""
<link rel="manifest" href="{_manifest_data_uri}">
<link rel="icon" type="image/svg+xml" href="{_icon_data_uri}">
<link rel="apple-touch-icon" href="{_icon_data_uri}">
<meta name="theme-color" content="#6366f1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Scanviction">
<meta name="msapplication-TileColor" content="#6366f1">
<meta name="msapplication-navbutton-color" content="#6366f1">
<meta name="format-detection" content="telephone=no">


<!-- Splash screen (visible only when launched as installed app) -->
<div id="sw-pwa-splash">
    <div id="sw-pwa-splash-logo">SW</div>
    <div id="sw-pwa-splash-title">Scanviction</div>
    <div id="sw-pwa-splash-tagline">Loading market intelligence…</div>
</div>

<!-- Offline indicator -->
<div id="sw-offline-banner">⚠️ You're offline — some features may not work</div>

<!-- Custom install banner (Android/Chrome) -->
<div id="sw-install-banner">
    <div id="sw-install-banner-icon">📲</div>
    <div id="sw-install-banner-text">
        <strong>Install Scanviction</strong>
        <span>Add to your home screen for instant access</span>
    </div>
    <button id="sw-install-banner-btn">Install</button>
    <button id="sw-install-banner-close" aria-label="Dismiss">×</button>
</div>

<script>
(function() {{
    // ── Hide splash after page loads ──
    setTimeout(() => {{
        const splash = document.getElementById('sw-pwa-splash');
        if (splash) {{
            splash.classList.add('hidden');
            setTimeout(() => splash.remove(), 600);
        }}
    }}, 800);

    // ── Online/Offline detection ──
    const offlineBanner = document.getElementById('sw-offline-banner');
    function updateOnlineStatus() {{
        if (!navigator.onLine) {{
            offlineBanner.classList.add('visible');
        }} else {{
            offlineBanner.classList.remove('visible');
        }}
    }}
    window.addEventListener('online', updateOnlineStatus);
    window.addEventListener('offline', updateOnlineStatus);
    updateOnlineStatus();

    // ── Install Prompt Handling (Android/Chrome desktop) ──
    let deferredPrompt = null;
    const installBanner = document.getElementById('sw-install-banner');
    const installBtn = document.getElementById('sw-install-banner-btn');
    const installClose = document.getElementById('sw-install-banner-close');

    // Don't show again for 14 days if dismissed
    const dismissedAt = localStorage.getItem('sw_install_dismissed');
    const SHOULD_SUPPRESS = dismissedAt && (Date.now() - parseInt(dismissedAt) < 14 * 24 * 60 * 60 * 1000);

    window.addEventListener('beforeinstallprompt', (e) => {{
        e.preventDefault();
        deferredPrompt = e;
        if (!SHOULD_SUPPRESS) {{
            // Wait a few seconds before showing (don't jump-scare on load)
            setTimeout(() => {{
                installBanner.classList.add('visible');
            }}, 4500);
        }}
    }});

    installBtn.addEventListener('click', async () => {{
        if (!deferredPrompt) return;
        installBanner.classList.remove('visible');
        deferredPrompt.prompt();
        const {{ outcome }} = await deferredPrompt.userChoice;
        if (outcome === 'accepted') {{
            console.log('Scanviction installed!');
        }} else {{
            localStorage.setItem('sw_install_dismissed', Date.now().toString());
        }}
        deferredPrompt = null;
    }});

    installClose.addEventListener('click', () => {{
        installBanner.classList.remove('visible');
        localStorage.setItem('sw_install_dismissed', Date.now().toString());
    }});

    // Hide install banner if app gets installed
    window.addEventListener('appinstalled', () => {{
        installBanner.classList.remove('visible');
        localStorage.removeItem('sw_install_dismissed');
    }});

    // ── iOS Safari install hint (no beforeinstallprompt support) ──
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
    const isInStandaloneMode = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
    if (isIOS && !isInStandaloneMode && !SHOULD_SUPPRESS) {{
        setTimeout(() => {{
            const banner = document.getElementById('sw-install-banner');
            const text = document.getElementById('sw-install-banner-text');
            text.innerHTML = '<strong>Install Scanviction</strong><span>Tap Share → Add to Home Screen</span>';
            installBtn.textContent = 'Got it';
            installBtn.addEventListener('click', () => {{
                banner.classList.remove('visible');
                localStorage.setItem('sw_install_dismissed', Date.now().toString());
            }});
            banner.classList.add('visible');
        }}, 5000);
    }}

    // ── Service Worker registration for offline support ──
    // Uses a tiny inline SW served as data: URI (since Streamlit has no static root)
    if ('serviceWorker' in navigator) {{
        const swCode = `
            const CACHE_NAME = 'msp-v1';
            self.addEventListener('install', e => self.skipWaiting());
            self.addEventListener('activate', e => e.waitUntil(clients.claim()));
            self.addEventListener('fetch', e => {{
                // Network-first strategy (Streamlit content is dynamic)
                if (e.request.method !== 'GET') return;
                e.respondWith(
                    fetch(e.request).catch(() => caches.match(e.request))
                );
            }});
        `;
        const blob = new Blob([swCode], {{ type: 'application/javascript' }});
        const swUrl = URL.createObjectURL(blob);
        navigator.serviceWorker.register(swUrl).catch(() => {{}});
    }}

    // ── SIDEBAR COLLAPSE BUTTON WATCHDOG ──
    // Streamlit aggressively hides the collapse control via inline styles after state changes.
    // We mutation-observe and force it visible. This runs continuously.
    function ensureSidebarToggleVisible() {{
        const selectors = [
            '[data-testid="collapsedControl"]',
            '[data-testid="stSidebarCollapseButton"]',
            '[data-testid="stSidebarCollapsedControl"]'
        ];
        selectors.forEach(sel => {{
            document.querySelectorAll(sel).forEach(el => {{
                // Strip inline display:none / visibility:hidden that Streamlit injects
                if (el.style.display === 'none')        el.style.display = '';
                if (el.style.visibility === 'hidden')   el.style.visibility = '';
                if (el.style.opacity === '0')           el.style.opacity = '';
                el.removeAttribute('hidden');
            }});
        }});
    }}
    // Run immediately, then every 400ms (cheap, catches Streamlit reruns)
    setInterval(ensureSidebarToggleVisible, 400);
    ensureSidebarToggleVisible();

    // Also observe DOM mutations to catch fresh insertions instantly
    const sidebarObserver = new MutationObserver(ensureSidebarToggleVisible);
    sidebarObserver.observe(document.body, {{ childList: true, subtree: true, attributes: true, attributeFilter: ['style','class','aria-expanded'] }});

    // ── MOBILE: Close sidebar when user taps outside it ──
    document.addEventListener('click', (e) => {{
        if (window.innerWidth > 992) return; // only mobile
        const sidebar = document.querySelector('[data-testid="stSidebar"]');
        if (!sidebar || sidebar.getAttribute('aria-expanded') !== 'true') return;
        const isInside = sidebar.contains(e.target);
        const isToggle = e.target.closest('[data-testid="collapsedControl"], [data-testid="stSidebarCollapseButton"]');
        if (!isInside && !isToggle) {{
            const btn = document.querySelector('[data-testid="stSidebarCollapseButton"]');
            if (btn) btn.click();
        }}
    }}, true);
}})();
</script>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# SECURITY
# ─────────────────────────────────────────────────────────────
# Password hashing (bcrypt + legacy sha256 verify) and HTML escaping moved to
# security.py and are imported at the top of this file: _hp, hp, _is_bcrypt_hash,
# verify_pw, _esc, HAS_BCRYPT. Covered by tests/test_security.py + test_app_security.py.

# ─────────────────────────────────────────────────────────────
# FILE-BASED PERSISTENCE (alerts + users readable by worker)
# ─────────────────────────────────────────────────────────────
import json as _json, os as _os

# Default data directory. We deliberately AVOID /tmp by default because /tmp is
# wiped on every container restart on most hosts (Streamlit Community Cloud,
# Heroku, etc.) — which is why registered accounts kept disappearing on reboot.
# A directory next to the app survives a normal process restart on hosts that
# preserve the working dir, and on truly ephemeral hosts the real fix is
# DATABASE_URL (see below) — but this is a strictly better default than /tmp.
# Override with MSP_DATA_DIR or the individual *_DB_PATH env vars.
_DEFAULT_DATA_DIR = _os.environ.get(
    "MSP_DATA_DIR",
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".msp_data")
)
try:
    _os.makedirs(_DEFAULT_DATA_DIR, exist_ok=True)
except Exception:
    _DEFAULT_DATA_DIR = "/tmp"  # last resort if the app dir isn't writable

def _data_path(name, env_var):
    return _os.environ.get(env_var, _os.path.join(_DEFAULT_DATA_DIR, name))

# ── Lightweight funnel analytics ─────────────────────────────────────────────
# One JSON line per funnel event appended to .msp_data/events.jsonl. No external SDK / no dashboard
# dependency — the founder can tail or aggregate it (`landing_view → signup_verified → login_success
# → payment_success`), or later ship it onward. Emails are stored HASHED so the log stays
# low-sensitivity, and track_event NEVER raises — analytics must never break a user flow.
_EVENTS_PATH = _data_path("events.jsonl", "MSP_EVENTS_PATH")
_EVENTS_LOCK = threading.Lock()
def track_event(event, props=None):
    try:
        import json as _j
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": str(event)}
        uid = None; extra = {}
        for k, v in (props or {}).items():
            if k == "email" and v:
                uid = hashlib.sha256(str(v).strip().lower().encode()).hexdigest()[:16]
                rec["uid"] = uid
            elif v is not None:
                rec[k] = v; extra[k] = v
        # Durable: Postgres when DATABASE_URL is set (survives Streamlit Cloud reboots).
        try: analytics_store.record_event(str(event), uid=uid, props=extra)
        except Exception: pass
        # Local JSONL — the fallback when there's no DB, and a belt-and-suspenders log.
        with _EVENTS_LOCK:
            with open(_EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(_j.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def track_once(flag, event, props=None):
    """Fire an event at most once per session (guarded by a session_state flag) — for funnel-top or
    return-page events that would otherwise re-fire on every Streamlit rerun."""
    try:
        if st.session_state.get(flag):
            return
        st.session_state[flag] = True
    except Exception:
        pass
    track_event(event, props)

def read_funnel_events(limit=200000):
    """Aggregate funnel events into {event: {"n": total, "u": unique_uids}} + the most recent rows.
    Read-only + best-effort (never raises). Powers the owner funnel view. Prefers durable Postgres
    (survives reboots); falls back to the local events.jsonl when there's no DB."""
    try:
        fc = analytics_store.funnel_counts()
        if fc is not None:                       # DB is up (even if empty) — use it
            return fc, analytics_store.recent_events(40)
    except Exception:
        pass
    import json as _j
    counts, uniq, recent = {}, {}, []
    try:
        with open(_EVENTS_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = _j.loads(line)
            except Exception:
                continue
            ev = e.get("event", "?")
            counts[ev] = counts.get(ev, 0) + 1
            u = e.get("uid")
            if u:
                uniq.setdefault(ev, set()).add(u)
        for line in reversed(lines[-40:]):
            try:
                recent.append(_j.loads(line.strip()))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {ev: {"n": counts[ev], "u": len(uniq.get(ev, ()))} for ev in counts}, recent

# ALERTS_DB_PATH / USERS_DB_PATH / SESS_DB_PATH and SESSION_TTL_SECONDS now come from
# auth_store (imported below); it sources the paths from msp_store so the app and the
# alerts worker share the exact same files/rows.

# ─────────────────────────────────────────────────────────────
# STORAGE BACKEND  (Phase A migration: JSON files → Postgres)
# ─────────────────────────────────────────────────────────────
# All persistence routes through _read_json / _write_json, keyed by a "path"
# string. That single seam lets us swap the physical store without touching any
# caller. When DATABASE_URL is set AND a driver is available, each logical
# "file" becomes one row in a kv_store(key TEXT PK, value JSONB) table; we
# OPTIONALLY dual-write to the JSON files too (STORAGE_DUAL_WRITE=1) during
# cutover for safety. Otherwise we use the JSON files exactly as before.
#
# This is intentionally a key→document mapping (not the fully normalized schema
# in DATABASE_SCHEMA.md). It gets the app onto a real, shared, durable database
# immediately — solving the /tmp-ephemerality and multi-replica problems — and
# the normalized tables can be introduced table-by-table afterward behind the
# same helpers. See DATABASE_SCHEMA.md, migration Phases A–D.
# The key->document storage seam (Postgres kv_store OR atomic JSON files) lives in
# kvstore.py — a pure, independently-testable module. app.py keeps the SAME names:
# _read_json / _write_json route every store op; _db_read / _db_write back the
# Twelve-Data usage counter; storage_backend() drives the diagnostics page.
from kvstore import (
    DATABASE_URL, STORAGE_DUAL_WRITE, storage_backend,
    _db_read, _db_write, _read_json, _write_json,
)

# Account & session persistence — users/alerts/sessions/pending-upgrades, the shared
# users DB + seed accounts, and the process-wide _STORE_LOCK — lives in auth_store.py.
# app.py keeps the SAME names via this import; the st.session_state-coupled callers
# (_toggle_watchlist, login/signup/settings UI) stay here and use these.
from auth_store import (
    SESSION_TTL_SECONDS, USERS_DB_PATH, ALERTS_DB_PATH, SESS_DB_PATH, _STORE_LOCK,
    save_alerts_to_file, save_user_to_file, load_all_users_from_file,
    _load_sessions, _save_sessions, _prune_sessions,
    new_session_token, lookup_session, destroy_session_token,
    _pending_upgrades_path, remember_pending_upgrade, apply_pending_upgrade,
    _get_global_db, _save_global_db, _load_seed_accounts,
)

def _toggle_watchlist(ticker):
    """Add/remove a ticker from the current user's watchlist — robustly.

    This is the single source of truth for watchlist changes (used by every
    watchlist button). It tolerates missing session keys, seed users not yet on
    disk, and never writes a partial/corrupt user record (it merges into the
    user's existing record rather than replacing it)."""
    if not ticker:
        return
    wl = list(st.session_state.get("watchlist", []) or [])
    if ticker in wl:
        wl = [x for x in wl if x != ticker]
    else:
        wl.append(ticker)
    st.session_state.watchlist = wl
    if not st.session_state.get("user"):
        return  # guest: keep it in session only
    email = st.session_state.user.get("email", "")
    if not email:
        return
    try:
        if "users_db" not in st.session_state or not isinstance(st.session_state.users_db, dict):
            st.session_state.users_db = _get_global_db()
        # Merge into the existing record so we never drop pw/name/role/etc.
        rec = dict(st.session_state.users_db.get(email, {}))
        rec["watchlist"] = wl
        st.session_state.users_db[email] = rec
        save_user_to_file(email, rec)
    except Exception:
        # Even if persistence fails, the in-session watchlist still updated.
        pass

# SERVER-SIDE SESSION STORE + pending upgrades moved to auth_store.py (imported above):
# _load_sessions/_save_sessions/_prune_sessions, new_session_token/lookup_session/
# destroy_session_token, and _pending_upgrades_path/remember_pending_upgrade/
# apply_pending_upgrade. They persist a random bearer token (server-side, with expiry,
# mirrored into localStorage) so a hard refresh / new tab re-establishes login.

# ─────────────────────────────────────────────────────────────
# RECOMMENDATION SNAPSHOT STORE  (performance + "$1000 since signal")
# ─────────────────────────────────────────────────────────────
# Self-contained, on-disk store of every recommendation the engine surfaces.
# The FIRST time a (category, ticker) signal appears we capture the entry
# price + timestamp and never overwrite it, so performance is always measured
# from the original signal. Each refresh we update the "live" fields (current
# price, max upside, max drawdown). This is what powers the card's
# "performance since signal" and "$1000 since signal" figures, and the
# historical-labeling pipeline in Phase 3.
# The recommendation snapshot store (load/save/key, prune, the process-wide
# _RECS_LOCK, and record_recommendation(s_bulk) / get_recommendation_snapshot) lives
# in recs_store.py — it routes through kvstore and shares msp_store's RECS_DB_PATH.
# app.py keeps the SAME names via this import, so every caller is unchanged.
from recs_store import (
    RECS_DB_PATH, RECS_RETENTION_DAYS, RECS_MAX_KEYS,
    _load_recs, _save_recs, _rec_key, _prune_recs, _RECS_LOCK,
    record_recommendation, record_recommendations_bulk, get_recommendation_snapshot,
)

# Recommendation performance & outcome evaluation (pure) lives in perf_eval.py:
# compute_performance, _humanize_age, the horizon-labeling framework (HORIZONS/
# SUCCESS_PCT/FAILURE_PCT/BENCH_MARGIN, _label_return, _label_vs_benchmark,
# _close_n_trading_days_after, evaluate_recommendation), and category_hit_rates.
# app.py keeps the SAME names via this import; evaluate_all_recommendations (the network
# driver) stays below.
from perf_eval import (
    compute_performance, _humanize_age,
    HORIZONS, SUCCESS_PCT, FAILURE_PCT, BENCH_MARGIN,
    _label_return, _label_vs_benchmark, _close_n_trading_days_after,
    evaluate_recommendation, category_hit_rates,
)

# market_status (US market-hours state + countdown) and _fmt_countdown are pure and
# live in market_utils.py. app.py keeps the names via this import; render_market_timer
# (the UI banner, below) and the warm worker's market-aware sleep both use market_status.
from market_utils import market_status, _fmt_countdown, fmt_mktcap, fmt_money, fmt_pct

def _html_iframe(html: str, height: int):
    """Render an HTML string in an ISOLATED, fixed-height iframe via the non-deprecated
    st.iframe (data: URL), replacing st.components.v1.html (deprecated, removed after
    2026-06-01). Used for self-contained visual blocks whose CSS sets html/body or uses
    generic class names — page-CSS isolation that inline st.html can't give. For blocks
    that only run a script side-effect, use st.html(unsafe_allow_javascript=True) instead.

    The leading <meta charset> + data-URL charset are REQUIRED: a data: URL carries no
    charset (the old srcdoc inherited the page's UTF-8), so without them multibyte glyphs
    (arrows, x-mark, middot) mojibake."""
    import base64
    doc = '<meta charset="utf-8">' + html
    b64 = base64.b64encode(doc.encode("utf-8")).decode("ascii")
    st.iframe(f"data:text/html;charset=utf-8;base64,{b64}", height=height)

def render_market_timer():
    """Centered market-status banner rendered as PURE HTML via st.markdown — NOT
    components.html. The old iframe version triggered a Streamlit rerun when it
    mounted, which left a dimmed stale copy of the page visible during the
    dashboard's data-load window (the 'everything renders twice' bug). Pure
    markdown has no iframe, so no rerun, no duplication. The countdown is
    accurate at render time and updates on the next page interaction/refresh —
    an acceptable trade for a rock-solid, flicker-free layout."""
    ms = market_status()
    state = ms["state"]; secs = ms["seconds"]; verb = ms["target"]; label = ms["label"]
    dot = {"open": GREEN, "pre": GOLD, "after": "#6b7fa0", "closed": "#6b7fa0"}.get(state, "#6b7fa0")
    glow = "0 0 8px rgba(34,197,94,0.6)" if state == "open" else "none"
    verb_txt = "Closes in" if verb == "closes" else "Opens in"
    cd = _fmt_countdown(int(secs))
    # Honest price-freshness note so a delayed/close price never looks silently wrong.
    delay_note = "Prices ~15-min delayed" if state == "open" else "Prices as of last close"
    st.markdown(f"""
    <div style="display:flex;align-items:center;justify-content:center;gap:14px;flex-wrap:wrap;
        font-family:'Inter',-apple-system,sans-serif;padding:11px 18px;margin:0 auto 18px;
        background:linear-gradient(135deg,#0d1525,#0a0f1a);border:1px solid rgba(255,255,255,0.08);
        border-radius:12px;max-width:560px;">
      <span style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;color:#e2e8f0;">
        <span style="width:9px;height:9px;border-radius:50%;background:{dot};box-shadow:{glow};display:inline-block;"></span>
        {label}
      </span>
      <span style="width:1px;height:16px;background:rgba(255,255,255,0.1);"></span>
      <span style="font-size:13px;color:#6b7fa0;">{verb_txt}
        <span style="font-family:'JetBrains Mono',monospace;font-weight:800;color:#a5b4fc;margin-left:4px;">{cd}</span>
      </span>
      <span style="width:1px;height:16px;background:rgba(255,255,255,0.1);"></span>
      <span style="font-size:11px;color:#4a5e7a;">{delay_note}</span>
    </div>
    """, unsafe_allow_html=True)



# HISTORICAL RECOMMENDATION LABELING FRAMEWORK moved to perf_eval.py (imported above):
# HORIZONS/SUCCESS_PCT/FAILURE_PCT/BENCH_MARGIN, _label_return, _label_vs_benchmark,
# _close_n_trading_days_after, evaluate_recommendation, category_hit_rates. Given a
# snapshot (frozen entry + trigger) and daily OHLCV it labels each horizon
# success/failure/neutral/pending, adds a vs-SPY label, and max upside/drawdown.
# evaluate_all_recommendations (below) is the network driver that feeds it OHLCV.

def evaluate_all_recommendations(horizons=HORIZONS, fetch_prices=True):
    """Evaluate every stored recommendation snapshot. When fetch_prices is True
    we pull each ticker's daily OHLCV (cached) and SPY as benchmark to compute
    horizon outcomes; otherwise we label only from snapshot extremes."""
    recs = _load_recs()
    bench_df = yf_ohlcv("SPY", 60) if fetch_prices else None
    evals = []
    seen_ohlcv = {}
    for key, snap in recs.items():
        ohlcv = None
        if fetch_prices:
            tk = snap.get("ticker", "")
            if tk not in seen_ohlcv:
                seen_ohlcv[tk] = yf_ohlcv(tk, 60)
            ohlcv = seen_ohlcv[tk]
        evals.append(evaluate_recommendation(snap, ohlcv, bench_df, horizons))
    return evals

# ─────────────────────────────────────────────────────────────
# MACHINE LEARNING  (Phase 4 · brief §K, §L)
# ─────────────────────────────────────────────────────────────
# Predicts the probability that a freshly-surfaced recommendation will be a
# "success" (>= +SUCCESS_PCT) at a chosen horizon, using ONLY signal-time
# features (never post-signal prices — that would be target leakage). Training
# uses a TIME-AWARE split (older signals train, newer signals test) so we never
# leak future information into the past, which is the cardinal sin of financial
# ML. Falls back gracefully (heuristic probability from the score) when sklearn
# is unavailable or there isn't enough labeled history to train on.
#
# HONEST CAVEAT: real predictive accuracy only emerges once the app has
# accumulated a meaningful number of RESOLVED outcomes from live market data.
# Until then the model is structurally correct but data-starved; the app shows
# how much training data exists and never fabricates accuracy.
try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                                  f1_score, roc_auc_score, confusion_matrix)
    HAS_SKLEARN = True
except Exception:
    HAS_SKLEARN = False

ML_MODEL_VERSION = "msp-gbc-v1"
ML_MIN_TRAIN = 40          # need at least this many resolved labels to train
ML_FEATURE_NAMES = ["score", "f_momentum", "f_trend", "f_macd", "f_volume",
                    "f_sentiment", "f_squeeze", "bull_pct", "short_float_pct",
                    "is_hot", "pe", "log_mktcap"]

def _ml_features_from_snapshot(snap):
    """Build the feature vector for a recommendation FROM SIGNAL-TIME DATA ONLY.

    We re-derive the signal-time technicals from the ticker's OHLCV up to (and
    including) the trigger date — never after it — plus fundamentals and the
    stored score. This guarantees no leakage from post-signal price action.
    Returns a list aligned with ML_FEATURE_NAMES, or None if data is missing.
    """
    try:
        tk = snap.get("ticker", "")
        trig = snap.get("triggered_at", 0)
        df = yf_ohlcv(tk, 60)
        if df is None or len(df) < 10:
            return None
        # Slice to bars on/before the trigger date — signal-time view only.
        import datetime as _dt
        cut = _dt.datetime.fromtimestamp(float(trig))
        mask = []
        for d in df["datetime"]:
            dd = d.to_pydatetime() if hasattr(d, "to_pydatetime") else d
            if getattr(dd, "tzinfo", None) is not None:
                dd = dd.replace(tzinfo=None)
            mask.append(dd <= cut)
        df_past = df[pd.Series(mask, index=df.index)]
        if len(df_past) < 10:
            df_past = df.iloc[:max(10, len(df)//2)]  # fallback: first half
        info = yf_fund(tk) or {}
        sent = st_sent(tk) or {}
        sc, bd, op, risk, conf = compute_scores(df_past, info, sent)
        mktcap = info.get("mktcap", 0) or 0
        return [
            float(snap.get("score_at_trigger") or sc or 0),
            float(bd.get("Momentum", 0)), float(bd.get("Trend", 0)),
            float(bd.get("MACD", 0)), float(bd.get("Volume", 0)),
            float(bd.get("Sentiment", 0)), float(bd.get("Squeeze", 0)),
            float(sent.get("bull", 50)),
            float((info.get("sf", 0) or 0) * 100),
            1.0 if snap.get("ticker") in (st_hot() or []) else 0.0,
            float(info.get("pe") or 0),
            float(math.log10(mktcap + 1)) if mktcap > 0 else 0.0,
        ]
    except Exception:
        return None

def build_ml_dataset(horizon=5):
    """Assemble (X, y, timestamps) from RESOLVED recommendations at `horizon`.
    y = 1 if the horizon outcome label is 'success', else 0. Pending/unlabeled
    rows are excluded. Returns (X, y, ts) as parallel lists."""
    recs = _load_recs()
    X, y, ts = [], [], []
    for key, snap in recs.items():
        ev = evaluate_recommendation(snap, yf_ohlcv(snap.get("ticker",""), 60),
                                     yf_ohlcv("SPY", 60), HORIZONS)
        hz = ev["horizons"].get(horizon, {})
        label = hz.get("label", "pending")
        if label == "pending":
            continue
        feats = _ml_features_from_snapshot(snap)
        if feats is None:
            continue
        X.append(feats)
        y.append(1 if label == "success" else 0)
        ts.append(snap.get("triggered_at", 0))
    return X, y, ts

def train_and_evaluate_model(horizon=5, test_frac=0.25):
    """Train the success classifier with a TIME-AWARE split and report metrics.

    Returns a dict with status, training size, the fitted model (in-memory), and
    classification metrics (accuracy/precision/recall/F1/ROC-AUC + confusion
    matrix) computed on the held-out NEWER portion. Never shuffles — the split
    is chronological so we evaluate on the future, not a random subset.
    """
    if not HAS_SKLEARN:
        return {"status": "no_sklearn",
                "message": "scikit-learn not installed; using heuristic fallback."}
    X, y, ts = build_ml_dataset(horizon)
    n = len(y)
    if n < ML_MIN_TRAIN:
        return {"status": "insufficient_data", "n": n, "needed": ML_MIN_TRAIN,
                "message": f"Only {n} resolved labels; need >= {ML_MIN_TRAIN} to train. "
                           "Accuracy will be meaningful once live history accumulates."}
    if len(set(y)) < 2:
        return {"status": "single_class", "n": n,
                "message": "All resolved outcomes are the same class so far; "
                           "cannot train a discriminative model yet."}
    # Chronological split — train on oldest, test on newest (no leakage).
    order = sorted(range(n), key=lambda i: ts[i])
    Xs = [X[i] for i in order]; ys = [y[i] for i in order]
    split = int(n * (1 - test_frac))
    Xtr, Xte = Xs[:split], Xs[split:]
    ytr, yte = ys[:split], ys[split:]
    if not Xte or len(set(ytr)) < 2:
        return {"status": "insufficient_split", "n": n,
                "message": "Not enough varied history in the time-split to evaluate."}
    model = GradientBoostingClassifier(n_estimators=120, max_depth=3,
                                       learning_rate=0.05, random_state=42)
    model.fit(Xtr, ytr)
    proba = model.predict_proba(Xte)[:, 1]
    pred = (proba >= 0.5).astype(int)
    metrics = {
        "accuracy": round(float(accuracy_score(yte, pred)), 3),
        "precision": round(float(precision_score(yte, pred, zero_division=0)), 3),
        "recall": round(float(recall_score(yte, pred, zero_division=0)), 3),
        "f1": round(float(f1_score(yte, pred, zero_division=0)), 3),
    }
    try:
        metrics["roc_auc"] = round(float(roc_auc_score(yte, proba)), 3) if len(set(yte)) > 1 else None
    except Exception:
        metrics["roc_auc"] = None
    cm = confusion_matrix(yte, pred, labels=[0, 1]).tolist()
    # Feature importances for explainability
    imp = sorted(zip(ML_FEATURE_NAMES, model.feature_importances_),
                 key=lambda x: x[1], reverse=True)
    return {"status": "ok", "n": n, "n_train": len(Xtr), "n_test": len(Xte),
            "horizon": horizon, "metrics": metrics, "confusion_matrix": cm,
            "feature_importance": [(f, round(float(v), 3)) for f, v in imp],
            "model": model, "model_version": ML_MODEL_VERSION}

def predict_success_probability(snap, model=None, horizon=5):
    """Inference for a single recommendation. Uses the trained model if given;
    otherwise a transparent heuristic mapping the composite score to a
    probability (so the UI always has something honest to show). Returns a dict
    with probability, a confidence band, and the basis used."""
    feats = _ml_features_from_snapshot(snap)
    if model is not None and feats is not None and HAS_SKLEARN:
        try:
            p = float(model.predict_proba([feats])[0][1])
            conf = "High" if abs(p - 0.5) > 0.30 else "Medium" if abs(p - 0.5) > 0.15 else "Low"
            return {"probability": round(p, 3), "confidence": conf,
                    "basis": "model", "model_version": ML_MODEL_VERSION}
        except Exception:
            pass
    # Heuristic fallback: monotonic in score, clearly labeled as such.
    score = float(snap.get("score_at_trigger") or 0)
    p = max(0.05, min(0.95, score / 100.0))
    return {"probability": round(p, 3),
            "confidence": "Heuristic",
            "basis": "heuristic_score",
            "note": "Score-based estimate; predictive model trains once enough "
                    "resolved outcomes accumulate."}

# _get_global_db / _save_global_db / _load_seed_accounts moved to auth_store.py
# (imported above): the shared users DB = seed accounts (owner/admin from st.secrets;
# demo/premium only when SEED_DEMO_ACCOUNTS=1) merged over the on-disk users (disk wins).

def get_td_key():
    try:
        k=st.secrets.get("TWELVE_DATA_API_KEY","")
        if k: return k
    except: pass
    if is_admin(): return st.session_state.get("_admin_td_key","")
    return ""

def get_fmp_key():
    """Financial Modeling Prep API key (optional). When present, Discover can
    perform TRUE market-wide screening (25,000+ US stocks) instead of filtering
    the curated fallback watchlist. Read from Streamlit secrets, with an
    admin-entered session fallback. No key → app still works on the curated
    universe."""
    try:
        k = st.secrets.get("FMP_API_KEY", "")
        if k: return k
    except Exception:
        pass
    return st.session_state.get("_admin_fmp_key", "")

def fmp_enabled():
    return bool(get_fmp_key())

def get_polygon_key():
    """Polygon.io (Massive) Stocks API key. When present, the scored universe is
    built from Polygon's whole-market bulk endpoints (grouped-daily history +
    snapshot quotes) instead of the curated ~85-name yfinance/FMP set — expanding
    coverage to thousands of liquid tickers with near-live prices. Read from
    Streamlit secrets first, then env, then an admin-entered session fallback.
    No key → the app falls back to the legacy universe and still works."""
    try:
        k = st.secrets.get("POLYGON_API_KEY", "")
        if k:
            return k
    except Exception:
        pass
    k = _os.environ.get("POLYGON_API_KEY", "") or ""
    if k:
        return k
    return st.session_state.get("_admin_polygon_key", "")

def polygon_enabled():
    return HAS_POLYGON and bool(get_polygon_key())

# ─────────────────────────────────────────────────────────────
# STRIPE PAYMENT PROCESSING
# ─────────────────────────────────────────────────────────────
def _stripe_key():
    try: return st.secrets.get("STRIPE_SECRET_KEY","")
    except: return ""

def stripe_configured():
    return bool(_stripe_key())

def _get_app_url():
    try: return st.secrets.get("APP_URL","https://stockwins.streamlit.app")
    except: return "https://stockwins.streamlit.app"

def create_checkout_session(plan, user_email):
    """Create Stripe Checkout Session. Returns (url, error)."""
    key = _stripe_key()
    if not key:
        return None, "STRIPE_SECRET_KEY not found in Secrets. Add it and reboot the app."

    # Validate library
    try:
        import stripe as _s
    except ImportError:
        return None, "stripe library not installed. Ensure requirements.txt contains 'stripe' and redeploy."

    # Validate key format
    if not (key.startswith("sk_test_") or key.startswith("sk_live_")):
        return None, (f"STRIPE_SECRET_KEY must start with sk_test_ or sk_live_. "
                      f"Got: {key[:12]}... — Make sure it's the Secret Key, not the Publishable Key (pk_test_)")

    # Get and validate price ID
    price_key = "STRIPE_PRICE_MONTHLY" if plan == "premium" else "STRIPE_PRICE_ANNUAL"
    try: price_id = st.secrets.get(price_key, "")
    except: price_id = ""
    if not price_id:
        return None, f"{price_key} not found in Secrets. Add price_xxx ID from Stripe Products page."
    if not price_id.startswith("price_"):
        return None, f"{price_key} must start with 'price_'. Got: {price_id[:30]} — use the Price ID, not a Payment Link URL."

    try:
        _s.api_key = key
        app_url = _get_app_url()
        # Try new-style API first (stripe 5+), fall back to old-style
        try:
            create_fn = _s.checkout.sessions.create
        except AttributeError:
            create_fn = _s.checkout.Session.create
        sess = create_fn(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=user_email,
            client_reference_id=user_email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{app_url}/?payment=success&sid={{CHECKOUT_SESSION_ID}}&plan={plan}",
            cancel_url=f"{app_url}/?payment=cancelled",
            metadata={"user_email": user_email, "plan": plan},
            subscription_data={"metadata": {"user_email": user_email, "plan": plan}},
            allow_promotion_codes=True,
        )
        return sess.url, None
    except Exception as e:
        # Get the most useful error info regardless of stripe version
        err_type = type(e).__name__
        err_msg  = repr(e)
        # Try to extract user-facing message from Stripe errors
        user_msg = getattr(e, "user_message", None) or getattr(e, "message", None) or str(e)
        if not user_msg:
            user_msg = err_msg
        return None, f"{err_type}: {user_msg}"

def _stripe_pub_key():
    try: return st.secrets.get("STRIPE_PUBLISHABLE_KEY","") or st.secrets.get("STRIPE_PUBLIC_KEY","")
    except: return ""

def create_embedded_subscription(plan, user_email):
    """Create an INCOMPLETE subscription + PaymentIntent so we can collect the card with
    an IN-PAGE Stripe Elements form (no redirect to a hosted Stripe page). Returns
    (pub_key, client_secret, error)."""
    key = _stripe_key()
    if not key:
        return None, None, "STRIPE_SECRET_KEY not set in Secrets."
    pub = _stripe_pub_key()
    if not pub or not pub.startswith(("pk_test_", "pk_live_")) or "REPLACE" in pub:
        # Empty, malformed, or still the placeholder → let _do_checkout fall back to
        # the hosted-redirect path rather than render a broken in-page card form.
        return None, None, "STRIPE_PUBLISHABLE_KEY not set/invalid (required for the in-page form)."
    try:
        import stripe as _s
    except ImportError:
        return None, None, "stripe library not installed."
    if not (key.startswith("sk_test_") or key.startswith("sk_live_")):
        return None, None, "STRIPE_SECRET_KEY must start with sk_test_ or sk_live_."
    price_key = "STRIPE_PRICE_MONTHLY" if plan == "premium" else "STRIPE_PRICE_ANNUAL"
    try: price_id = st.secrets.get(price_key, "")
    except: price_id = ""
    if not price_id.startswith("price_"):
        return None, None, f"{price_key} missing/invalid — add the Price ID (price_…) from Stripe."
    try:
        _s.api_key = key
        custs = _s.Customer.list(email=user_email, limit=1)
        cust = (custs.data[0] if getattr(custs, "data", None)
                else _s.Customer.create(email=user_email, metadata={"plan": plan}))
        sub = _s.Subscription.create(
            customer=cust.id,
            items=[{"price": price_id}],
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            expand=["latest_invoice.payment_intent"],
            metadata={"user_email": user_email, "plan": plan},
        )
        pi = getattr(getattr(sub, "latest_invoice", None), "payment_intent", None)
        secret = getattr(pi, "client_secret", None) if pi else None
        if not secret:
            return None, None, "Stripe returned no client_secret for the payment."
        return pub, secret, None
    except Exception as e:
        user_msg = getattr(e, "user_message", None) or getattr(e, "message", None) or str(e)
        return None, None, f"{type(e).__name__}: {user_msg}"

def verify_checkout_session(session_id):
    """Verify completed Stripe Checkout. Returns (plan, email) or (None, error)."""
    key = _stripe_key()
    if not key: return None,"Stripe not configured"
    try:
        import stripe as _s
        _s.api_key = key
        sess = _s.checkout.sessions.retrieve(session_id)
        if sess.payment_status == "paid":
            plan  = sess.metadata.get("plan","premium")
            email = sess.customer_email or sess.client_reference_id or ""
            return plan, email
        return None, f"Payment status: {sess.payment_status}"
    except Exception as e:
        return None, f"Stripe error: {e}"

def create_portal_session(user_email):
    """Create Stripe Customer Portal session. Returns (url, error)."""
    key = _stripe_key()
    if not key: return None,"Stripe not configured"
    try:
        import stripe as _s
        _s.api_key = key
        customers = _s.Customer.list(email=user_email, limit=1)
        if not customers.data:
            return None,"No billing account found for this email. Contact support."
        # Carry the session token in the return URL — without it, coming back from the
        # Stripe portal dropped ?sid= and logged the user out.
        _tok = ""
        try:
            _tok = st.session_state.get("_sid", "") or ""
        except Exception:
            _tok = ""
        _ret = _get_app_url().rstrip("/") + "/?page=settings" + (f"&sid={_tok}" if _tok else "")
        portal = _s.billing_portal.sessions.create(
            customer=customers.data[0].id,
            return_url=_ret,
        )
        return portal.url, None
    except Exception as e:
        return None, f"Stripe error: {e}"

def handle_payment_return():
    """Check URL params for Stripe redirect or push subscription. Returns True if handled."""
    try: params = st.query_params.to_dict()
    except: return False

    # ── Logout via URL ──
    if params.get("page") == "__logout__":
        st.query_params.clear()
        logout()
        return True

    # ── Legacy topbar_nav support (backward compat) ──
    if params.get("topbar_nav"):
        target = params.get("topbar_nav","").strip()
        st.query_params.clear()
        if target == "__logout__":
            logout()
            return True
        valid_pages = {"landing","features","login","signup","verify_email","forgot_pw","pricing",
                       "contact","dashboard","discover","watchlist","screener","bi_dashboard",
                       "stock_detail","settings","admin","signal_track","performance","signals"}
        if target in valid_pages:
            nav(target)
            return True
        return True

    # ── Native Web Push subscription registration (VAPID flow, ?push_sub=<b64 json>) ──
    if params.get("push_sub") and is_authed():
        sub = _webpush.decode_subscription(params.get("push_sub", "")) if _webpush else None
        if sub:
            try:
                e = st.session_state.user["email"]
                subs = st.session_state.users_db.get(e, {}).get("push_subscriptions", [])
                if not any((s or {}).get("endpoint") == sub.get("endpoint") for s in subs):
                    subs.append(sub)
                    subs = subs[-5:]   # keep last 5 devices
                st.session_state.users_db[e]["push_subscriptions"] = subs
                st.session_state.users_db[e]["push_subscribed"] = True
                _save_global_db(st.session_state.users_db)
                save_user_to_file(e, st.session_state.users_db[e])
                st.session_state["_push_registered"] = True
            except Exception:
                pass
        st.query_params.clear()

    # ── Push subscription registration (from OneSignal JS callback) ──
    if params.get("push_sub_id") and is_authed():
        sub_id = params.get("push_sub_id","").strip()
        if sub_id:
            try:
                e = st.session_state.user["email"]
                existing = st.session_state.users_db.get(e,{}).get("push_subscription_ids",[])
                if sub_id not in existing:
                    existing.append(sub_id)
                    # Keep last 5 devices
                    existing = existing[-5:]
                st.session_state.users_db[e]["push_subscription_ids"] = existing
                st.session_state.users_db[e]["push_subscribed"] = True
                _save_global_db(st.session_state.users_db)
                save_user_to_file(e, st.session_state.users_db[e])
                st.session_state["_push_registered"] = True
            except Exception:
                pass
        st.query_params.clear()
        return True

    if params.get("payment") == "success":
        sid = params.get("sid","")
        st.query_params.clear()
        if not sid:
            st.session_state["_pay_error"] = "Missing checkout session — could not confirm payment."
            return True
        v_plan, v_info = verify_checkout_session(sid)
        if not v_plan:
            st.session_state["_pay_error"] = v_info
            return True
        # v_info = the VERIFIED buyer email from Stripe. Grant premium to THAT account —
        # never to whoever merely happens to be logged in (that let an attacker replay a
        # paid ?sid to upgrade themselves). If the buyer isn't logged in / has no account
        # yet, stash a pending upgrade so they get it on next login/signup.
        buyer = (v_info or "").strip()
        new_plan = "Monthly" if v_plan == "premium" else "Annual"
        if not buyer:
            st.session_state["_pay_error"] = "Could not verify the purchaser. Please contact support."
            return True
        db = _get_global_db()
        if buyer in db:
            db[buyer]["role"] = "premium"; db[buyer]["plan"] = new_plan
            _save_global_db(db); save_user_to_file(buyer, db[buyer])
            st.session_state.users_db = db
            # If the buyer IS the logged-in user, reflect it now + ROTATE the session token
            # (privilege change → new token, mitigates session fixation).
            if is_authed() and st.session_state.user["email"].strip().lower() == buyer.lower():
                st.session_state.role = "premium"
                _old = st.session_state.get("_sid")
                if _old: destroy_session_token(_old)
                st.session_state["_sid"] = new_session_token(buyer, "premium")
        else:
            remember_pending_upgrade(buyer, new_plan)
        st.session_state["_pay_success"] = v_plan
        track_once("_pay_tracked", "payment_success", {"plan": v_plan, "email": buyer})
        return True

    elif params.get("payment") == "cancelled":
        st.query_params.clear()
        st.session_state["_pay_cancelled"] = True
        return True

    elif params.get("checkout"):
        plan = params.get("checkout","")
        st.query_params.clear()
        if plan in ("premium","annual"):
            if is_authed():
                url, err = create_checkout_session(plan, st.session_state.user["email"])
                if url: st.session_state["_redirect_url"] = url
                else:   st.session_state["_pay_error"] = err
            else:
                st.session_state["_pending_checkout"] = plan
                nav("signup")
        return True

    return False

# ─────────────────────────────────────────────────────────────
# DESIGN SYSTEM
# ─────────────────────────────────────────────────────────────
# Brand palette lives in theme.py (single source of truth); imported back so all of
# app.py's UI references (GREEN/RED/GOLD/...) are unchanged.
from theme import GOLD, GOLD2, BLUE, GREEN, RED, BG, CARD, BORDER

# ─────────────────────────────────────────────────────────────
# CUSTOM ICON SYSTEM  (bespoke line-glyphs per category — no stock emoji)
# ─────────────────────────────────────────────────────────────
# Each category/theme has a hand-built inline SVG that VISUALLY encodes its meaning
# (a rising line for Momentum, a magnet for Smart-Money, clustered dots for Insider,
# a V for Reversal, etc.). They're a single cohesive set: 24×24 viewBox, rounded
# line-style, drawn in currentColor so callers tint them via CSS. The COMPOSITE_CATS
# keys still carry an emoji prefix (used as IDs across scoring/alerts), so we strip
# that for display via _clean_name and render OUR icon instead.
def _svg(paths, size=18, sw=2.15):
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" '
            f'stroke-linejoin="round" class="msp-ic">{paths}</svg>')

_ICON_PATHS = {
    # ── Composite categories ──
    "🌊 Momentum Leaders":      '<polyline points="3 17 9 11 13 14 21 6"/><polyline points="15 6 21 6 21 12"/>',
    "⚡ Momentum Surge":         '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>',
    "🏆 Relative Strength":      '<line x1="5" y1="20" x2="5" y2="14"/><line x1="12" y1="20" x2="12" y2="9"/><line x1="19" y1="20" x2="19" y2="4"/>',
    "🏅 Quality Momentum":       '<path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6z"/><polyline points="9 12 11 14 15 9"/>',
    "🎯 Pullback Buy":           '<polyline points="3 16 9 10 13 13 20 6"/><circle cx="13" cy="13" r="1.5"/>',
    "🚀 Breakout Watch":         '<line x1="4" y1="13" x2="20" y2="13" stroke-dasharray="2 3"/><polyline points="12 20 12 4"/><polyline points="7 9 12 4 17 9"/>',
    "🌪️ Volatility Squeeze":     '<path d="M4 4 11 11 4 18"/><path d="M20 4 13 11 20 18"/>',
    "💥 Volatility Expansion":   '<path d="M12 2v6M12 16v6M2 12h6M16 12h6M5 5l3.5 3.5M15.5 15.5 19 19M19 5l-3.5 3.5M8.5 15.5 5 19"/>',
    "🍃 VCP Volume Dry-Up":      '<line x1="4" y1="20" x2="4" y2="9"/><line x1="8" y1="20" x2="8" y2="12"/><line x1="12" y1="20" x2="12" y2="15"/><line x1="16" y1="20" x2="16" y2="17"/><polyline points="18 10 21 6"/>',
    "📉→📈 Oversold Reversal":   '<polyline points="3 5 11 17 21 7"/><polyline points="17 7 21 7 21 11"/>',
    "🪂 Fallen Angels":          '<path d="M4 5C8 18 14 19 21 16"/><polyline points="21 16 21 11"/>',
    "🩸 Capitulation Bottom":    '<polyline points="3 4 9 20 13 12 21 16"/>',
    "🔥 Short Squeeze":          '<path d="M6 20h12M7 16h10M8 12h8"/><polyline points="9 8 12 3 15 8"/>',
    "⚡🧲 Smart-Money Squeeze":   '<path d="M7 3v8a5 5 0 0 0 10 0V3"/><line x1="5.5" y1="3" x2="9" y2="3"/><line x1="15" y1="3" x2="18.5" y2="3"/>',
    "🏛️ Insider Cluster":        '<circle cx="7" cy="10" r="2"/><circle cx="17" cy="10" r="2"/><circle cx="12" cy="16" r="2"/><path d="M9 10h6M8.6 11.6 11 14.6M15.4 11.6 13 14.6"/>',
    "🎪 Catalyst / Gap":         '<polyline points="3 16 10 16"/><polyline points="14 7 21 7"/><line x1="10" y1="16" x2="14" y2="7" stroke-dasharray="2 2"/>',
    "🦈 Quiet Accumulation":     '<path d="M4 20h4v-3h4v-4h4v-5h4"/>',
    "🎭 Social Catalyst":        '<path d="M4 5h16v10H10l-4 4z"/><circle cx="9" cy="10" r=".7"/><circle cx="12" cy="10" r=".7"/><circle cx="15" cy="10" r=".7"/>',
    "💎 Value Momentum":         '<path d="M6 4h12l3 5-9 11-9-11z"/><path d="M3 9h18"/>',
    "💡 Hidden Movers":          '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/>',
    # ── Themes ──
    "🔥 Momentum & Trend":       '<polyline points="3 17 9 11 13 14 21 6"/><polyline points="15 6 21 6 21 12"/>',
    "📈 Breakouts & Volatility": '<line x1="4" y1="13" x2="20" y2="13" stroke-dasharray="2 3"/><polyline points="12 20 12 4"/><polyline points="7 9 12 4 17 9"/>',
    "📉 Reversals & Bottoms":    '<polyline points="3 5 11 17 21 7"/><polyline points="17 7 21 7 21 11"/>',
    "⚡ Squeeze & Short Interest":'<path d="M4 4 11 11 4 18"/><path d="M20 4 13 11 20 18"/>',
    "🏛️ Smart Money & Catalysts":'<circle cx="7" cy="10" r="2"/><circle cx="17" cy="10" r="2"/><circle cx="12" cy="16" r="2"/><path d="M9 10h6M8.6 11.6 11 14.6M15.4 11.6 13 14.6"/>',
    "🎭 Social, Value & Hidden": '<path d="M4 5h16v10H10l-4 4z"/><circle cx="9" cy="10" r=".7"/><circle cx="12" cy="10" r=".7"/><circle cx="15" cy="10" r=".7"/>',
    "🐻 Bearish & Short":        '<polyline points="3 6 9 12 13 9 21 18"/><polyline points="15 18 21 18 21 12"/>',
    # ── Standard (free) screens ──
    "🔥 Trending Now":  '<path d="M12 3c1 4-2 5-2 8a2 2 0 0 0 4 0c0-1 0-2-1-3 2 1 4 3 4 6a5 5 0 0 1-10 0c0-4 4-6 5-11z"/>',
    "📡 Social Buzz":   '<path d="M5 12a7 7 0 0 1 7-7M5 12a7 7 0 0 0 7 7M8 12a4 4 0 0 1 4-4M8 12a4 4 0 0 0 4 4"/><circle cx="12" cy="12" r="1.4"/>',
    "💻 Tech":          '<rect x="3" y="5" width="18" height="12" rx="1"/><line x1="8" y1="21" x2="16" y2="21"/>',
    "🤖 AI":            '<rect x="5" y="8" width="14" height="11" rx="2"/><line x1="12" y1="4" x2="12" y2="8"/><circle cx="12" cy="3.5" r="1"/><circle cx="9.5" cy="13" r="1.1"/><circle cx="14.5" cy="13" r="1.1"/>',
    "⚡ EV":            '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>',
    "🧬 Biotech":       '<path d="M7 3c0 6 10 6 10 12M7 21c0-6 10-6 10-12M8 6.5h8M8 17.5h8M9.5 10h5M9.5 14h5"/>',
    "📊 S&P 500":       '<line x1="5" y1="20" x2="5" y2="12"/><line x1="10" y1="20" x2="10" y2="6"/><line x1="15" y1="20" x2="15" y2="9"/><line x1="20" y1="20" x2="20" y2="4"/>',
    "💹 NASDAQ":        '<polyline points="3 16 8 10 12 13 17 5"/><polyline points="13 5 17 5 17 9"/>',
    "🔬 Small Cap":     '<circle cx="10" cy="10" r="6"/><line x1="14.5" y1="14.5" x2="21" y2="21"/>',
    # ── Bearish / short setups ──
    "📉 Breakdown":         '<polyline points="3 6 9 12 13 9 21 18"/><polyline points="15 18 21 18 21 12"/>',
    "🐻 Distribution":      '<line x1="5" y1="20" x2="5" y2="8"/><line x1="10" y1="20" x2="10" y2="11"/><line x1="15" y1="20" x2="15" y2="14"/><line x1="20" y1="20" x2="20" y2="16"/><polyline points="3 5 21 15"/>',
    "🔻 Overbought Fade":   '<polyline points="3 7 9 5 13 9 21 17"/><polyline points="15 17 21 17 21 11"/><line x1="3" y1="4" x2="21" y2="4" stroke-dasharray="2 3"/>',
    # ── Event-driven alert types (Signals feed) ──
    "🏛️ Insider Buy":   '<circle cx="7" cy="10" r="2"/><circle cx="17" cy="10" r="2"/><circle cx="12" cy="16" r="2"/><path d="M9 10h6M8.6 11.6 11 14.6M15.4 11.6 13 14.6"/>',
    "📰 8-K Filing":    '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 15.5h6M9 8.5h2"/>',
    "📊 Short Interest":'<line x1="5" y1="20" x2="5" y2="13"/><line x1="10" y1="20" x2="10" y2="8"/><line x1="15" y1="20" x2="15" y2="11"/><line x1="20" y1="20" x2="20" y2="5"/><polyline points="16 5 20 5 20 9"/>',
}
_ICON_FALLBACK = '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.4"/>'

# Short, plain-English definitions surfaced next to each category on Discover.
COMPOSITE_DEF = {
    "🌊 Momentum Leaders":      "Strong, steady uptrends holding above both moving averages",
    "⚡ Momentum Surge":         "Momentum accelerating fast with a fresh MACD upturn",
    "🏆 Relative Strength":      "Top-decile performers versus the entire market",
    "🏅 Quality Momentum":       "Profitable, sanely-valued names in a confirmed strong trend",
    "🎯 Pullback Buy":           "Healthy uptrends dipping back to support — buy-the-dip zones",
    "🚀 Breakout Watch":         "Pressing fresh 60-day highs on heavy volume",
    "🌪️ Volatility Squeeze":     "Coiled, compressed ranges primed to expand",
    "💥 Volatility Expansion":   "Big moves on surging volume — ranges igniting now",
    "🍃 VCP Volume Dry-Up":      "Coiling near highs as volume quietly dries up",
    "📉→📈 Oversold Reversal":   "Oversold names with momentum turning back up",
    "🪂 Fallen Angels":          "Beaten-down names basing as volume returns",
    "🩸 Capitulation Bottom":    "Deep washouts flushing out the last sellers",
    "🔥 Short Squeeze":          "High days-to-cover with real upward pressure",
    "⚡🧲 Smart-Money Squeeze":   "Heavily shorted while big money quietly accumulates",
    "🏛️ Insider Cluster":        "Multiple insiders buying on the open market",
    "🎪 Catalyst / Gap":         "Gapping on heavy volume — a news or event in play",
    "🦈 Quiet Accumulation":     "Stealthy, steady accumulation on a calm climb",
    "🎭 Social Catalyst":        "Social chatter spiking alongside a volume surge",
    "💎 Value Momentum":         "Cheap valuations meeting a rising trend",
    "💡 Hidden Movers":          "Strong setups the crowd hasn't noticed yet",
    "📉 Breakdown":              "Breaking to new lows on heavy volume — a short setup",
    "🐻 Distribution":           "Money quietly flowing out on rising volume — a short setup",
    "🔻 Overbought Fade":        "Stretched and rolling over near a high — short-the-rip",
}
CATEGORY_DEF = {
    "🔥 Trending Now":  "The most talked-about tickers right now",
    "📡 Social Buzz":   "Reddit & StockTwits community favorites",
    "💻 Tech":          "Large-cap technology leaders",
    "🤖 AI":            "Artificial-intelligence & compute names",
    "⚡ EV":            "Electric-vehicle & charging stocks",
    "🧬 Biotech":       "Biotech & pharma innovators",
    "📊 S&P 500":       "Mega-cap S&P 500 components",
    "💹 NASDAQ":        "NASDAQ-100 heavyweights",
    "🔬 Small Cap":     "Higher-beta small-cap movers",
}

def _clean_name(name):
    """Strip the leading emoji/symbol prefix from a category key for display."""
    s = str(name)
    i = 0
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in "(["):
            break
        i += 1
    return s[i:].strip() or s

def cat_icon(name, size=18):
    """Inline custom SVG glyph for a category/theme (tints to currentColor)."""
    return _svg(_ICON_PATHS.get(name, _ICON_FALLBACK), size)

def cat_def(name):
    """Short plain-English definition for a category (composite or standard)."""
    return COMPOSITE_DEF.get(name) or CATEGORY_DEF.get(name) or ""

def cat_chip(name, size=18, cls=""):
    """Inline 'custom-icon + clean name' unit for headers/labels/buttons."""
    return (f'<span class="msp-chip {cls}">{cat_icon(name, size)}'
            f'<span class="msp-chip-t">{_clean_name(name)}</span></span>')

def clickable_tile(inner_html, key, label="open"):
    """Render inner_html as a FULLY clickable tile (the whole box is the hit target,
    not a separate button). Call inside a st.columns() column. Returns True when clicked.

    Reliability: a real st.button follows the card; we find THAT button's element-
    container via an invisible MARKER sibling (`.ctile-ov`) using the
    `.element-container:has(<marker>)+.element-container` selector — the SAME pattern the
    topbar logo uses, which works across Streamlit versions. (The previous
    `[data-testid="stElementContainer"]:has(.stButton)` form silently failed in some
    browsers/versions, which is why tiles weren't clickable.) The button is stretched to
    cover the card and made invisible, so a click anywhere on the card fires it."""
    st.markdown(f'<div class="ctile">{inner_html}</div>', unsafe_allow_html=True)
    st.markdown('<div class="ctile-ov"></div>', unsafe_allow_html=True)
    return st.button(label, key=key)


def brand_logomark(size=26):
    """The Scanviction brandmark: a rounded indigo→violet badge with a rising
    'signal' line peaking at a gold spark. Bold + premium; pairs with the wordmark and
    works as an app icon / favicon / loading mark. Drawn inline so it tints with the
    brand palette and needs no asset hosting."""
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 32 32" fill="none" '
            f'class="msp-mark" style="vertical-align:-6px;flex-shrink:0;">'
            f'<defs><linearGradient id="mspmk" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">'
            f'<stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs>'
            f'<rect x="1.5" y="1.5" width="29" height="29" rx="8.5" fill="url(#mspmk)"/>'
            f'<path d="M7 21l5-6 3.5 2.6L22 9" stroke="#fff" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="23.3" cy="8.4" r="2.6" fill="#f59e0b"/></svg>')

# ─────────────────────────────────────────────────────────────
# CSS — Full Premium Design System
# ─────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

*,*::before,*::after{{box-sizing:border-box;}}
html,body,[data-testid="stAppViewContainer"]{{
    background:{BG} !important;color:#d1d9e6 !important;
    font-family:'Inter',-apple-system,sans-serif !important;
}}
[data-testid="stHeader"],#MainMenu,footer,[data-testid="stDecoration"]{{display:none !important;}}
/* ── Stale-frame hiding (fixes duplicate timer / Market Overview on load) ───
   During a rerun Streamlit keeps the PREVIOUS frame's elements in the DOM
   (marked data-stale) until the new frame paints. On the dashboard, the
   blocking market-data load created a window where the old frame AND the new
   frame were both visible — showing two "Market Closed" banners and two
   "MARKET OVERVIEW" headers. Hiding stale elements removes the old frame so
   only the current one shows.
   NOTE: this is safe now that the Discover auto-poll loop is gone. The earlier
   "falling/popping" was that poll re-running every 1.5s (each rerun hid+
   repainted everything). With no poll, stale-hide only acts during a single
   navigation/refresh — a clean one-time transition, not a repeating blank.
   The actual rule below was lost in a refactor, which is why a TALLER previous
   page (e.g. the Dashboard's Market Overview) bled through underneath a SHORTER
   one (e.g. the Discover warming state): its trailing elements stay in the DOM
   marked data-stale, and with nothing hiding them they remained visible. */
/* Stale-frame handling that does NOT flash the warming poll. A hard display:none
   blinked the preparing card on every 2.2s fragment rerun. Instead we DELAY the hide:
   an element only fades out after it has been stale for >0.3s. Fragment-rerun reuse
   resolves in milliseconds (never reaches the delay → no flash), while a real
   cross-page stale frame (the Market Overview bleed) persists past 0.3s → fades away. */
[data-stale="true"]{{opacity:0 !important;transition:opacity 0.08s linear 0.3s !important;}}
/* Hide Streamlit's built-in "running" status pill — the app shows its own loaders. */
[data-testid="stStatusWidget"]{{display:none !important;}}
/* ── Custom icon glyphs ── */
.msp-ic{{vertical-align:-3px;flex-shrink:0;}}
.msp-chip{{display:inline-flex;align-items:center;gap:7px;min-width:0;}}
.msp-chip-t{{line-height:1.15;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
/* Signals-feed event pill (icon + clean label) */
.sig-pill{{display:inline-flex;align-items:center;gap:5px;background:rgba(129,140,248,.12);color:#a5b4fc;
    font-size:11px;font-weight:700;padding:2px 9px;border-radius:6px;border:1px solid rgba(129,140,248,.25);}}
.sig-pill svg{{color:#a5b4fc;flex-shrink:0;}}
/* Detail-page "Recent alerts for TICKER" strip */
.ra-lbl{{font-size:11px;font-weight:800;color:#4a5e7a;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:9px;}}
.ra-strip{{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px;}}
.ra-chip{{flex:0 0 auto;min-width:188px;max-width:260px;background:#0c1322;border:1px solid #1a2740;border-radius:11px;padding:10px 13px;}}
.ra-chip:hover{{border-color:rgba(99,102,241,0.4);}}
.ra-h{{display:flex;align-items:center;gap:7px;margin-bottom:5px;}}
.ra-h svg{{color:#818cf8;flex-shrink:0;}}
.ra-cat{{font-size:12px;font-weight:800;color:#eef3fc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.ra-age{{margin-left:auto;font-size:10px;color:#4a5e7a;white-space:nowrap;flex-shrink:0;}}
.ra-sub{{font-size:11px;color:#8da3c4;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
/* ── Clickable tiles ── the .ctile shows the visual card; the invisible st.button that
   follows it in the SAME column is stretched to cover the whole card so a click anywhere
   fires the button. The overlay is anchored on the COLUMN (we force the intermediate
   Streamlit wrappers to be static / not a positioning context, which is what collapsed
   the hit-area to 0 before), and the .ctile is made pointer-transparent so the click
   always reaches the button. Hover feedback is driven by the column so it still lights
   up even though the card itself ignores the pointer. ── */
[data-testid="stColumn"]:has(.ctile),[data-testid="column"]:has(.ctile){{position:relative !important;}}
/* neutralise any positioning on the intermediate Streamlit wrappers so the absolute
   overlay below anchors to the COLUMN (the relative ancestor), not a nested wrapper. */
[data-testid="stColumn"]:has(.ctile) [data-testid="stVerticalBlock"],[data-testid="stColumn"]:has(.ctile) [data-testid="stVerticalBlockBorderWrapper"],[data-testid="column"]:has(.ctile) [data-testid="stVerticalBlock"],[data-testid="column"]:has(.ctile) [data-testid="stVerticalBlockBorderWrapper"]{{position:static !important;}}
/* the MARKER's own container takes no space */
.element-container:has(.ctile-ov){{display:none !important;}}
/* the button container = the element-container immediately AFTER the marker. Stretch it
   over the whole card. This .element-container:has(marker)+next form is the proven one. */
.element-container:has(.ctile-ov)+.element-container{{position:absolute !important;top:0 !important;left:0 !important;width:100% !important;height:100% !important;margin:0 !important;z-index:6 !important;pointer-events:auto !important;}}
.element-container:has(.ctile-ov)+.element-container .stButton,.element-container:has(.ctile-ov)+.element-container .stButton>button{{height:100% !important;width:100% !important;}}
.element-container:has(.ctile-ov)+.element-container .stButton>button{{min-height:0 !important;opacity:0 !important;border:none !important;background:transparent !important;cursor:pointer !important;padding:0 !important;box-shadow:none !important;transform:none !important;}}
.ctile{{cursor:pointer;height:100%;pointer-events:none;}}
/* column-hover → light up the card inside (pointer-events:none on .ctile means the
   card's own :hover can't fire, so we drive it from the column) */
[data-testid="stColumn"]:has(.ctile):hover .cat-tile,[data-testid="stColumn"]:has(.ctile):hover .cv-card{{border-color:rgba(99,102,241,0.6) !important;}}
[data-testid="stColumn"]:has(.ctile):hover .th-head{{border-color:rgba(129,140,248,0.55) !important;background:linear-gradient(135deg,#0f1830,#0b1322) !important;}}
[data-testid="stColumn"]:has(.ctile):hover .sw-pc-col{{border-color:rgba(129,140,248,0.5) !important;}}
/* ── Scroll-reveal via NATIVE CSS scroll-driven animations (no JS). Each .reveal
   fades + slides up as it scrolls into view, on its own view() timeline. Gated behind
   @supports so browsers without it (Safari/older FF) just show content normally —
   never hidden. ── */
@keyframes msp-reveal{{from{{opacity:0;transform:translateY(34px);}}to{{opacity:1;transform:none;}}}}
@supports (animation-timeline: view()){{
  .reveal{{animation:msp-reveal linear both;animation-timeline:view();animation-range:entry 2% cover 26%;}}
}}
@media(prefers-reduced-motion:reduce){{.reveal{{animation:none !important;opacity:1 !important;transform:none !important;}}}}
/* Container width is governed by the single authoritative rule lower in this
   stylesheet (search APP_MAX_WIDTH). Do not set max-width here — competing
   rules were what caused the edge-to-edge stretch on wide monitors. */
div.block-container{{padding:0 !important;}}
section.main>div{{padding-top:0 !important;}}
/* ── Sidebar (Desktop default — 240px fixed) ── */
[data-testid="stSidebar"]{{
    background:#080c18 !important;
    border-right:1px solid {BORDER} !important;
    width:240px !important;min-width:240px !important;max-width:240px !important;
    position:sticky !important;top:0 !important;
    height:100vh !important;
    transition: margin-left 0.3s ease !important;
}}
[data-testid="stSidebar"]>div{{
    padding:0 !important;
    height:100vh !important;
    overflow-y:auto !important;
}}

/* ── SIDEBAR BUTTON VISIBILITY — always readable by default ── */
[data-testid="stSidebar"] .stButton>button{{
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.18) !important;
    color: #d1dce8 !important;
    font-weight: 600 !important;
    transition: all 0.2s ease;
}}
[data-testid="stSidebar"] .stButton>button:hover{{
    background: rgba(99,102,241,0.18) !important;
    border-color: rgba(99,102,241,0.55) !important;
    color: #fff !important;
}}
[data-testid="stSidebar"] .stButton>button[kind="primary"]{{
    background: #6366f1 !important;
    border-color: #6366f1 !important;
    color: #fff !important;
    font-weight: 700 !important;
}}

/* ── Collapse/Expand Button — ALWAYS VISIBLE — moved to right when open ── */
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"],
button[kind="header"][data-testid="baseButton-header"],
[data-testid="stSidebarCollapsedControl"]{{
    visibility: visible !important;
    opacity: 1 !important;
    display: flex !important;
    position: fixed !important;
    top: 12px !important;
    left: 12px !important;
    z-index: 999999 !important;
    background: rgba(13, 21, 37, 0.95) !important;
    backdrop-filter: blur(12px) !important;
    -webkit-backdrop-filter: blur(12px) !important;
    border: 1px solid rgba(96, 165, 250, 0.4) !important;
    border-radius: 8px !important;
    color: #a5b4fc !important;
    width: 36px !important;
    height: 36px !important;
    padding: 6px !important;
    box-shadow: 0 4px 14px rgba(0,0,0,0.4) !important;
    cursor: pointer !important;
    align-items: center !important;
    justify-content: center !important;
}}
[data-testid="collapsedControl"]:hover,
[data-testid="stSidebarCollapseButton"]:hover{{
    background: rgba(37, 99, 235, 0.25) !important;
    border-color: rgba(96, 165, 250, 0.8) !important;
    transform: scale(1.05) !important;
}}
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapseButton"] svg{{
    width: 18px !important;
    height: 18px !important;
    color: #a5b4fc !important;
    fill: #a5b4fc !important;
}}

/* When sidebar is OPEN, move the collapse button to the RIGHT side of the sidebar (inside it) */
[data-testid="stSidebar"][aria-expanded="true"] ~ * [data-testid="collapsedControl"],
[data-testid="stSidebar"]:not([aria-expanded="false"]) ~ * [data-testid="collapsedControl"]{{
    left: 196px !important;  /* near right edge of 240px sidebar */
}}

/* ── Base Button ── */
/* ── MSP button system ── a bespoke 'indigo glass' treatment: subtle vertical
   sheen, a faint indigo border + inner top-highlight, a crisp 10px radius, and a
   tactile lift+glow on hover. One consistent look across every plain button. */
.stButton>button{{
    background:linear-gradient(180deg, rgba(129,140,248,0.07), rgba(129,140,248,0.02)) !important;
    border:1px solid rgba(129,140,248,0.22) !important;
    color:#c3d0e6 !important;
    border-radius:10px !important;
    font-family:'Inter',sans-serif !important;
    font-size:13px !important;font-weight:600 !important;
    padding:8px 16px !important;
    min-height:40px !important;
    transition:all 0.18s cubic-bezier(.4,0,.2,1) !important;
    width:100% !important;
    display:flex !important;align-items:center !important;justify-content:center !important;
    -webkit-font-smoothing:antialiased !important;
    text-rendering:optimizeLegibility !important;
    letter-spacing:0.2px !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05) !important;
}}
.stButton>button:hover{{
    background:linear-gradient(180deg, rgba(99,102,241,0.20), rgba(99,102,241,0.07)) !important;
    border-color:rgba(129,140,248,0.6) !important;
    color:#e8ecff !important;
    transform:translateY(-1px) !important;
    box-shadow:0 5px 18px rgba(99,102,241,0.28), inset 0 1px 0 rgba(255,255,255,0.08) !important;
}}
.stButton>button:active{{
    transform:translateY(0) !important;
    box-shadow:inset 0 2px 6px rgba(0,0,0,0.25) !important;
}}
.stButton>button[kind="primary"]{{
    background:linear-gradient(135deg,#4f46e5,#6366f1 55%,#8b5cf6) !important;
    border:1px solid rgba(139,92,246,0.55) !important;color:#fff !important;font-weight:800 !important;
    letter-spacing:0.2px !important;
    box-shadow:0 4px 18px rgba(99,102,241,0.34) !important;
}}
.stButton>button[kind="primary"]:hover{{
    background:linear-gradient(135deg,#4338ca,#6366f1 55%,#7c3aed) !important;
    box-shadow:0 6px 24px rgba(124,58,237,0.5) !important;
    transform:translateY(-1px) !important;
}}

/* ── Sidebar nav ── crisp list rows with a rounded indigo hover (Linear-style) ── */
[data-testid="stSidebar"] .stButton>button{{
    background:transparent !important;border:1px solid transparent !important;
    border-radius:9px !important;
    color:#6b7a93 !important;font-size:13px !important;font-weight:600 !important;
    padding:9px 14px !important;text-align:left !important;
    min-height:38px !important;margin:1px 8px !important;width:calc(100% - 16px) !important;
    justify-content:flex-start !important;
    transition:all 0.15s cubic-bezier(.4,0,.2,1) !important;
}}
[data-testid="stSidebar"] .stButton>button:hover{{
    background:rgba(99,102,241,0.12) !important;
    border-color:rgba(129,140,248,0.28) !important;color:#e8ecff !important;
    transform:none !important;
}}
[data-testid="stSidebar"] .stButton>button[kind="primary"]{{
    background:linear-gradient(135deg,#4f46e5,#6366f1 55%,#8b5cf6) !important;
    border:1px solid rgba(139,92,246,0.55) !important;color:#fff !important;font-weight:800 !important;
}}

/* ── Gold premium button ── */
.gold-btn .stButton>button,
button[aria-label="👑 Go Premium"],
button[aria-label="👑 Unlock Premium"],
button[aria-label="👑 Upgrade to Premium"] {{
    background:linear-gradient(135deg,#92400e 0%,{GOLD2} 35%,{GOLD} 60%,#fcd34d 100%) !important;
    border:1px solid {GOLD} !important;
    color:#1a0800 !important;font-weight:800 !important;font-size:14px !important;
    box-shadow:0 4px 20px rgba(245,158,11,0.4),0 0 0 1px rgba(245,158,11,0.2) !important;
    border-radius:10px !important;min-height:48px !important;letter-spacing:0.3px !important;
}}
button[aria-label="👑 Go Premium"]:hover,
button[aria-label="👑 Unlock Premium"]:hover,
button[aria-label="👑 Upgrade to Premium"]:hover {{
    background:linear-gradient(135deg,#b45309 0%,{GOLD} 40%,#fcd34d 70%,#fef3c7 100%) !important;
    box-shadow:0 8px 32px rgba(245,158,11,0.6),0 0 0 1px rgba(245,158,11,0.4) !important;
    transform:translateY(-1px) !important;
}}

/* ── Nav CSS ── (matches the app-wide indigo-glass button system) */
.sw-nav .stButton>button{{
    font-size:13px !important;font-weight:600 !important;
    padding:6px 12px !important;min-height:38px !important;height:38px !important;
    border:1px solid rgba(129,140,248,0.18) !important;
    background:linear-gradient(180deg, rgba(129,140,248,0.07), rgba(129,140,248,0.02)) !important;color:#c3d0e6 !important;
    border-radius:10px !important;white-space:nowrap !important;width:100% !important;
    transition:all 0.16s cubic-bezier(.4,0,.2,1) !important;
}}
.sw-nav .stButton>button:hover{{
    border-color:rgba(129,140,248,0.5) !important;
    background:linear-gradient(180deg, rgba(99,102,241,0.18), rgba(99,102,241,0.07)) !important;color:#e8ecff !important;
    transform:translateY(-1px) !important;box-shadow:0 4px 14px rgba(99,102,241,0.22) !important;
}}
.sw-nav .stButton>button[kind="primary"]{{
    background:linear-gradient(135deg,#4f46e5,#6366f1 55%,#8b5cf6) !important;
    border:1px solid rgba(139,92,246,0.55) !important;
    color:#fff !important;font-weight:800 !important;
}}

/* ── Logo overlay ── */
.element-container:has(.sw-logo-click-target)+.element-container{{
    height:0px !important;overflow:visible !important;margin:0 !important;padding:0 !important;
}}
.element-container:has(.sw-logo-click-target)+.element-container .stButton>button{{
    position:relative !important;top:-48px !important;left:0 !important;
    width:180px !important;height:48px !important;min-height:48px !important;
    opacity:0 !important;cursor:pointer !important;z-index:999 !important;
    background:transparent !important;border:none !important;box-shadow:none !important;
}}

/* ── Topbar vertical center ── */
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type{{
    align-items:center !important;min-height:60px !important;
}}
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type>[data-testid="column"]{{
    display:flex !important;align-items:center !important;
    padding-top:0 !important;padding-bottom:0 !important;
}}
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type>[data-testid="column"]>div{{width:100% !important;}}

/* ── Cards ── */
.card{{background:{CARD};border:1px solid {BORDER};border-radius:10px;padding:16px;margin-bottom:10px;transition:border-color 0.2s;}}
.card:hover{{border-color:rgba(99,102,241,0.3);}}
.card-blue{{background:linear-gradient(135deg,#05112a,{CARD});border-color:rgba(99,102,241,0.25);}}
.card-gold{{background:linear-gradient(135deg,#120d00,{CARD});border-color:rgba(245,158,11,0.3);}}
.card-green{{background:linear-gradient(135deg,#001a0e,{CARD});border-color:rgba(34,197,94,0.25);}}
.card-purple{{background:linear-gradient(135deg,#0e0520,{CARD});border-color:rgba(139,92,246,0.25);}}

/* ── Pricing cards ── */
.price-card{{
    background:{CARD};border:1px solid {BORDER};border-radius:14px;
    padding:28px 24px;height:100%;
    transition:all 0.25s ease;
}}
.price-card:hover{{
    border-color:rgba(99,102,241,0.5);
    box-shadow:0 8px 32px rgba(99,102,241,0.15);
    transform:translateY(-2px);
}}
.price-card-featured{{
    background:linear-gradient(160deg,#060f2a,{CARD});
    border:2px solid {BLUE};border-radius:14px;padding:28px 24px;height:100%;
    box-shadow:0 8px 40px rgba(99,102,241,0.2);
    transition:all 0.25s ease;
}}
.price-card-featured:hover{{
    border-color:#6366f1;box-shadow:0 16px 60px rgba(99,102,241,0.35);
    transform:translateY(-3px);
}}
.price-card-gold{{
    background:linear-gradient(160deg,#1a0d00,#120800,{CARD});
    border:2px solid {GOLD};border-radius:14px;padding:28px 24px;height:100%;
    box-shadow:0 8px 40px rgba(245,158,11,0.2);
    transition:all 0.25s ease;
}}
.price-card-gold:hover{{
    border-color:#fcd34d;box-shadow:0 16px 60px rgba(245,158,11,0.35);
    transform:translateY(-3px);
}}

/* ── Stock row ── */
.sr{{background:{CARD};border:1px solid {BORDER};border-radius:10px;padding:14px 16px;
     margin-bottom:6px;transition:all 0.15s ease;cursor:pointer;}}
.sr:hover{{border-color:rgba(99,102,241,0.4);background:#101828;}}
.sr-tick{{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;color:#818cf8;}}
.sr-name{{font-size:11px;color:#2a3a52;margin-top:2px;}}
.sr-why{{font-size:12px;color:#3d5270;margin-top:4px;line-height:1.5;}}
.sr-price{{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:700;color:#e2e8f0;}}

/* ── Badges ── */
.b{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;margin-right:3px;vertical-align:middle;}}
.b-bull{{background:#05260f;color:#4ade80;border:1px solid rgba(74,222,128,.3);}}
.b-bear{{background:#260505;color:#f87171;border:1px solid rgba(248,113,113,.3);}}
.b-neu {{background:#151b28;color:#64748b;border:1px solid rgba(100,116,139,.3);}}
.b-hot {{background:#260d00;color:#fb923c;border:1px solid rgba(251,146,60,.3);}}
.b-prem{{background:#201000;color:{GOLD};border:1px solid rgba(245,158,11,.3);}}
.b-blue{{background:#060f2a;color:#a5b4fc;border:1px solid rgba(165,180,252,.3);}}

/* Score pill */
.sp{{display:inline-block;padding:3px 10px;border-radius:5px;font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;}}
.sp-hi{{background:#05260f;color:#4ade80;border:1px solid rgba(74,222,128,.3);}}
.sp-md{{background:#201000;color:{GOLD};border:1px solid rgba(245,158,11,.3);}}
.sp-lo{{background:#260505;color:#f87171;border:1px solid rgba(248,113,113,.3);}}

/* Index widget */
.idx-w{{background:{CARD};border:1px solid {BORDER};border-radius:9px;padding:14px 16px;}}
.idx-name{{font-size:10px;color:#4a5e7a;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;}}
.idx-price{{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:#e2e8f0;}}

/* Insight block */
.ins{{background:#0a1020;border-left:3px solid {BLUE};border-radius:0 7px 7px 0;padding:11px 14px;margin:5px 0;}}
.ins-bull{{border-left-color:{GREEN};}}
.ins-bear{{border-left-color:{RED};}}
.ins-label{{font-size:12px;font-weight:700;color:#c9d3e0;margin-bottom:4px;}}
.ins-text{{font-size:12px;color:#374f6e;line-height:1.6;}}

/* Section header */
.sec-hd{{font-size:15px;font-weight:700;color:#e2e8f0;display:flex;align-items:center;gap:8px;
         padding-bottom:10px;border-bottom:1px solid {BORDER};margin-bottom:14px;}}

/* Tags */
.tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;}}
.tag-free{{background:rgba(34,197,94,0.12);color:#4ade80;border:1px solid rgba(34,197,94,0.3);}}
.tag-prem{{background:rgba(245,158,11,0.12);color:{GOLD};border:1px solid rgba(245,158,11,0.3);}}
.tag-live{{background:rgba(99,102,241,0.12);color:#a5b4fc;border:1px solid rgba(99,102,241,0.3);}}

/* Stats */
.stat{{background:{CARD};border:1px solid {BORDER};border-radius:9px;padding:12px 14px;text-align:center;}}
.stat-v{{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#e2e8f0;}}
.stat-l{{font-size:10px;color:#2a3a52;text-transform:uppercase;letter-spacing:.5px;margin-top:3px;}}

/* Mover row */
.mv{{display:flex;justify-content:space-between;align-items:center;padding:7px 0;
     border-bottom:1px solid rgba(255,255,255,.04);font-size:13px;}}
.mv:last-child{{border-bottom:none;}}

/* Lock */
.lock{{background:rgba(7,9,15,.96);border:1px solid rgba(245,158,11,.3);border-radius:10px;padding:36px 24px;text-align:center;}}

/* Disclaimer */
.disc{{background:#0a1020;border-left:3px solid #854d0e;border-radius:0 7px 7px 0;padding:12px 16px;font-size:11px;color:#2a3752;line-height:1.7;}}

/* Page padding */
.pg{{padding:20px 28px 40px;}}
.div-line{{border-bottom:1px solid {BORDER};margin:20px 0;}}

/* ── Page container — matches the APP_MAX_WIDTH container so styled HTML
   blocks (greetings, headers) align with Streamlit widgets below them. ── */
.page-wrap{{
    max-width:1200px;
    width:100%;
    margin:0 auto;
    padding:0;
    box-sizing:border-box;
}}
.page-wrap.pw-narrow{{
    max-width:1200px;
}}
/* ════════════════════════════════════════════════════════════
   APP_MAX_WIDTH — the SINGLE source of truth for content width.
   Targets every container selector Streamlit has used across versions
   so content is centered with comfortable side margins on wide monitors
   instead of stretching edge-to-edge (the dead-space problem).
════════════════════════════════════════════════════════════ */
[data-testid="stMainBlockContainer"],
[data-testid="stAppViewBlockContainer"],
section.main > div.block-container,
div.block-container,
.block-container{{
    max-width:1200px !important;
    margin-left:auto !important;
    margin-right:auto !important;
    padding-left:32px !important;
    padding-right:32px !important;
    padding-top:8px !important;
}}
@media(max-width:900px){{
    .page-wrap{{padding:12px 14px 28px !important;}}
    [data-testid="stMainBlockContainer"],
    [data-testid="stAppViewBlockContainer"],
    section.main > div.block-container,
    div.block-container,
    .block-container{{padding-left:16px !important;padding-right:16px !important;}}
}}
/* Mobile safety: prevent any horizontal overflow / sideways scroll, and let
   long monospace tickers and prices wrap instead of pushing layout wide. */
@media(max-width:560px){{
    .page-wrap.pw-narrow{{padding:10px 12px 28px !important;}}
    .sr{{padding:12px !important;}}
    .stat{{padding:10px !important;}}
    [data-testid="stHorizontalBlock"]{{flex-wrap:wrap !important;}}
}}
html,body,[data-testid="stAppViewContainer"]{{overflow-x:hidden;}}

/* Hero */
.hero-h1{{font-size:48px;font-weight:900;color:#f1f5f9;line-height:1.05;letter-spacing:-2px;margin-bottom:12px;}}
.hero-h1 .hi{{color:{BLUE};}}
.hero-h1 .hg{{color:{GOLD};}}
.hero-sub{{font-size:16px;color:#3d5270;line-height:1.75;max-width:480px;margin-bottom:28px;}}

/* Forms */
.stTextInput>div>div>input,.stTextArea>div>div>textarea,.stSelectbox>div>div{{
    background:#0e1421 !important;border:1px solid rgba(255,255,255,.1) !important;
    color:#d1d9e6 !important;border-radius:7px !important;font-family:'Inter',sans-serif !important;font-size:13px !important;
}}
.stTextInput>div>div>input:focus{{border-color:{BLUE} !important;box-shadow:0 0 0 3px rgba(99,102,241,.15) !important;}}
[data-testid="InputInstructions"]{{display:none !important;}}
.stTextInput>div{{margin-bottom:0 !important;}}
.streamlit-expanderHeader{{background:#0e1421 !important;border:1px solid {BORDER} !important;border-radius:7px !important;color:#6b7fa0 !important;font-size:13px !important;}}
.streamlit-expanderContent{{background:#0a1020 !important;border:1px solid {BORDER} !important;border-top:none !important;}}
[data-testid="stTabs"]>div{{border-color:{BORDER} !important;}}
[data-testid="stTab"]{{font-size:13px !important;font-weight:500 !important;color:#4a5e7a !important;}}
[aria-selected="true"][data-testid="stTab"]{{color:#a5b4fc !important;border-bottom-color:{BLUE} !important;}}
.stProgress>div>div{{background:#141927 !important;height:5px !important;border-radius:3px !important;}}
.stProgress>div>div>div{{background:linear-gradient(90deg,{BLUE},#6366f1) !important;border-radius:3px !important;}}
[data-testid="stDataFrame"]{{border:1px solid {BORDER} !important;border-radius:10px !important;overflow:hidden !important;}}

/* Footer */
.sw-footer-wrap{{
    width:100vw;margin-left:calc(-50vw + 50%);
    background:#050810;border-top:1px solid {BORDER};
    padding:40px 64px 28px;margin-top:40px;box-sizing:border-box;
}}

/* Equal column heights */
[data-testid="column"]{{display:flex;flex-direction:column;}}
[data-testid="column"]>.element-container{{flex:1;display:flex;flex-direction:column;}}
[data-testid="column"] .stButton{{display:flex;}}
[data-testid="column"] .stButton>button{{flex:1;}}

/* ── Download button styling ── */
[data-testid="stDownloadButton"]>button{{
    background:rgba(99,102,241,0.1)!important;
    border:1px solid rgba(99,102,241,0.3)!important;
    color:#a5b4fc!important;
    font-size:13px!important;font-weight:600!important;
    border-radius:8px!important;
    transition:all 0.2s!important;
}}
[data-testid="stDownloadButton"]>button:hover{{
    background:rgba(99,102,241,0.2)!important;
    border-color:#6366f1!important;
}}
/* ── Tabs styling (admin panel + settings) ── */
[data-testid="stTabs"]>div>[role="tablist"]{{
    gap:10px!important;border-bottom:1px solid rgba(255,255,255,0.08)!important;
    flex-wrap:wrap!important;             /* never clip/scroll tabs on narrow widths */
    row-gap:4px!important;
    margin-bottom:14px!important;         /* breathing room before tab content */
    padding-bottom:2px!important;
}}
button[role="tab"]{{
    font-size:13px!important;font-weight:500!important;
    padding:9px 18px!important;border-radius:8px 8px 0 0!important;
    color:#4a5e7a!important;
    white-space:nowrap!important;
    line-height:1.3!important;
    transition:color .15s ease, background .15s ease!important;
}}
button[role="tab"]:hover{{
    color:#a5b4fc!important;background:rgba(99,102,241,0.05)!important;
}}
button[role="tab"][aria-selected="true"]{{
    color:#e2e8f0!important;font-weight:700!important;
    background:rgba(99,102,241,0.08)!important;
    border-bottom:2px solid #6366f1!important;
}}
/* Keep the little tab highlight bar aligned with the roomier tabs */
[data-testid="stTabs"] [data-baseweb="tab-highlight"]{{display:none!important;}}
/* ── Expander styling ── */
.streamlit-expanderHeader{{
    font-size:13px!important;font-weight:600!important;color:#6b7fa0!important;
}}
/* ── Success/error/info messages ── */
[data-testid="stAlert"]{{border-radius:10px!important;font-size:13px!important;}}
/* ── Sticky page back button ── */
.sw-back-btn-wrap{{
    position:sticky !important;
    top:8px !important;
    z-index:50 !important;
    margin-bottom:12px !important;
    background:rgba(7,9,15,0.92) !important;
    backdrop-filter:blur(8px) !important;
    -webkit-backdrop-filter:blur(8px) !important;
    padding:6px 0 !important;
}}
.sw-back-btn-wrap .stButton>button{{
    background:rgba(255,255,255,0.04) !important;
    border:1px solid rgba(99,102,241,0.3) !important;
    color:#a5b4fc !important;
    font-size:12px !important;
    min-height:34px !important;
    width:auto !important;
    padding:0 16px !important;
    max-width:120px !important;
}}
.sw-back-btn-wrap .stButton>button:hover{{
    background:rgba(99,102,241,0.15) !important;
    border-color:{BLUE} !important;
}}

/* ── Mobile & Tablet Responsive ── */
@media (max-width:900px) {{
    /* Marker divs - hidden, just used as anchors */
    .sw-desktop-nav-anchor, .sw-mobile-nav-anchor {{ display: none; }}

    /* ════════════════════════════════════════════════════════════
       MOBILE: Hide desktop topbar, show mobile topbar
       
       Pure HTML/CSS topbar — guaranteed to work because they're not
       Streamlit widgets but raw HTML inside our wrapper divs.
    ════════════════════════════════════════════════════════════ */
    .sw-desktop-topbar {{ display: none !important; }}
    .sw-mobile-topbar-bar {{ display: flex !important; }}
    .sw-nav {{ display: none !important; }}
}}
@media (min-width: 901px) {{
    .sw-desktop-nav-anchor, .sw-mobile-nav-anchor {{ display: none; }}
    .sw-desktop-topbar {{ display: flex !important; }}
    .sw-mobile-topbar-bar {{ display: none !important; }}
}}

/* ── DESKTOP TOPBAR styling (pure HTML) ── */
.sw-desktop-topbar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 0;          /* reduced vertical height */
    margin: 0 0 12px 0;       /* tighter spacing under header */
    gap: 24px;
    min-height: 56px;        /* shorter than before */
}}
.sw-topbar-logo {{
    flex-shrink: 0;
    margin-right: 24px;
}}
.sw-topbar-nav {{
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: nowrap;
    margin-right: auto;       /* push nav farther left, away from user widget */
    margin-left: 12px;
}}
.sw-topbar-link {{
    font-family: 'Inter', sans-serif;
    font-size: 15px;          /* bigger */
    font-weight: 600;
    color: #cbd5e1 !important;
    text-decoration: none !important;
    padding: 11px 22px;       /* larger touch targets */
    border: 1px solid rgba(255,255,255,0.18);
    background: rgba(255,255,255,0.05);
    border-radius: 8px;
    transition: all 0.18s ease;
    white-space: nowrap;
    cursor: pointer;
    min-width: 92px;          /* uniform pill width */
    text-align: center;
}}
.sw-topbar-link:hover {{
    border-color: rgba(99,102,241,0.6);
    background: rgba(99,102,241,0.12);
    color: #fff !important;
}}
.sw-topbar-link.active {{
    background: #6366f1 !important;
    border-color: #6366f1 !important;
    color: #fff !important;
    font-weight: 700 !important;
}}
.sw-topbar-link.primary {{
    background: #6366f1 !important;
    border-color: #6366f1 !important;
    color: #fff !important;
    font-weight: 700 !important;
}}
.sw-topbar-link.primary:hover {{
    background: #4f46e5 !important;
    box-shadow: 0 4px 16px rgba(99,102,241,0.4);
}}
.sw-topbar-user {{
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
}}
.sw-topbar-icon {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border: 1px solid rgba(255,255,255,0.18);
    background: rgba(255,255,255,0.05);
    border-radius: 8px;
    color: #cbd5e1 !important;
    text-decoration: none !important;
    font-size: 16px;
    transition: all 0.18s ease;
}}
.sw-topbar-icon:hover {{
    background: rgba(99,102,241,0.12);
    border-color: rgba(99,102,241,0.5);
    color: #fff !important;
}}

/* ── MOBILE TOPBAR styling (pure HTML) ── */
.sw-mobile-topbar-bar {{
    display: none;  /* shown only on mobile via media query above */
    align-items: center;
    justify-content: space-between;
    padding: 8px 4px;
    margin-bottom: 12px;
}}
.sw-mobile-logo {{ text-decoration: none !important; }}
.sw-mobile-icon {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border: 1px solid rgba(255,255,255,0.15);
    background: rgba(255,255,255,0.04);
    border-radius: 8px;
    color: #a8bdd4 !important;
    text-decoration: none !important;
    font-size: 18px;
}}

    /* Hero text */
    .hero-h1{{font-size:32px !important;letter-spacing:-1px !important;}}
    .hero-sub{{font-size:14px !important;}}
    /* Feature grids → single column */
    .sw-feat-grid{{grid-template-columns:1fr !important;}}
    /* Page padding */
    .pg{{padding:12px 14px 28px !important;}}
    /* Footer */
    .sw-footer-wrap{{padding:24px 20px 20px !important;}}

    /* ════════════════════════════════════════════════════════════
       MOBILE SIDEBAR — narrower, overlay-style, collapsed by default
    ════════════════════════════════════════════════════════════ */
    [data-testid="stSidebar"]{{
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        height: 100vh !important;
        width: 75vw !important;
        min-width: 75vw !important;
        max-width: 280px !important;
        z-index: 999990 !important;
        box-shadow: 4px 0 30px rgba(0, 0, 0, 0.5) !important;
        transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1) !important;
    }}
    /* Sidebar collapsed = slide off-screen */
    [data-testid="stSidebar"][aria-expanded="false"]{{
        transform: translateX(-100%) !important;
        box-shadow: none !important;
    }}
    /* Make sure main content doesn't get pushed by sidebar on mobile */
    section.main, [data-testid="stMain"]{{
        margin-left: 0 !important;
        width: 100vw !important;
    }}
    /* Backdrop overlay when sidebar open on mobile */
    [data-testid="stSidebar"][aria-expanded="true"]::after{{
        content: '';
        position: fixed;
        top: 0; left: 100%;
        width: 100vw;
        height: 100vh;
        background: rgba(0, 0, 0, 0.6);
        backdrop-filter: blur(2px);
        z-index: -1;
        pointer-events: auto;
    }}

    /* Collapse button on mobile - bigger and more visible */
    [data-testid="collapsedControl"],
    [data-testid="stSidebarCollapseButton"]{{
        width: 44px !important;
        height: 44px !important;
        top: 16px !important;
        left: 16px !important;
    }}
    [data-testid="collapsedControl"] svg,
    [data-testid="stSidebarCollapseButton"] svg{{
        width: 22px !important;
        height: 22px !important;
    }}
    /* When sidebar OPEN on mobile, move collapse button inside sidebar at top right */
    [data-testid="stSidebar"][aria-expanded="true"] ~ * [data-testid="collapsedControl"]{{
        left: calc(75vw - 56px) !important;
        max-width: 280px !important;
    }}

    /* Stack hero columns */
    [data-testid="stHorizontalBlock"]{{flex-wrap:wrap !important;}}
    [data-testid="stHorizontalBlock"] [data-testid="column"]{{min-width:100% !important;flex:none !important;}}
    /* Topbar shrink */
    .sw-nav .stButton>button{{font-size:11px !important;padding:4px 8px !important;}}
    /* Cards */
    .card{{padding:12px 14px !important;}}
}}
@media (max-width:600px) {{
    .hero-h1{{font-size:26px !important;}}
    .hero-sub{{font-size:13px !important;}}
    /* Trust bar wrap */
    .sw-trust-bar{{flex-wrap:wrap !important;gap:16px !important;padding:16px !important;}}
}}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
CATEGORIES = {
    "🔥 Trending Now":  [],
    "📡 Social Buzz":   ["GME","AMC","BBIG","ATER","SPCE","HOOD","MSTR","PLTR","SOUN","BBAI"],
    "💻 Tech":          ["AAPL","MSFT","GOOGL","META","AMZN","NVDA","AMD","INTC","QCOM","AVGO","CRM","ADBE","NOW","SNOW","NET","DDOG","CRWD"],
    "🤖 AI":            ["NVDA","AMD","PLTR","MSFT","GOOGL","SOUN","BBAI","AI","ASTS","IONQ","QUBT","RGTI","SMCI","ARM","ALAB","MRVL"],
    "⚡ EV":            ["TSLA","RIVN","LCID","NIO","LI","XPEV","F","GM","CHPT","BLNK","ACHR","JOBY"],
    "🧬 Biotech":       ["MRNA","BNTX","NVAX","VRTX","REGN","BIIB","GILD","AMGN","SRPT","EDIT","CRSP","BEAM"],
    "📊 S&P 500":       ["AAPL","MSFT","AMZN","GOOGL","META","TSLA","NVDA","JPM","JNJ","V","PG","MA","UNH","HD","XOM","CVX","LLY","ABBV","MRK","PFE","BAC","WMT"],
    "💹 NASDAQ":        ["AAPL","MSFT","AMZN","NVDA","META","GOOGL","TSLA","AVGO","COST","AMD","CSCO","ADBE","QCOM","AMGN","INTU","ISRG","REGN","PANW"],
    "🔬 Small Cap":     ["WKHS","ATER","SPCE","SOUN","BBAI","ASTS","IONQ","QUBT","RGTI","ACHR"],
}

# COMPOSITE_CATS now lives in scoring.py (shared with the alerts worker) and is
# imported at the top of this file.

SECTOR_ETFS = {"Technology":"XLK","Healthcare":"XLV","Financials":"XLF","Energy":"XLE","Cons Disc":"XLY","Industrials":"XLI","Materials":"XLB","Utilities":"XLU","Real Estate":"XLRE","Comm Svcs":"XLC"}
INDEXES     = {"NASDAQ":"^IXIC","S&P 500":"^GSPC","DOW":"^DJI","VIX":"^VIX","Russell":"^RUT"}
BROAD_UNI   = ["AAPL","MSFT","NVDA","AMD","TSLA","META","AMZN","GOOGL","PLTR","MSTR","GME","AMC","RIVN","MRNA","BNTX","SMCI","ARM","SOUN","ASTS","IONQ","JPM","BAC","XOM","LLY","ABBV","AVGO","QCOM","IBM","SPCE","BBAI","QUBT","RIVN"]

FREE_COMPOSITE  = [k for k,(d,t) in COMPOSITE_CATS.items() if t=="free"]
PREM_COMPOSITE  = [k for k,(d,t) in COMPOSITE_CATS.items() if t=="premium"]

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
def init():
    # ── Always reload users_db from disk on every session start ──
    # This ensures registered users survive container restarts
    if "users_db" not in st.session_state:
        st.session_state.users_db = _get_global_db()

    if "initialized" in st.session_state:
        # Even after init, refresh users_db from disk so new signups in other tabs are visible
        st.session_state.users_db = _get_global_db()
        return
    st.session_state.initialized=True
    st.session_state.update({
        "page":"landing","user":None,"role":"guest",
        "watchlist":[],"alerts":[],"saved_screeners":[],
        "detail_ticker":None,"detail_data":{},"discover_cat":"__home__",
        "prev_page":None,"hero_panel":0,"_page_hist":[],
        "users_db":_get_global_db(),
        "site_stats":{"total_signups":1847,"premium_users":312,"daily_active":634,"conversion":16.9},
        "email_digest_enabled":False,"digest_frequency":"Daily",
        "ranking_sort":"SW Score","ranking_filter":"All",
    })
init()

# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────
def _client_key():
    """Best-effort client identifier for abuse RATE-LIMITING only (never authorization): the
    forwarded client IP when the proxy exposes it (Streamlit Cloud sets X-Forwarded-For), else a
    stable per-session random id. A bot spinning fresh sessions gets fresh keys, but the global
    signup circuit-breaker + honeypot + min-fill-time still catch that."""
    try:
        h = st.context.headers or {}
        ip = (h.get("X-Forwarded-For") or h.get("x-forwarded-for") or "").split(",")[0].strip()
        if ip:
            return "ip:" + ip
    except Exception:
        pass
    k = st.session_state.get("_client_fp")
    if not k:
        k = "sess:" + _secrets.token_hex(8)
        st.session_state["_client_fp"] = k
    return k

def login(email, pw):
    # Always reload users_db from disk first (in case signups happened in other tabs)
    st.session_state.users_db = _get_global_db()
    db=st.session_state.users_db
    # Tolerant email match: strip pasted whitespace, and fall back to a
    # case-insensitive lookup — "Chris@x.com" and a trailing space both used to
    # fail as "Invalid email or password" against a stored "chris@x.com".
    email = (email or "").strip()
    if email not in db:
        _m = next((k for k in db if isinstance(k, str) and k.strip().lower() == email.lower()), None)
        if _m:
            email = _m
    if email in db and verify_pw(pw, db[email].get("pw","")):
        # Lazy migration: transparently upgrade any legacy sha256 hash to bcrypt on a
        # successful login (so old accounts get the stronger hash without a reset).
        if HAS_BCRYPT and not _is_bcrypt_hash(db[email].get("pw","")):
            db[email]["pw"] = hp(pw)
            try: _save_global_db(db); save_user_to_file(email, db[email])
            except Exception: pass
        # Grant any paid-but-unclaimed upgrade for this email (Stripe finished while logged out)
        if apply_pending_upgrade(email, db):
            try: _save_global_db(db); save_user_to_file(email, db[email])
            except Exception: pass
        st.session_state.user={"email":email,"name":db[email]["name"]}
        st.session_state.role=db[email]["role"]
        # ── Persistent session: create a server-side token so the login
        #    survives hard refreshes / direct URLs / new tabs. ──
        st.session_state["_sid"] = new_session_token(email, db[email]["role"])
        # Load this user's alerts from file
        user_alerts_db = _read_json(ALERTS_DB_PATH, {})
        st.session_state.alerts = user_alerts_db.get(email, [])
        # Load watchlist from user record
        st.session_state.watchlist = db[email].get("watchlist", [])
        return True
    return False

def signup(email, pw, first_name, last_name=""):
    """Register a new user. Accepts first+last name (last name optional for backward compatibility)."""
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    # Always reload users_db from disk first
    st.session_state.users_db = _get_global_db()
    db=st.session_state.users_db
    if email in db: return False,"Account already exists."
    db[email]={
        "pw":hp(pw),
        "name":full_name,
        "first_name": first_name,
        "last_name": last_name,
        "role":"free",
        "verified":False,
        "joined":datetime.now().strftime("%Y-%m-%d"),
        "plan":"Free",
        "watchlist": [],
        "saved_screeners": [],
        "watchlist_notes": {},
        "recently_viewed": [],
    }
    # Grant premium if they already paid before creating the account (pending upgrade).
    apply_pending_upgrade(email, db)
    _save_global_db(db)  # persist to process-level store AND disk
    save_user_to_file(email, db[email])  # double-save to file for worker visibility
    st.session_state.site_stats["total_signups"]+=1
    _role = db[email]["role"]
    st.session_state.user={"email":email,"name":full_name}
    st.session_state.role=_role
    st.session_state["_sid"] = new_session_token(email, _role)
    return True,""

def logout():
    # ── Tear down the persistent session token (server-side + browser) ──
    tok = st.session_state.get("_sid", "")
    if tok:
        destroy_session_token(tok)
    st.session_state["_clear_sid_ls"] = True  # signals JS to wipe localStorage
    keys_to_clear = ["user","role","watchlist","alerts","saved_screeners","sel_plan",
                     "support_chat","_page_hist","prev_page","detail_ticker","detail_data",
                     "_redirect_url","_pay_success","_pay_error","_pay_cancelled",
                     "_login_welcome","_signup_success","_sid"]
    for k in keys_to_clear:
        st.session_state.pop(k, None)
    # Drop sid from the URL so a stale token can't re-auth
    try: st.query_params.pop("sid", None)
    except Exception: pass
    st.session_state["_logged_out"] = True
    nav("landing")

def _effective_role():
    """Authoritative role for the current user.

    Reads the role from the persisted user database rather than trusting a
    value that's only in session_state. This closes the bypass where a role in
    session/token could be stale or tampered with, and means an admin changing
    a user's role takes effect on the user's next action. Falls back to the
    session value only if the user isn't found in the DB (e.g. seed-only edge).
    """
    u = st.session_state.get("user")
    if not u:
        return "guest"
    email = u.get("email", "")
    try:
        db = st.session_state.get("users_db") or {}
        role = db.get(email, {}).get("role")
        if role:
            # keep session in sync so the rest of the app sees the live role
            if st.session_state.get("role") != role:
                st.session_state.role = role
            return role
    except Exception:
        pass
    return st.session_state.get("role", "free")

def is_owner():   return _effective_role()=="owner"
def is_admin():   return _effective_role() in ("owner","admin")
def is_premium(): return _effective_role() in ("owner","admin","premium")
def is_authed():  return st.session_state.get("user") is not None

# ── Single source of truth for page access control ──
# Maps page → minimum capability required. The router and any page that wants a
# belt-and-suspenders check both call can_access()/require_access() so there is
# exactly ONE place that defines who can see what. No query-param or
# session-state bypass: roles come from _effective_role() (the DB).
PAGE_ACCESS = {
    # auth-only
    "dashboard": "auth", "discover": "auth", "watchlist": "auth",
    "settings": "auth", "signal_track": "auth", "signals": "auth",
    "stock_detail": "auth", "performance": "auth",
    # premium
    # Retired: both now route to the Brief, which is auth-level. Left in the map (rather
    # than deleted) so a stale bookmark still hits the auth gate instead of falling through
    # to can_access()'s "unlisted means public" default.
    "screener": "auth", "bi_dashboard": "auth",
    # admin
    "admin": "admin",
}

def can_access(page) -> bool:
    """True if the current user may view `page`. Public pages always pass."""
    need = PAGE_ACCESS.get(page)
    if need is None:
        return True  # public page (landing, login, pricing, etc.)
    if not is_authed():
        return False
    if need == "auth":
        return True
    if need == "premium":
        return is_premium()
    if need == "admin":
        return is_admin()
    return True

def nav(p):
    """Navigate to a page and update URL params so browser back/forward works."""
    # Leaving a page ALSO exits any in-progress checkout — the checkout screens
    # (_redirect_url / _stripe_embed) hard-override the page render, so without this a
    # topbar/nav click gets swallowed and the user is trapped on the checkout screen.
    for _k in ("_redirect_url", "_stripe_embed"):
        st.session_state.pop(_k, None)
    cur = st.session_state.get("page")
    if cur and cur != p:
        hist = st.session_state.get("_page_hist", [])
        if not hist or hist[-1] != cur:
            hist.append(cur)
        if len(hist) > 20: hist = hist[-20:]
        st.session_state["_page_hist"] = hist
    st.session_state.prev_page = cur
    st.session_state.page = p
    # ── Update URL so browser back/forward works natively ──
    try:
        st.query_params["page"] = p
    except Exception:
        pass
    st.rerun()

def _scroll_to_top_on_change():
    """Reset scroll to the top when the page OR the Discover category changes, so a click doesn't
    leave you stranded mid-list on the previous scroll position. Fires ONLY on a real view change
    (guarded by _view_key) — not every rerun, which would make scrolling impossible."""
    try:
        key = f"{st.session_state.get('page','')}|{st.session_state.get('discover_cat','')}"
        if st.session_state.get("_view_key") == key:
            return
        st.session_state["_view_key"] = key
        # KEPT on components.html (deprecated) deliberately: the script must reach
        # window.parent to scroll the app page, which needs the srcdoc iframe's
        # same-origin. st.iframe (data: URL) is opaque-origin and can't do this.
        # See the pinned streamlit ceiling in requirements.txt.
        components.html(
            "<script>try{var w=window.parent||window;var d=w.document;"
            "var el=d.querySelector('section.main')||d.querySelector('[data-testid=\"stMain\"]')"
            "||d.querySelector('[data-testid=\"stAppViewContainer\"]');"
            "if(el){if(el.scrollTo)el.scrollTo(0,0);else el.scrollTop=0;}"
            "if(w.scrollTo)w.scrollTo(0,0);}catch(e){}</script>",
            height=0,
        )
    except Exception:
        pass

def go_back():
    """Back button helper — navigates via in-app history."""
    hist = st.session_state.get("_page_hist", [])
    if hist:
        prev = hist.pop()
        st.session_state["_page_hist"] = hist
        st.session_state.page = prev
        try: st.query_params["page"] = prev
        except: pass
        st.rerun()
    else:
        nav("discover" if is_authed() else "landing")

def back_button(key="page_back"):
    """No-op: in-page Back button removed per design. Users navigate via logo (home) or browser back."""
    pass

def _restore_session():
    """Rehydrate the logged-in user across hard refreshes / direct URLs / new tabs."""
    cm = _cookie_manager()

    # Case 1: already authed in this session — mirror token to cookie + URL.
    if st.session_state.get("user"):
        tok = st.session_state.get("_sid", "")
        if tok:
            try:
                if st.query_params.get("sid", "") != tok:
                    st.query_params["sid"] = tok
            except Exception:
                pass
            # Write the cookie ONCE per token — NOT on every rerun. The cookie
            # manager is a frontend (iframe) component: cm.set() round-trips to
            # the browser and triggers a rerun, and cm.get() reads back async, so
            # the old "if cm.get()!=tok: cm.set()" guard never stabilised and
            # fired set() every run -> infinite rerun loop. Each rerun marked the
            # prior frame data-stale (hidden by our CSS), so the page blanked and
            # repainted continuously (the navbar "falling in / popping out").
            # Writing only when the last-persisted token differs from the current
            # one means exactly one write after login/rotation, then silence. We
            # never call cm.get() on the hot path.
            if cm is not None and st.session_state.get("_cookie_synced_tok") != tok:
                try:
                    from datetime import datetime as _dtt, timedelta as _td
                    cm.set(MSP_COOKIE, tok,
                           expires_at=_dtt.now() + _td(seconds=SESSION_TTL_SECONDS),
                           key="msp_cookie_set")
                    st.session_state["_cookie_synced_tok"] = tok
                except Exception:
                    pass
        return

    # Case 2: token from URL or COOKIE — validate against the server-side store.
    sid = ""
    try:
        sid = st.query_params.get("sid", "")
    except Exception:
        sid = ""
    if not sid and cm is not None:
        try:
            sid = cm.get(MSP_COOKIE) or ""
        except Exception:
            sid = ""
    if sid:
        sess = lookup_session(sid)
        if sess:
            email = sess["email"]
            db = _get_global_db()
            if email in db:
                st.session_state.user = {"email": email, "name": db[email]["name"]}
                st.session_state.role = sess.get("role") or db[email].get("role", "free")
                st.session_state["_sid"] = sid
                st.session_state["_cookie_synced_tok"] = sid  # already on the client; don't re-write
                st.session_state.alerts = _read_json(ALERTS_DB_PATH, {}).get(email, [])
                st.session_state.watchlist = db[email].get("watchlist", [])
                try:
                    if st.query_params.get("sid", "") != sid:
                        st.query_params["sid"] = sid
                except Exception:
                    pass
                return
        if cm is not None:
            try: cm.delete(MSP_COOKIE, key="msp_cookie_del_stale")
            except Exception: pass
        try: st.query_params.pop("sid", None)
        except Exception: pass
        return

    # Case 3: not authed, no token anywhere.
    if st.session_state.pop("_clear_sid_ls", False):
        if cm is not None:
            try: cm.delete(MSP_COOKIE, key="msp_cookie_del_logout")
            except Exception: pass
    return

def _restore_page_from_url():
    """On initial page load, check URL for ?page= and restore navigation state.

    Also routes special deep-links that don't carry an explicit ?page= value:
    - password reset links built as `/?reset_token=...&email=...` must open the
      forgot_pw page so the "set a new password" form renders. Previously these
      links dropped the user on the landing page (the reset form never showed),
      which is the password-reset bug. We detect the reset params here and force
      the forgot_pw page regardless of whether ?page= is present.
    """
    try:
        params = st.query_params.to_dict() if hasattr(st.query_params, 'to_dict') else dict(st.query_params)
    except Exception:
        return

    valid = {"landing","features","login","signup","verify_email","forgot_pw","pricing",
             "contact","dashboard","discover","watchlist","screener","bi_dashboard",
             "stock_detail","settings","admin","signal_track","terms","privacy",
             "performance","signals"}

    # ── Password reset deep-link: route to forgot_pw so the reset form shows ──
    # The reset email link is `/?reset_token=<tok>&email=<addr>` with no &page=,
    # so without this the app would fall through to the default landing page.
    if params.get("reset_token") and params.get("email"):
        if st.session_state.get("page") != "forgot_pw":
            st.session_state.page = "forgot_pw"
        return

    url_page = params.get("page", "")
    if url_page:
        if url_page in valid and st.session_state.get("page") != url_page:
            st.session_state.page = url_page

# Restore session (auth) first, then page, on every run
_restore_session()
# Restore page from URL on every run — supports browser back/forward
_restore_page_from_url()
# ─────────────────────────────────────────────────────────────
# EXCEL EXPORT
# ─────────────────────────────────────────────────────────────
def make_excel(rows: list, sheet_name: str = "Scanviction Data") -> bytes:
    """Build an Excel workbook from a list of dicts. Returns bytes."""
    import io
    try:
        import xlsxwriter
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet(sheet_name[:31])

        # Formats
        hdr_fmt = wb.add_format({"bold": True, "bg_color": "#0d1525", "font_color": "#818cf8",
                                  "border": 1, "border_color": "#1e3a5f", "font_size": 11})
        pos_fmt = wb.add_format({"font_color": "#22c55e", "font_size": 10})
        neg_fmt = wb.add_format({"font_color": "#ef4444", "font_size": 10})
        num_fmt = wb.add_format({"num_format": "$#,##0.00", "font_size": 10})
        pct_fmt = wb.add_format({"num_format": "0.00%", "font_size": 10})
        def_fmt = wb.add_format({"font_size": 10})
        gold_fmt = wb.add_format({"font_color": "#f59e0b", "font_size": 10, "bold": True})

        if not rows:
            wb.close()
            return buf.getvalue()

        headers = list(rows[0].keys())
        col_widths = {h: max(len(str(h)), 8) for h in headers}

        # Write headers
        for ci, h in enumerate(headers):
            ws.write(0, ci, h, hdr_fmt)

        # Write data rows
        for ri, row in enumerate(rows, 1):
            for ci, h in enumerate(headers):
                val = row.get(h, "")
                w = max(col_widths[h], len(str(val)))
                col_widths[h] = min(w, 40)
                # Choose format based on content
                if isinstance(val, (int, float)):
                    fmt = num_fmt if h.lower() in ("price","open","high","low","close") else def_fmt
                    ws.write_number(ri, ci, val, fmt)
                elif isinstance(val, str) and val.startswith("+"):
                    ws.write(ri, ci, val, pos_fmt)
                elif isinstance(val, str) and val.startswith("-"):
                    ws.write(ri, ci, val, neg_fmt)
                elif isinstance(val, str) and ("BUY" in val or "STRONG" in val):
                    ws.write(ri, ci, val, gold_fmt)
                else:
                    ws.write(ri, ci, val, def_fmt)

        # Set column widths
        for ci, h in enumerate(headers):
            ws.set_column(ci, ci, col_widths[h] + 2)

        # Add auto-filter
        ws.autofilter(0, 0, len(rows), len(headers) - 1)
        ws.freeze_panes(1, 0)

        wb.close()
        return buf.getvalue()

    except Exception as e:
        # Fallback to CSV bytes if xlsxwriter fails
        import io
        buf = io.StringIO()
        if rows:
            import csv
            w = csv.DictWriter(buf, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        return buf.getvalue().encode()

def export_button(rows: list, filename: str, label: str = "📥 Export to Excel", key: str = "export"):
    """Render a download button for Excel export."""
    if not rows:
        st.info("No data to export.")
        return
    try:
        data = make_excel(rows, filename.replace(".xlsx","")[:31])
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = ".xlsx"
    except:
        import io
        buf = io.StringIO()
        import csv
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
        data = buf.getvalue().encode(); mime = "text/csv"; ext = ".csv"
    st.download_button(label=label, data=data,
                       file_name=filename.replace(".xlsx", ext),
                       mime=mime, key=key, use_container_width=True)



# ─────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────
# ── Raw (uncached) fetchers ──
# These have NO Streamlit dependency, so the background refresh thread can call
# them safely (st.cache_data requires a ScriptRunContext that threads lack).
# The @st.cache_data wrappers below delegate to these for the request path.
# ── Data-source telemetry ──
# Records the outcome of each external fetch so the app can SHOW whether live
# data is actually flowing (see the Data Health panel in Settings/Admin). This
# is how you confirm, on your deployment, that yfinance/StockTwits are reachable
# — instead of silently falling back to stale/placeholder values.
# source -> {"ok":int,"fail":int,"last_ok":ts,"last_err":str,"last_ms":int}
# MUST be a cache_resource singleton, not a plain module global: Streamlit re-executes
# this script in a fresh namespace on every rerun, so a plain dict was reset each run
# and the worker thread's health records were invisible to the UI — the System panel
# permanently showed "No data fetches have run yet this session" while the scanner
# was in fact running.
@st.cache_resource(show_spinner=False)
def _data_health_state():
    return {}
_DATA_HEALTH = _data_health_state()

def _record_health(source, ok, ms=0, err=""):
    h = _DATA_HEALTH.setdefault(source, {"ok":0,"fail":0,"last_ok":0,"last_err":"","last_ms":0})
    if ok:
        h["ok"] += 1; h["last_ok"] = time.time(); h["last_ms"] = ms
    else:
        h["fail"] += 1; h["last_err"] = str(err)[:160]

def data_health_snapshot():
    """Return a copy of the live-data health table for display."""
    return {k: dict(v) for k, v in _DATA_HEALTH.items()}

def _poly_key():
    """The configured Polygon key (secrets first, then env). '' if unset."""
    try: k = st.secrets.get("POLYGON_API_KEY", "") or ""
    except Exception: k = ""
    return k or _os.environ.get("POLYGON_API_KEY", "") or ""

def _poly_bars(ticker, days=90):
    """Polygon daily OHLCV bars for ANY US ticker (stock or ETF), oldest→newest. [] on failure.
    This is the per-ticker data path — covers names outside the warm universe so we never
    need to scrape Yahoo."""
    k = _poly_key()
    if not k: return []
    try:
        import polygon_adapter as _pa
        return _pa.daily_bars(k, ticker, days=days)
    except Exception:
        return []

def _poly_quote_one(ticker):
    """Fresh-ish single-ticker Polygon snapshot (delayed on non-realtime tiers). {} on failure."""
    k = _poly_key()
    if not k: return {}
    try:
        import polygon_adapter as _pa
        return _pa.snapshot_one(k, ticker) or {}
    except Exception:
        return {}

def _poly_reference(ticker):
    """Per-ticker Polygon reference (name, mktcap, sector-ish, desc). {} on failure."""
    k = _poly_key()
    if not k: return {}
    try:
        import polygon_adapter as _pa
        return _pa.ticker_reference(k, ticker) or {}
    except Exception:
        return {}

def _raw_quote(ticker):
    # Use Twelve Data FIRST when a key is configured — it uses YOUR API budget
    # so it's not subject to rate limiting on a shared IP. Then fall back to POLYGON
    # (per-ticker snapshot → daily bars) — the same licensed feed the app runs on, which
    # covers any US-listed name. (Yahoo/yfinance was removed: ToS gray area for a paid app.)
    t0 = time.time()
    # ── Twelve Data path (preferred when key present AND budget remains) ──
    try:
        _td_key = ""
        try: _td_key = st.secrets.get("TWELVE_DATA_API_KEY", "") or ""
        except Exception: _td_key = ""
        if not _td_key:
            _td_key = _os.environ.get("TWELVE_DATA_API_KEY", "") or ""
        # Skip TD entirely if we've used our daily budget — saves the key from
        # getting locked AND avoids the lag of HTTP failures piling up.
        if _td_key and _td_usage_check_and_increment(1):
            r = requests.get(f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={_td_key}",
                             timeout=6, headers={"User-Agent": "Scanviction/1.0"})
            _td_sync_from_headers(r.headers)
            if r.status_code == 200:
                d = r.json()
                if "close" in d:
                    p = float(d["close"]); pv = float(d.get("previous_close", p) or p)
                    pct = float(d.get("percent_change", 0) or 0)
                    vol = int(float(d.get("volume", 0) or 0))
                    _record_health("twelvedata", True, int((time.time()-t0)*1000))
                    return {"price": round(p, 2), "prev": round(pv, 2), "pct": round(pct, 2),
                            "chg": round(p - pv, 2),   # was missing here (present in the yfinance branch)
                            "name": d.get("name", ticker),
                            "open": float(d.get("open", p) or p), "high": float(d.get("high", p) or p),
                            "low": float(d.get("low", p) or p), "volume": vol}
    except Exception as e:
        _record_health("twelvedata", False, err=e)
    # ── Polygon fallback (per-ticker; covers any US-listed name) ──
    try:
        s = _poly_quote_one(ticker)                       # fresh-ish snapshot
        if not s or s.get("price") is None:               # else EOD from daily bars
            bars = _poly_bars(ticker, days=6)
            if bars:
                last = bars[-1]; prev = bars[-2] if len(bars) >= 2 else last
                s = {"price": last["c"], "prev": prev["c"], "open": last["o"],
                     "high": last["h"], "low": last["l"], "volume": last["v"]}
        if s and s.get("price") is not None:
            p = round(float(s["price"]), 2)
            pv = round(float(s.get("prev") or p), 2)
            out = {"price": p, "prev": pv, "open": round(float(s.get("open") or p), 2),
                   "high": round(float(s.get("high") or p), 2), "low": round(float(s.get("low") or p), 2),
                   "volume": int(s.get("volume") or 0),
                   "pct": round(((p - pv) / pv) * 100, 2) if pv else 0,
                   "chg": round(p - pv, 2), "name": ticker}
            _record_health("polygon", True, int((time.time() - t0) * 1000))
            return out
        _record_health("polygon", False, err=f"no data {ticker}")
        return None
    except Exception as e:
        _record_health("polygon", False, err=e)
        return None

def _raw_ohlcv(ticker,n=60):
    t0 = time.time()
    try:
        bars = _poly_bars(ticker, days=n + 20)
        if len(bars) < 5:
            _record_health("polygon", False, err=f"thin ohlcv {ticker}")
            return None
        rows = bars[-n:]
        out = pd.DataFrame([{"datetime": b["date"], "open": b["o"], "high": b["h"],
                             "low": b["l"], "close": b["c"], "volume": b["v"]} for b in rows])
        out["datetime"] = pd.to_datetime(out["datetime"])
        _record_health("polygon", True, int((time.time()-t0)*1000))
        return out
    except Exception as e:
        _record_health("polygon", False, err=e)
        return None

def _raw_fund(ticker):
    # Polygon reference (name / market cap / sector-ish / description) + daily bars for the
    # 52-week range and average volume. Short-float, days-to-cover, P/E and beta aren't in
    # Polygon's reference feed — the warm universe already carries short-interest + EPS for
    # scored names, and the detail page merges that in; for unscored names these stay blank.
    t0 = time.time()
    try:
        ref = _poly_reference(ticker)
        bars = _poly_bars(ticker, days=365)
        hi52 = round(max((b["h"] for b in bars), default=0), 2)
        lo52 = round(min((b["l"] for b in bars if b["l"] > 0), default=0), 2)
        avgvol = int(sum(b["v"] for b in bars) / len(bars)) if bars else 0
        sic = ref.get("sic") or "N/A"
        out = {"mktcap": ref.get("mktcap") or 0, "sf": 0, "dtc": 0, "avgvol": avgvol,
               "sector": sic, "industry": sic, "pe": None, "hi52": hi52, "lo52": lo52,
               "beta": None, "name": ref.get("name") or ticker,
               "desc": (ref.get("desc") or "")}
        _record_health("polygon_fund", bool(ref or bars), int((time.time()-t0)*1000))
        return out
    except Exception as e:
        _record_health("polygon_fund", False, err=e)
        return {}

def _stocktwits_token():
    """StockTwits OAuth access token. StockTwits moved their public v2 API behind
    OAuth — unauthenticated requests now return HTTP 403 (a Cloudflare block page,
    not JSON), which is why sentiment silently degraded to a flat 50/50 for every
    ticker. Read the token from Streamlit secrets first, then env. Returns '' if
    unset (callers then skip the doomed call and return neutral sentiment fast)."""
    tok = ""
    try: tok = st.secrets.get("STOCKTWITS_ACCESS_TOKEN", "") or ""
    except Exception: tok = ""
    if not tok:
        tok = _os.environ.get("STOCKTWITS_ACCESS_TOKEN", "") or ""
    return tok

# Circuit-breaker: when Finnhub's premium social endpoint returns 401/403 (plan
# doesn't include it), stop calling it for a cooldown so we don't waste a request
# per ticker on every warm. Clears on restart or after the cooldown — so it
# auto-recovers once you upgrade the plan.
_FH_STATE = {"social_locked_until": 0.0}

def _finnhub_key():
    """Finnhub API key (social sentiment source). Secrets first, then env."""
    key = ""
    try: key = st.secrets.get("FINNHUB_API_KEY", "") or ""
    except Exception: key = ""
    if not key:
        key = _os.environ.get("FINNHUB_API_KEY", "") or ""
    return key

def _finnhub_sent(ticker):
    """Social sentiment via Finnhub /stock/social-sentiment (Reddit + X mention
    counts). Returns the app's sentiment contract or None on failure (caller then
    tries the next source). Contract fields:
      bull / bear  – positive vs negative mention share (%)
      msgs         – total mentions in the window (volume → confidence weight)
      buzz_trend   – % change in mention volume, recent half vs older half
    NOTE: this endpoint is premium-gated on some Finnhub plans (returns HTTP 403).
    If your key 403s here, the plan doesn't include social sentiment — switch source."""
    key = _finnhub_key()
    if not key:
        return None
    if time.time() < _FH_STATE["social_locked_until"]:
        return None  # known premium-locked → skip until cooldown expires
    t0 = time.time()
    try:
        _to = datetime.now(); _from = _to - timedelta(days=30)
        r = requests.get("https://finnhub.io/api/v1/stock/social-sentiment",
                         params={"symbol": ticker, "token": key,
                                 "from": _from.strftime("%Y-%m-%d"),
                                 "to": _to.strftime("%Y-%m-%d")},
                         timeout=8, headers={"User-Agent": "Scanviction/1.0"})
        if r.status_code != 200:
            if r.status_code in (401, 403):  # plan doesn't include it — back off 6h
                _FH_STATE["social_locked_until"] = time.time() + 21600
            _record_health("finnhub_sent", False, err=f"HTTP {r.status_code}")
            return None
        d = r.json()
        # Current API shape: {"symbol","data":[...]}. Older shape: reddit/twitter arrays.
        data = d.get("data")
        if not data:
            data = (d.get("reddit") or []) + (d.get("twitter") or [])
        try: data = sorted(data, key=lambda x: x.get("atTime", ""))  # chronological
        except Exception: pass
        pos  = sum(int(x.get("positiveMention", 0) or 0) for x in data)
        neg  = sum(int(x.get("negativeMention", 0) or 0) for x in data)
        msgs = sum(int(x.get("mention", 0) or 0) for x in data)
        tot  = pos + neg
        half = len(data) // 2     # mention-volume trend: recent half vs older half
        recent = sum(int(x.get("mention", 0) or 0) for x in data[half:])
        older  = sum(int(x.get("mention", 0) or 0) for x in data[:half])
        buzz_trend = round(((recent - older) / older) * 100) if older else 0
        _record_health("finnhub_sent", True, int((time.time() - t0) * 1000))
        return {"bull": round((pos / tot) * 100) if tot else 50,
                "bear": round((neg / tot) * 100) if tot else 50,
                "msgs": msgs, "wl": 0, "buzz_trend": buzz_trend, "src": "finnhub"}
    except Exception as e:
        _record_health("finnhub_sent", False, err=e)
        return None

def _stocktwits_sent(ticker):
    """Legacy StockTwits sentiment — only runs when STOCKTWITS_ACCESS_TOKEN is set
    (their public API is Cloudflare-blocked). Returns the contract or None."""
    tok = _stocktwits_token()
    if not tok:
        return None
    t0 = time.time()
    try:
        r=requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
                       params={"access_token":tok},
                       timeout=8, headers={"User-Agent":"Mozilla/5.0"})
        d=r.json()
        msgs=d.get("messages",[])
        bull=sum(1 for m in msgs if m.get("entities",{}).get("sentiment",{}) and m["entities"]["sentiment"].get("basic")=="Bullish")
        bear=sum(1 for m in msgs if m.get("entities",{}).get("sentiment",{}) and m["entities"]["sentiment"].get("basic")=="Bearish")
        tot=bull+bear
        _record_health("stocktwits", True, int((time.time()-t0)*1000))
        return {"bull":round((bull/tot)*100) if tot else 50,"bear":round((bear/tot)*100) if tot else 50,
                "msgs":len(msgs),"wl":d.get("symbol",{}).get("watchlist_count",0),"src":"stocktwits"}
    except Exception as e:
        _record_health("stocktwits", False, err=e)
        return None

# ── Free social-buzz source: ApeWisdom (Reddit + StockTwits mention aggregator) ──
# Free, keyless; tracks ~900 trending tickers with mention counts + 24h-ago counts.
# Gives us social VOLUME and a buzz trend (but no bull/bear direction — that comes
# from the news scorer below). One bulk fetch (a few pages) cached hourly, then
# per-ticker lookups are instant — no per-ticker network call.
APEWISDOM_TTL       = int(_os.environ.get("APEWISDOM_TTL", "3600"))
APEWISDOM_MAX_PAGES = int(_os.environ.get("APEWISDOM_MAX_PAGES", "10"))
_APEWISDOM = {"map": {}, "built_at": 0.0, "lock": threading.Lock()}

def _apewisdom_map():
    """{TICKER: {'mentions','mentions_24h_ago','upvotes'}} from ApeWisdom, cached
    hourly. Tickers absent from the result simply aren't trending (zero buzz)."""
    now = time.time()
    with _APEWISDOM["lock"]:
        if _APEWISDOM["map"] and (now - _APEWISDOM["built_at"]) < APEWISDOM_TTL:
            return dict(_APEWISDOM["map"])
    out = {}; t0 = time.time()
    try:
        for pg in range(1, APEWISDOM_MAX_PAGES + 1):
            r = requests.get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{pg}",
                             timeout=10, headers={"User-Agent": "Scanviction/1.0"})
            if r.status_code != 200:
                break
            d = r.json()
            for x in d.get("results", []):
                t = (x.get("ticker") or "").upper()
                if t:
                    out[t] = {"mentions": int(x.get("mentions", 0) or 0),
                              "mentions_24h_ago": int(x.get("mentions_24h_ago", 0) or 0),
                              "upvotes": int(x.get("upvotes", 0) or 0)}
            if pg >= int(d.get("pages", 1) or 1):
                break
        if out:
            with _APEWISDOM["lock"]:
                _APEWISDOM["map"] = out; _APEWISDOM["built_at"] = now
            _record_health("apewisdom", True, int((time.time() - t0) * 1000))
    except Exception as e:
        _record_health("apewisdom", False, err=e)
    with _APEWISDOM["lock"]:
        return dict(_APEWISDOM["map"])

# VADER: lightweight rule-based sentiment scorer for news headlines. Optional —
# if the package is missing, news direction is simply unavailable (neutral).
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _SIA
    _VADER = _SIA()
except Exception:
    _VADER = None

NEWS_LOOKBACK_DAYS = int(_os.environ.get("NEWS_LOOKBACK_DAYS", "7"))

def _news_direction(ticker):
    """Bull/bear direction from recent Finnhub company-news (free) headlines scored
    with VADER. Returns (bull_pct, bear_pct, article_count) or None if unavailable."""
    key = _finnhub_key()
    if not key or _VADER is None:
        return None
    t0 = time.time()
    try:
        _to = datetime.now(); _from = _to - timedelta(days=NEWS_LOOKBACK_DAYS)
        r = requests.get("https://finnhub.io/api/v1/company-news",
                         params={"symbol": ticker, "token": key,
                                 "from": _from.strftime("%Y-%m-%d"), "to": _to.strftime("%Y-%m-%d")},
                         timeout=8, headers={"User-Agent": "Scanviction/1.0"})
        if r.status_code != 200:
            _record_health("finnhub_news", False, err=f"HTTP {r.status_code}")
            return None
        arts = r.json()
        if not isinstance(arts, list) or not arts:
            _record_health("finnhub_news", True, int((time.time() - t0) * 1000))
            return None
        pos = neg = 0
        for a in arts[:50]:
            text = f"{a.get('headline','')} {a.get('summary','')}".strip()
            if not text:
                continue
            c = _VADER.polarity_scores(text)["compound"]
            if c >= 0.05: pos += 1
            elif c <= -0.05: neg += 1
        tot = pos + neg
        _record_health("finnhub_news", True, int((time.time() - t0) * 1000))
        if not tot:
            return (50, 50, len(arts))
        return (round(pos / tot * 100), round(neg / tot * 100), len(arts))
    except Exception as e:
        _record_health("finnhub_news", False, err=e)
        return None

def _yf_news_direction(ticker):
    """Keyless bull/bear DIRECTION from recent Yahoo Finance news headlines, scored
    with VADER. Free replacement for StockTwits direction (their public API is now
    Cloudflare-blocked) — no API key, no signup. Returns
    (bull_pct, bear_pct, headline_count) or None when unavailable.

    Hits Yahoo's search endpoint DIRECTLY with a hard timeout, rather than going
    through yfinance's Ticker.news (which makes extra auth/crumb round-trips and
    can hang with no timeout). That matters: this runs inside the universe-scan
    thread pool, and a single hung call would block a worker and stall the whole
    'Preparing live market data…' warm-up. The bounded timeout is load-bearing."""
    if _VADER is None:
        return None
    t0 = time.time()
    try:
        r = requests.get("https://query1.finance.yahoo.com/v1/finance/search",
                         params={"q": ticker, "newsCount": 10, "quotesCount": 0},
                         timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            _record_health("yahoo_news", False, err=f"HTTP {r.status_code}")
            return None
        news = r.json().get("news", []) or []
        texts = [(n.get("title") or "").strip() for n in news if n.get("title")]
        pos = neg = 0
        for t in texts:
            score = _VADER.polarity_scores(t)["compound"]
            if score >= 0.05: pos += 1
            elif score <= -0.05: neg += 1
        tot = pos + neg
        _record_health("yahoo_news", True, int((time.time() - t0) * 1000))
        if not texts:
            return None
        if not tot:                       # headlines exist but all neutral
            return (50, 50, len(texts))
        return (round(pos / tot * 100), round(neg / tot * 100), len(texts))
    except Exception as e:
        _record_health("yahoo_news", False, err=e)
        return None

def _hybrid_sent(ticker):
    """Free, keyless social sentiment reconstructed from two free sources:
       • ApeWisdom          → social buzz VOLUME (msgs) + 24h buzz trend
       • news + VADER       → bull/bear DIRECTION (Finnhub if a key is set,
                              otherwise keyless Yahoo Finance news headlines)
    Returns the {bull,bear,msgs,wl,buzz_trend,...} contract, or None if neither
    source produced anything (caller falls through to neutral)."""
    a = _apewisdom_map().get(ticker.upper())
    mentions = a["mentions"] if a else 0
    # Direction (bull/bear) costs ONE per-ticker news fetch. We only spend it on
    # tickers with real social volume: below SENT_MIN_MSGS the split is treated as
    # noise and contributes neutrally anyway (see compute_scores). Gating here is
    # what keeps the universe scan from firing a news request for every one of the
    # hundreds of tickers it scores — the unbounded fan-out that stalled the
    # Discover warm-up. Untrending names simply stay neutral (no buzz = no read).
    nd = None
    if mentions >= SENT_MIN_MSGS:
        # Finnhub news when a key is set, else keyless Yahoo Finance news.
        nd = _news_direction(ticker) or _yf_news_direction(ticker)
    if a is None and nd is None:
        return None
    if a:
        prev = a.get("mentions_24h_ago", 0) or 0
        buzz_trend = round(((mentions - prev) / prev) * 100) if prev else 0
        upvotes = a.get("upvotes", 0)
    else:
        buzz_trend = 0; upvotes = 0
    if nd:
        bull, bear, narts = nd
        msgs = mentions or narts        # prefer social mentions; else article count
    else:
        bull = bear = 50; msgs = mentions
    return {"bull": bull, "bear": bear, "msgs": msgs, "wl": 0,
            "buzz_trend": buzz_trend, "upvotes": upvotes, "src": "hybrid"}

def _raw_sent(ticker):
    """Social sentiment with graceful source fallback:
       Finnhub social (premium; auto-activates on upgrade)
         → free hybrid (ApeWisdom buzz + news/VADER direction)
         → StockTwits (only if token set)
         → neutral 50/50.
    Always returns the {bull,bear,msgs,wl,...} contract; never raises."""
    for fetch in (_finnhub_sent, _hybrid_sent, _stocktwits_sent):
        try:
            r = fetch(ticker)
            if r is not None:
                return r
        except Exception:
            continue
    return {"bull":50,"bear":50,"msgs":0,"wl":0,"src":"none"}

def _raw_hot():
    t0 = time.time()
    tok = _stocktwits_token()
    if not tok:
        return ["NVDA","TSLA","AAPL","AMD","MSTR","PLTR","META","MSFT","GME","AMC"]
    try:
        r=requests.get("https://api.stocktwits.com/api/2/trending/symbols.json",
                       params={"access_token":tok}, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
        d=r.json()
        syms=[s["symbol"] for s in d.get("symbols",[])]
        if syms:
            _record_health("stocktwits", True, int((time.time()-t0)*1000))
            return syms
        raise ValueError("empty trending")
    except Exception as e:
        _record_health("stocktwits", False, err=e)
        return ["NVDA","TSLA","AAPL","AMD","MSTR","PLTR","META","MSFT","GME","AMC"]

@st.cache_data(ttl=300,show_spinner=False)
def yf_quote(ticker):
    return _raw_quote(ticker)

@st.cache_data(ttl=600,show_spinner=False)
def yf_ohlcv(ticker,n=60):
    return _raw_ohlcv(ticker,n)

@st.cache_data(ttl=3600,show_spinner=False)
def yf_fund(ticker):
    return _raw_fund(ticker)

@st.cache_data(ttl=900,show_spinner=False)
def yf_fast_fund(ticker):
    """FAST fundamentals: market cap (Polygon reference) + 52-week range (Polygon daily bars).
    Kept name for call-site compatibility; no longer touches Yahoo."""
    try:
        ref = _poly_reference(ticker)
        bars = _poly_bars(ticker, days=365)
        hi52 = round(max((b["h"] for b in bars), default=0), 2)
        lo52 = round(min((b["l"] for b in bars if b["l"] > 0), default=0), 2)
        return {"mktcap": ref.get("mktcap") or 0, "hi52": hi52, "lo52": lo52}
    except Exception:
        return {}

@st.cache_data(ttl=300,show_spinner=False)
def td_quote(ticker,key):
    if not key: return None
    try:
        d=requests.get(f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={key}",timeout=8).json()
        if "close" not in d: return None
        return {"price":float(d["close"]),"open":float(d.get("open",0)),"high":float(d.get("high",0)),
                "low":float(d.get("low",0)),"volume":int(d.get("volume",0)),"prev":float(d.get("previous_close",0)),
                "chg":float(d.get("change",0)),"pct":float(d.get("percent_change",0)),"name":d.get("name",ticker)}
    except: return None

def get_quote(ticker):
    key=get_td_key()
    if key:
        q=td_quote(ticker,key)
        if q: return q
    return yf_quote(ticker)

@st.cache_data(ttl=900,show_spinner=False)
def st_hot():
    return _raw_hot()

@st.cache_data(ttl=900,show_spinner=False)
def st_sent(ticker):
    return _raw_sent(ticker)

@st.cache_data(ttl=300,show_spinner=False)
def get_indexes():
    """Market-overview tiles from Polygon ETF PROXIES. The Polygon Stocks plan has no index
    feed (indices are a separate subscription), so we show the liquid ETF that tracks each
    index — labeled honestly ('S&P 500 · SPY'). EOD close + a 5-day sparkline, one call each.
    (Yahoo/yfinance removed.)"""
    proxies = [("S&P 500 · SPY", "SPY"), ("Nasdaq · QQQ", "QQQ"), ("Dow · DIA", "DIA"),
               ("Russell · IWM", "IWM"), ("Volatility · VIXY", "VIXY")]
    out = {}
    for label, etf in proxies:
        try:
            bars = _poly_bars(etf, days=8)
            closes = [b["c"] for b in bars]
            if len(closes) >= 2:
                p = closes[-1]; pv = closes[-2]
                out[label] = {"price": round(p, 2), "pct": round(((p-pv)/pv)*100, 2) if pv else 0,
                              "hist": [round(float(v), 2) for v in closes[-5:]]}
            else:
                out[label] = {"price": 0, "pct": 0, "hist": []}
        except Exception:
            out[label] = {"price": 0, "pct": 0, "hist": []}
    return out

@st.cache_data(ttl=900,show_spinner=False)
def get_sectors():
    out={}
    for s,e in SECTOR_ETFS.items():
        try:
            bars=_poly_bars(e, days=6)
            if len(bars)>=2: out[s]=round(((bars[-1]["c"]-bars[-2]["c"])/bars[-2]["c"])*100,2)
            else: out[s]=0.0
        except: out[s]=0.0
    return out

@st.cache_data(ttl=600,show_spinner=False)
def get_bi_movers():
    out=[]
    for t in BROAD_UNI[:28]:
        try:
            bars=_poly_bars(t, days=8)
            if len(bars)>=2:
                p=bars[-1]["c"]; pv=bars[-2]["c"]; v=bars[-1]["v"]; av=sum(b["v"] for b in bars)/len(bars)
                out.append({"t":t,"price":round(p,2),"pct":round(((p-pv)/pv)*100,2),"vol":int(v),"vr":round(v/av,1) if av>0 else 1})
        except: continue
    return out

# ─────────────────────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────────────────────
# SENT_MIN_MSGS / SENT_FULL_MSGS and compute_scores() now live in scoring.py
# (shared with the alerts worker) and are imported at the top of this file.

# get_recommendation / get_insights / risk_color moved to advice.py (pure advisory
# logic; colors from theme). app.py keeps the SAME names via this import — the warm
# worker uses get_recommendation, the detail page uses get_insights, UI uses risk_color.
from advice import get_recommendation, get_insights, risk_color

def sc_pill(sc):
    cls="sp-hi" if sc>=65 else "sp-md" if sc>=40 else "sp-lo"
    return f'<span class="sp {cls}">{sc}</span>'

# The rich factor engine + unique category fit-scoring (compute_factors,
# COMPOSITE_FIT, category_for_feat, assign_categories, …) now lives in scoring.py
# and is imported at the top of this file — shared with the alerts worker.

# ─────────────────────────────────────────────────────────────
# DETAIL-PAGE MULTI-FACTOR SCORECARD  (rich detail only on click)
# ─────────────────────────────────────────────────────────────
def _scrow(label, val, score, why):
    """One factor row: label, value, a 0-100 strength bar, plain-English line.
    Built as a single inline string (no standalone {var} lines) so it renders
    cleanly inside st.markdown."""
    score = int(_cl(score, 0, 100))
    color = GREEN if score >= 66 else GOLD if score >= 40 else RED
    return (f'<div style="margin-bottom:11px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;font-size:12px;margin-bottom:3px;">'
            f'<span style="color:#cbd5e1;font-weight:600;">{label}</span>'
            f'<span style="font-family:JetBrains Mono,monospace;color:{color};font-weight:700;">{val}</span></div>'
            f'<div style="background:rgba(255,255,255,0.05);border-radius:3px;height:5px;overflow:hidden;">'
            f'<div style="background:{color};width:{max(3,score)}%;height:5px;"></div></div>'
            f'<div style="font-size:10.5px;color:#4a5e7a;margin-top:3px;line-height:1.35;">{why}</div></div>')

def _sccard(title, rows_html):
    return (f'<div style="background:#0a1018;border:1px solid {BORDER};border-radius:12px;padding:16px 18px;margin-bottom:14px;">'
            f'<div style="font-size:12px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:14px;">{title}</div>'
            f'{rows_html}</div>')

def render_scorecard(t, df, info, sent, sc, bd, factors=None):
    """Full multi-factor recommendation scorecard for the detail page. Combines the
    rich factor engine with the lazily-loaded fundamentals + sentiment. Each factor
    shows its value, a strength bar, and a plain-English 'why'; the header carries
    the overall rating with the top contributing reasons and the key risks.
    `factors` (from compute_factors) is reused if passed, else computed here."""
    f = factors if factors is not None else compute_factors(df)
    info = info or {}; sent = sent or {}
    reasons = []   # (label, score) bullish contributors
    risks = []     # (label, note) risk contributors

    # ── Momentum & Trend ──
    rsi = f["rsi"]
    rsi_score = (90 if 55 <= rsi <= 70 else 70 if 45 <= rsi < 55 else 62 if rsi < 30
                 else 50 if 30 <= rsi < 45 else 45 if 70 < rsi <= 78 else 28)
    rsi_why = ("Oversold — historically precedes a bounce" if rsi < 30 else
               "Healthy momentum, not overextended" if 55 <= rsi <= 70 else
               "Overbought — vulnerable to profit-taking" if rsi > 78 else "Neutral momentum")
    roc_score = _cl((f["roc20"] + 10) * 3, 0, 100)
    trend_score = f["trend_align"] / 3 * 100
    trend_why = ("Above both its 20- & 50-day averages — buyers in control" if f["trend_align"] == 3 else
                 "Above one key moving average" if f["trend_align"] >= 1 else
                 "Below its key moving averages — downtrend")
    macd_score = f["macd_state"] / 3 * 100
    macd_why = ("Fresh bullish crossover — momentum just turned up" if f["macd_state"] == 3 else
                "Positive and rising" if f["macd_state"] == 2 else
                "Improving but still below zero" if f["macd_state"] == 1 else "Bearish")
    adx = f.get("adx", 0.0); adx_lbl = "Strong" if adx >= 25 else "Building" if adx >= 18 else "Choppy/weak"
    mt_rows = (_scrow("RSI (14)", f"{rsi:.0f}", rsi_score, rsi_why) +
               _scrow("20-Day Return", f"{f['roc20']:+.1f}%", roc_score, "Price change over the last month — raw momentum") +
               _scrow("Trend Alignment", f"{f['trend_align']}/3", trend_score, trend_why) +
               _scrow("Trend Strength (ADX)", f"{adx:.0f} · {adx_lbl}", _cl(adx*2.5, 0, 100), "ADX measures how DIRECTIONAL the move is — >25 = a real trend, <18 = chop to avoid") +
               _scrow("MACD", ["Bearish", "Improving", "Positive", "Cross ↑"][f["macd_state"]], macd_score, macd_why))
    for lbl, s in (("Strong momentum", roc_score), ("Healthy RSI", rsi_score),
                   ("Confirmed uptrend", trend_score), ("MACD turning up", macd_score)):
        if s >= 72: reasons.append((lbl, s))

    # ── Volume & Volatility ──
    vr = f["vol_ratio"]; vt = f["vol_trend"]; cmf = f.get("cmf", 0.0); mfi = f.get("mfi", 50.0)
    vr_score = _cl((vr - 0.5) * 55, 0, 100); vt_score = _cl((vt - 0.7) * 70, 0, 100)
    cmf_score = _cl((cmf + 0.15) * 330, 0, 100)   # CMF ~ -0.15..+0.15 → 0..100
    vv_rows = (_scrow("Relative Volume", f"{vr:.1f}×", vr_score, "Today vs its 20-day average — " + ("unusually active" if vr >= 1.5 else "normal activity")) +
               _scrow("Money Flow (CMF)", f"{cmf:+.2f}", cmf_score, "Chaikin money flow — positive = net buying / institutional accumulation; negative = distribution") +
               _scrow("Money Flow Index", f"{mfi:.0f}", mfi, "Volume-weighted RSI — >80 overbought, <20 oversold, the sweet spot is rising 50–70") +
               _scrow("Volatility Squeeze", f"{f['bb_squeeze']*100:.0f}%", f["bb_squeeze"]*100, "Bands compressed — a coiled-spring setup" if f["bb_squeeze"] >= 0.6 else "Normal volatility range") +
               _scrow("Range Position", f"{f['range_pos']*100:.0f}%", f["range_pos"]*100, "Where price sits in its 60-day range (higher = nearer highs)"))
    if vr >= 1.5: reasons.append((f"{vr:.1f}× normal volume", vr_score))
    if cmf > 0.1: reasons.append(("Money flowing in (accumulation)", cmf_score))

    # ── Valuation & Sentiment ──
    pe = info.get("pe"); mc = info.get("mktcap", 0) or 0
    pe_str = f"{pe:.1f}×" if pe else "N/A"
    pe_score = (85 if pe and 0 < pe < 15 else 65 if pe and pe < 25 else 45 if pe and pe < 40 else 40)
    bull = sent.get("bull", 50) or 50; msgs = sent.get("msgs", 0) or 0
    mc_str = fmt_mktcap(mc)
    vs_rows = (_scrow("Valuation (P/E)", pe_str, pe_score, "Reasonable earnings multiple" if pe and pe < 25 else ("Rich / unprofitable — paying for growth" if pe else "Loads when you open the stock")) +
               _scrow("Market Cap", mc_str, _cl((mc/2e10)*100, 20, 100) if mc else 40, "Larger = steadier; smaller = higher risk & reward") +
               _scrow("Bullish Sentiment", f"{bull:.0f}%", bull, "Share of recent chatter leaning bullish" if msgs >= 10 else "Too little chatter yet to read direction") +
               _scrow("Social Volume", f"{int(msgs)}", _cl(msgs*0.5, 0, 100), "Mentions across Reddit / StockTwits trackers"))
    if pe and 0 < pe < 20: reasons.append(("Attractive valuation", pe_score))
    if bull >= 62 and msgs >= 10: reasons.append((f"{bull:.0f}% bullish chatter", bull))

    # ── Catalysts & Insider Activity (SEC EDGAR) ──
    ins_n = int(info.get("insider_buys", 0) or 0); ins_val = float(info.get("insider_value", 0.0) or 0.0)
    if ins_n > 0:
        _vs = fmt_money(ins_val)
        reasons.append((f"{ins_n} insider open-market buy{'s' if ins_n>1 else ''} ({_vs})", 92 if ins_n >= 2 else 74))
    if info.get("has_8k"):
        reasons.append(("Fresh SEC 8-K (material event)", 60))

    # ── Risk ──
    atr = f["atr_pct"]; dd = f["drawdown"]; sf = (info.get("sf", 0) or 0) * 100; beta = info.get("beta")
    if atr >= 4: risks.append(("High volatility", f"~{atr:.1f}% average daily range"))
    if dd <= -25: risks.append(("Deep drawdown", f"{dd:.0f}% below its recent high"))
    if sf >= 15: risks.append(("Crowded short", f"{sf:.0f}% short float — violent two-way moves"))
    if mc and mc < 5e8: risks.append(("Micro-cap liquidity", "Small float can gap and slip"))
    if beta and beta > 1.6: risks.append(("High beta", f"{beta:.1f}× the market's swings"))
    if rsi > 78: risks.append(("Overbought", "Stretched short-term — chase risk"))
    risk_rows = (_scrow("Daily Volatility (ATR)", f"{atr:.1f}%", _cl(100-atr*12, 0, 100), "Lower = calmer day-to-day price action") +
                 _scrow("Drawdown from High", f"{dd:.0f}%", _cl(100+dd*1.6, 0, 100), "How far below its 90-day high it trades") +
                 _scrow("Short Float", f"{sf:.0f}%" if sf else "N/A", _cl(100-sf*3, 0, 100) if sf else 60, "High short interest = squeeze fuel but volatile") +
                 _scrow("Beta", f"{beta:.2f}" if beta else "N/A", _cl(100-((beta or 1)-1)*60, 0, 100), "Sensitivity to overall market moves"))

    # ── Header: overall rating + top reasons + key risks ──
    rec_lbl, rec_clr, rec_txt = get_recommendation(sc, bd, info)
    top_reasons = sorted(set(reasons), key=lambda x: -x[1])[:3]
    rlist = "".join(f'<li style="margin-bottom:3px;">{lbl}</li>' for lbl, _ in top_reasons) or '<li>Mixed signals — no standout bullish factor</li>'
    klist = "".join(f'<li style="margin-bottom:3px;">{lbl} — <span style="color:#64748b;">{note}</span></li>' for lbl, note in risks[:3]) or '<li>No major red flags detected</li>'
    # ── Scanviction Conviction Score (the signature blended metric) ──
    feat_cs = dict(f); feat_cs["sc"] = sc
    feat_cs["dtc"] = info.get("dtc") or 0; feat_cs["pe"] = info.get("pe")
    feat_cs["insider_buys"] = int(info.get("insider_buys", 0) or 0)
    feat_cs["insider_value"] = float(info.get("insider_value", 0.0) or 0.0)
    cscore, cbreak = conviction_score(feat_cs)
    cs_c = GREEN if cscore >= 70 else GOLD if cscore >= 45 else RED
    comp_html = "".join(
        f'<div style="flex:1;min-width:84px;">'
        f'<div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-bottom:2px;"><span>{lbl}</span><span style="font-weight:700;color:#cbd5e1;">{int(sub)}</span></div>'
        f'<div style="background:rgba(255,255,255,0.06);border-radius:2px;height:4px;overflow:hidden;"><div style="background:{GREEN if sub>=66 else GOLD if sub>=40 else RED};width:{max(3,int(sub))}%;height:4px;"></div></div></div>'
        for lbl, sub, _w in cbreak)
    st.markdown('<div class="sec-hd" style="margin-top:14px;">🔬 Conviction Breakdown</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div style="background:linear-gradient(135deg,{rec_clr}14,#0a1018);border:1px solid {rec_clr}44;border-radius:12px;padding:16px 18px;margin-bottom:14px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'
        f'<div><div style="font-size:18px;font-weight:800;color:{rec_clr};">{rec_lbl}</div>'
        f'<div style="font-size:12px;color:#94a3b8;margin-top:2px;font-style:italic;">{rec_txt}</div></div>'
        f'<div style="text-align:right;flex-shrink:0;"><div style="font-family:JetBrains Mono,monospace;font-size:30px;font-weight:800;color:{cs_c};line-height:1;">{cscore}</div>'
        f'<div style="font-size:9px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px;">Conviction</div></div></div>'
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:14px;">{comp_html}</div>'
        f'<div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:14px;">'
        f'<div style="flex:1;min-width:200px;"><div style="font-size:11px;font-weight:700;color:{GREEN};text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;">✓ Top reasons</div><ul style="margin:0;padding-left:16px;font-size:12px;color:#cbd5e1;">{rlist}</ul></div>'
        f'<div style="flex:1;min-width:200px;"><div style="font-size:11px;font-weight:700;color:{RED};text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;">⚠ Key risks</div><ul style="margin:0;padding-left:16px;font-size:12px;color:#cbd5e1;">{klist}</ul></div>'
        f'</div></div>', unsafe_allow_html=True)
    cA, cB = st.columns(2, gap="small")
    with cA:
        st.markdown(_sccard("Momentum & Trend", mt_rows), unsafe_allow_html=True)
        st.markdown(_sccard("Valuation & Sentiment", vs_rows), unsafe_allow_html=True)
    with cB:
        st.markdown(_sccard("Volume & Volatility", vv_rows), unsafe_allow_html=True)
        st.markdown(_sccard("Risk Profile", risk_rows), unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# COMPOSITE SCORING ENGINE
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# PRECOMPUTED SCORED UNIVERSE  (Discover speed)
# ─────────────────────────────────────────────────────────────
# The old get_composite_stocks() fetched + scored ~30 tickers live on EVERY
# category click (no caching of the scored result), so switching categories
# meant a full re-scan — 20-60s on a cold cache, and noticeable lag even warm.
#
# New architecture:
#   score_ticker(t)          → fetch + score ONE ticker, cached 5 min.
#   build_scored_universe()  → score the whole universe ONCE, cached 5 min.
#   get_composite_stocks(c)  → pure in-memory FILTER over the cached universe;
#                              no network, no re-scoring. Category switches are
#                              now milliseconds.
# The heavy work happens once per 5-min window; every click after that — and
# every re-click of a previously viewed category — reuses the cached rows.

@st.cache_data(ttl=300, show_spinner=False)
def score_ticker(t):
    """Fetch + score a single ticker (request-path, cached 5 min). Used by the
    standard-category scan. The composite universe uses the tiered/threaded
    path below."""
    try:
        q = get_quote(t); df = yf_ohlcv(t, 60); info = yf_fund(t); sent = st_sent(t)
        if not q or df is None:
            return None
        sc, bd, op, risk, conf = compute_scores(df, info, sent)
        ig = get_insights(df, info)
        # Quote no longer carries a name (fast_info path); backfill from fund.
        if q and not q.get("name") or q.get("name") == t:
            q = dict(q); q["name"] = (info.get("name") or t)
        return {"t": t, "q": q, "sc": sc, "bd": bd, "ig": ig, "op": op,
                "risk": risk, "conf": conf, "hot": False, "df": df,
                "info": info, "sent": sent}
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# TIERED NEAR-LIVE REFRESH  +  BACKGROUND PRECOMPUTE
# ─────────────────────────────────────────────────────────────
# Refresh tiers (seconds) — different data changes at different speeds, so we
# refetch each at its own cadence instead of re-pulling everything every time:
#   FAST     price / move %        → 90s   (feels near-live)
#   MODERATE social sentiment      → 900s  (15 min)
#   SLOW     fundamentals + OHLCV  → 3600s (1 hr; slow-moving)
# A single background daemon thread (per process) keeps a warm, fully-scored
# universe so the FIRST Discover click is instant too — not just subsequent
# ones. build_scored_universe() returns that warm snapshot; category switching
# remains a pure in-memory filter (see get_composite_stocks).
#
# PRODUCTION NOTE: a per-process thread warms only its own process. On a single
# Streamlit Community Cloud replica that's the whole app, so this is effective.
# For multi-replica / horizontally-scaled deploys, the correct architecture is
# a separate scheduled worker process writing the scored universe + snapshots
# to a shared store (Redis or Postgres) that all replicas read. The functions
# here are structured so that swap is mechanical: replace the in-process
# _UNIVERSE_CACHE with reads/writes against that shared store.
import threading as _threading

# ── Refresh cadences tuned for the Twelve Data FREE tier (800 credits/day) ──
# Each ticker × each endpoint = 1 credit. With 33 curated tickers + 4 endpoints,
# a naive 90-second warm burns ~95k credits/day (we hit this — see git history).
# These TTLs and the worker's market-hour-aware sleep below cap us at <800/day.
FAST_TTL = 1800      # price refresh window: 30 minutes
MOD_TTL  = 3600      # sentiment: 1 hour
SLOW_TTL = 21600     # fundamentals + ohlcv: 6 hours (these rarely change intraday)
UNIVERSE_REFRESH        = int(_os.environ.get("UNIVERSE_REFRESH", "3600"))   # FULL re-score cadence off-hours
UNIVERSE_REFRESH_CLOSED = 14400  # every 4 hours when market is closed
UNIVERSE_REFRESH_WEEKEND= 0      # 0 = skip entirely on weekends (no point — market shut)
# ── Intraday signals (spread-out firing) ──────────────────────────────────────
# When ON, the warm APPENDS today's (delayed) snapshot as a synthetic "today bar" to each
# ticker's daily history before scoring, and re-scores on the faster INTRADAY_RESCORE cadence
# DURING MARKET HOURS. So a stock that breaks out at 11am enters its category at ~11:15 (15-min
# delayed on Starter) instead of every entry batching at the once-a-day EOD re-score. The daily
# history is cached (_POLY_STATE), so an intraday re-score is scoring-only — no history re-fetch.
# NOTE: needs an ALWAYS-ON host to actually run continuously — on hibernating Streamlit Cloud the
# worker only ticks while someone has the app open.
INTRADAY_SIGNALS = _os.environ.get("INTRADAY_SIGNALS", "1").strip().lower() not in ("0", "false", "no", "")
INTRADAY_RESCORE = int(_os.environ.get("INTRADAY_RESCORE", "900"))   # market-hours re-score cadence (s)
# FAST price refresh: re-pull just the Polygon snapshot (one cheap call) and update
# the displayed prices on already-scored rows every few minutes, WITHOUT the
# expensive full re-score. This is what makes Discover feel live intraday. The
# heavy category re-scoring stays on UNIVERSE_REFRESH (daily-bar driven), which
# also keeps categories stable instead of churning on intraday noise.
POLY_PRICE_REFRESH      = int(_os.environ.get("POLY_PRICE_REFRESH", "180"))   # 3 min

# Hard daily credit budget for Twelve Data. The fetcher tracks usage and stops
# calling TD once we hit this — we'd rather degrade to "last known" than burn
# the whole budget and lock the key for the rest of the day (which is exactly
# what happened on 11/22 — see _DATA_HEALTH telemetry).
TWELVE_DATA_DAILY_BUDGET = int(_os.environ.get("TWELVE_DATA_DAILY_BUDGET", "700"))
_TD_USAGE = {"date": "", "credits": 0, "loaded": False}
_TD_USAGE_LOCK = _threading.Lock()
_TD_USAGE_KEY = "td_usage_counter"  # kv_store row that persists across restarts

def _td_usage_load():
    """Load the persistent counter from Postgres. Without this, every Streamlit
    Cloud restart resets the in-memory counter to 0, but Twelve Data's server
    keeps the real count — so the budget gate was useless. This is the fix.
    See 2026-11-22 incident: 111k credits burned in a day with the gate at 700."""
    global _TD_USAGE
    try:
        val, found = _db_read(_TD_USAGE_KEY)
        if found and isinstance(val, dict) and "date" in val and "credits" in val:
            _TD_USAGE["date"] = val.get("date", "")
            _TD_USAGE["credits"] = int(val.get("credits", 0))
    except Exception:
        pass
    _TD_USAGE["loaded"] = True

def _td_usage_save():
    """Persist current counter to Postgres (best-effort; failures don't block)."""
    try:
        _db_write(_TD_USAGE_KEY, {"date": _TD_USAGE["date"], "credits": _TD_USAGE["credits"]})
    except Exception:
        pass

def _td_sync_from_headers(headers):
    """Twelve Data returns api-credits-used / api-credits-left on every response.
    Sync our local counter to THEIR truth so the gate works even when our
    process restarts or another caller (worker, another replica) shares the
    same key. This is the only safe source of truth."""
    try:
        used = headers.get("api-credits-used") if hasattr(headers, "get") else None
        if used is None: return
        used = int(used)
        from datetime import datetime as _dtu
        today = _dtu.utcnow().strftime("%Y-%m-%d")
        with _TD_USAGE_LOCK:
            if not _TD_USAGE.get("loaded"):
                _td_usage_load()
            if _TD_USAGE["date"] != today:
                _TD_USAGE["date"] = today
            # Always take the max of (local, server) — never decrease unless date changed.
            if used > _TD_USAGE["credits"]:
                _TD_USAGE["credits"] = used
                _td_usage_save()
    except Exception:
        pass

def _td_usage_check_and_increment(n=1):
    """Returns True if we have budget for n more credits today; increments if so.
    Counter is loaded from Postgres on first use and persisted on every change,
    so it survives restarts and is shared across replicas. Resets at UTC midnight
    (matches Twelve Data's reset behavior)."""
    from datetime import datetime as _dtu
    today = _dtu.utcnow().strftime("%Y-%m-%d")
    with _TD_USAGE_LOCK:
        if not _TD_USAGE.get("loaded"):
            _td_usage_load()
        if _TD_USAGE["date"] != today:
            _TD_USAGE["date"] = today
            _TD_USAGE["credits"] = 0
            _td_usage_save()
        if _TD_USAGE["credits"] + n > TWELVE_DATA_DAILY_BUDGET:
            return False
        _TD_USAGE["credits"] += n
        # Save every 5 credits to limit DB writes while keeping count accurate.
        if _TD_USAGE["credits"] % 5 == 0 or n >= 5:
            _td_usage_save()
        return True

def td_usage_today():
    """Read-only view of TD credit usage today (for the System panel)."""
    from datetime import datetime as _dtu
    today = _dtu.utcnow().strftime("%Y-%m-%d")
    with _TD_USAGE_LOCK:
        if not _TD_USAGE.get("loaded"):
            _td_usage_load()
        # If the stored date is from a previous day, today's usage is 0.
        used = _TD_USAGE["credits"] if _TD_USAGE["date"] == today else 0
        return {"date": today, "used": used,
                "budget": TWELVE_DATA_DAILY_BUDGET,
                "remaining": max(0, TWELVE_DATA_DAILY_BUDGET - used)}

# PROCESS-WIDE SINGLETONS via @st.cache_resource — NOT plain module globals.
# Streamlit re-executes this whole script in a FRESH namespace on every rerun, so
# a plain `_UNIVERSE_CACHE = {...}` is reset to empty each rerun. The background
# worker thread (started in the first run's namespace) would then be writing its
# warm results into a stale dict that later reruns can never see — leaving the UI
# stuck on "Preparing live market data…" forever even though the scan finished.
# cache_resource returns the SAME object across all reruns/sessions/threads, so
# the worker's writes are visible to every rerun. (Verified: plain globals and a
# `not in globals()` guard both reset across reruns; only cache_resource persists.)
@st.cache_resource(show_spinner=False)
def _shared_runtime_state():
    import threading as __th
    return {
        "data_cache": {},                 # ticker -> {field: (value, fetched_at)}
        "data_lock": __th.Lock(),
        "universe_cache": {"rows": [], "built_at": 0.0, "hot": [], "attempted": False,
                           "scanned": 0, "ok": 0, "market_wide": False, "last_error": "",
                           "force": False},
        "universe_lock": __th.Lock(),
    }
_RT_STATE = _shared_runtime_state()
_DATA_CACHE = _RT_STATE["data_cache"]
_DATA_LOCK = _RT_STATE["data_lock"]
_UNIVERSE_CACHE = _RT_STATE["universe_cache"]
_UNIVERSE_LOCK = _RT_STATE["universe_lock"]
_WORKER_STARTED = False
_WORKER_LOCK = _threading.Lock()

def _tiered_get(ticker, field, ttl, fetch_fn):
    """Return a cached field for a ticker, refetching only if older than ttl.
    Thread-safe; usable from both the worker thread and the request path."""
    now = time.time()
    with _DATA_LOCK:
        slot = _DATA_CACHE.get(ticker, {}).get(field)
    if slot is not None and (now - slot[1]) < ttl:
        return slot[0]
    val = fetch_fn(ticker)
    with _DATA_LOCK:
        _DATA_CACHE.setdefault(ticker, {})[field] = (val, now)
    return val

def _tiered_score_ticker(t, hot):
    """Score one ticker using the tiered cache: fresh price (FAST), moderately
    fresh sentiment (MOD), slow fundamentals/OHLCV (SLOW). No Streamlit calls,
    so the worker thread can run it."""
    try:
        q    = _tiered_get(t, "quote", FAST_TTL, _raw_quote)
        df   = _tiered_get(t, "ohlcv", SLOW_TTL, lambda x: _raw_ohlcv(x, 60))
        info = _tiered_get(t, "fund",  SLOW_TTL, _raw_fund)
        sent = _tiered_get(t, "sent",  MOD_TTL,  _raw_sent)
        if not q or df is None:
            return None
        sc, bd, op, risk, conf = compute_scores(df, info, sent)
        ig = get_insights(df, info)
        if q and (not q.get("name") or q.get("name") == t):
            q = dict(q); q["name"] = (info.get("name") or t)
        return {"t": t, "q": q, "sc": sc, "bd": bd, "ig": ig, "op": op,
                "risk": risk, "conf": conf, "hot": t in hot, "df": df,
                "info": info, "sent": sent}
    except Exception:
        return None

def fetch_realtime_ticker(t):
    """User-on-demand fresh fetch: bypasses the price cache so the user always
    sees a real-time quote when THEY interact with a ticker (clicks a card,
    hits refresh, opens detail). The slow tiers (OHLCV, fundamentals) still
    come from cache since they barely change intraday — this keeps the credit
    cost of an interaction to ~1 TD credit instead of 4.

    Used by buttons that say 'refresh' or when the user opens a stock page.
    """
    try:
        # Invalidate the price cache for this ticker so _raw_quote actually runs
        with _DATA_LOCK:
            if t in _DATA_CACHE:
                _DATA_CACHE[t].pop("quote", None)
        # Now score normally — quote will be a fresh fetch, OHLCV/fund/sent
        # stay cached (no need to refetch the 60-day price history on a click).
        with _UNIVERSE_LOCK:
            hot = list(_UNIVERSE_CACHE.get("hot", []))
        row = _tiered_score_ticker(t, hot)
        # Also push the fresh row into the universe cache so the next page
        # render shows the updated price immediately.
        if row:
            with _UNIVERSE_LOCK:
                rows = list(_UNIVERSE_CACHE.get("rows", []))
                rows = [r for r in rows if r.get("t") != t] + [row]
                _UNIVERSE_CACHE["rows"] = rows
        return row
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# FMP MARKET-WIDE SCREENER  (true whole-market discovery, optional)
# ─────────────────────────────────────────────────────────────
# When an FMP_API_KEY is configured, we use Financial Modeling Prep's
# server-side Stock Screener (25,000+ US equities) to DISCOVER candidate tickers
# market-wide, instead of being limited to the hardcoded ~85-name watchlist.
# FMP returns matching symbols; those symbols then flow through the SAME scoring
# pipeline (_tiered_score_ticker) the app already uses, so nothing downstream
# changes. Without a key, every function here is a no-op and the app falls back
# to the curated universe — so the app always works.
#
# The screener has NO free-text query; it filters by marketCap, price, volume,
# beta, sector, exchange, etc. (server-side). We run a small number of broad
# screens to assemble a market-wide candidate pool, cache it, and refresh it on
# a slow cadence (the pool of liquid names changes slowly; per-ticker live
# prices still refresh on the fast tier via the normal scoring path).
FMP_SCREENER_URL = "https://financialmodelingprep.com/stable/company-screener"
FMP_POOL_TTL = 6 * 3600          # refresh the discovered candidate pool every 6h
# IMPORTANT: FMP_MAX_POOL defaults to 0 on free tiers — every extra ticker FMP
# discovers triggers 3 TD calls (quote+ohlcv+fund) in the warm. With FMP_MAX_POOL=120
# a single warm burns ~450+ TD credits, exhausting the 800/day free budget in
# under two cycles. Set FMP_MAX_POOL via env var when you upgrade TD/yfinance
# usage to a paid tier that can support market-wide scoring.
FMP_MAX_POOL = int(_os.environ.get("FMP_MAX_POOL", "0"))
_FMP_CACHE = {"pool": [], "built_at": 0.0, "by_screen": {}}
_FMP_LOCK = _threading.Lock()

def _fmp_screen(api_key, **params):
    """One raw call to the FMP screener. Returns a list of result dicts (each
    has at least 'symbol'), or [] on any failure. Records telemetry."""
    if not api_key:
        return []
    t0 = time.time()
    q = dict(params); q["apikey"] = api_key
    # Sensible defaults: tradable, reasonable liquidity, US exchanges.
    q.setdefault("isActivelyTrading", "true")
    q.setdefault("limit", 200)
    try:
        r = requests.get(FMP_SCREENER_URL, params=q, timeout=12,
                         headers={"User-Agent": "Scanviction/1.0"})
        if r.status_code != 200:
            _record_health("fmp", False, err=f"HTTP {r.status_code}")
            return []
        data = r.json()
        if not isinstance(data, list):
            _record_health("fmp", False, err="unexpected payload")
            return []
        _record_health("fmp", True, int((time.time() - t0) * 1000))
        return data
    except Exception as e:
        _record_health("fmp", False, err=e)
        return []

# Broad screens used to assemble a market-wide candidate pool. Each is a coarse
# net; the app's own composite filters do the fine-grained selection afterward.
# Param names follow FMP's stable screener (…MoreThan / …LowerThan).
_FMP_DISCOVERY_SCREENS = [
    # Large/mid-cap liquid names
    {"marketCapMoreThan": 2_000_000_000, "volumeMoreThan": 1_000_000, "limit": 200},
    # Active mid-caps with strong volume
    {"marketCapMoreThan": 300_000_000, "marketCapLowerThan": 2_000_000_000,
     "volumeMoreThan": 750_000, "limit": 200},
    # Small/micro-caps with volume (where "hidden movers" live)
    {"marketCapMoreThan": 50_000_000, "marketCapLowerThan": 300_000_000,
     "volumeMoreThan": 500_000, "limit": 200},
]

def _fmp_build_pool(api_key):
    """Assemble a market-wide candidate ticker pool from a few broad screens.
    Deduplicated, capped at FMP_MAX_POOL. US common stock only (filters out
    funds/ETFs when FMP flags them)."""
    seen = []
    seen_set = set()
    for screen in _FMP_DISCOVERY_SCREENS:
        rows = _fmp_screen(api_key, exchange="NASDAQ,NYSE,AMEX", **screen)
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if not sym or sym in seen_set:
                continue
            # Skip obvious non-common-stock if FMP tells us
            if row.get("isEtf") or row.get("isFund"):
                continue
            # Skip symbols with dots/odd chars that yfinance can't price
            if not sym.isalpha():
                continue
            seen_set.add(sym)
            seen.append(sym)
            if len(seen) >= FMP_MAX_POOL:
                return seen
    return seen

def fmp_universe_tickers(api_key):
    """Cached market-wide candidate pool (refreshed every FMP_POOL_TTL). Returns
    [] if no key or all screens failed (caller then falls back to curated set)."""
    if not api_key:
        return []
    now = time.time()
    with _FMP_LOCK:
        if _FMP_CACHE["pool"] and (now - _FMP_CACHE["built_at"]) < FMP_POOL_TTL:
            return list(_FMP_CACHE["pool"])
    pool = _fmp_build_pool(api_key)
    if pool:
        with _FMP_LOCK:
            _FMP_CACHE["pool"] = pool
            _FMP_CACHE["built_at"] = now
    return pool

def fmp_pool_status():
    with _FMP_LOCK:
        return {"count": len(_FMP_CACHE["pool"]), "built_at": _FMP_CACHE["built_at"]}

def _full_universe_tickers():
    """The complete set of tickers to pre-score: the broad universe, every
    standard category's list, and current trending. Scoring this once means
    BOTH composite and standard categories become instant in-memory filters —
    nothing is fetched on a category click."""
    allt = list(BROAD_UNI)
    for lst in CATEGORIES.values():
        allt.extend(lst)
    return allt

# The worker thread can't read st.secrets/session_state, so the main thread
# captures the FMP key into this global on each run (see app body). The worker
# reads it to decide whether to discover tickers market-wide.
_FMP_KEY_CAPTURED = ""
# Same pattern for the Polygon key: captured on the main thread so the universe
# worker (which can't read st.secrets) can build the whole-market scan.
_POLYGON_KEY_CAPTURED = ""
# Telegram bot token captured on the main thread too, so the worker thread can DELIVER
# freshly-recorded signals to subscribers inline (see _deliver_new_signals).
_TG_TOKEN_CAPTURED = ""
# Email (Resend) config captured the same way: _alert_email used to read st.secrets
# directly, which is only reliable on the main thread — from the worker thread a
# failed read silently returned "" and EMAIL ALERTS NEVER SENT, with no error
# anywhere. Captured at router startup (script context) like the tokens above.
_RESEND_KEY_CAPTURED = ""
_EMAIL_FROM_CAPTURED = ""
_APP_URL_CAPTURED = ""

def _effective_universe_tickers():
    """Tickers to pre-score. With an FMP key, this is the market-wide candidate
    pool (hundreds of names discovered across 25k+ US stocks) UNIONED with the
    curated list (so well-known names are always present). Without a key, it's
    just the curated ~85. Falls back to curated if FMP returns nothing."""
    curated = _full_universe_tickers()
    key = _FMP_KEY_CAPTURED
    if not key:
        return curated, False
    pool = fmp_universe_tickers(key)
    if not pool:
        return curated, False
    # Union: curated names first (guaranteed coverage), then market-wide pool.
    merged = list(dict.fromkeys(curated + pool))
    return merged, True

# ─────────────────────────────────────────────────────────────
# POLYGON WHOLE-MARKET SCAN  (primary universe builder when a key is set)
# ─────────────────────────────────────────────────────────────
# Replaces the rate-limited ~4-calls-per-ticker yfinance scan (which capped the
# universe at ~85 names) with Polygon bulk endpoints:
#   • grouped-daily aggregates  → one call = every ticker's OHLCV for one day.
#     We pull POLY_HIST_DAYS of them ONCE (cold start), trim to the liquid
#     universe to stay memory-safe, and re-pivot only when a NEW trading day
#     appears — intraday refreshes reuse the cached daily bars.
#   • snapshot (all tickers)    → one call = a near-live quote for every ticker.
# Technicals (RSI/MACD/MAs/Bollinger/volume) are computed from the daily bars for
# the WHOLE liquid universe; fundamentals + real social sentiment stay LAZY —
# fetched per-ticker only when a user opens a name (see page_detail). Social BUZZ
# volume comes free from the already-cached keyless ApeWisdom map (no per-ticker
# fan-out), so volume-driven categories still surface market-wide names.
POLY_HIST_DAYS    = int(_os.environ.get("POLY_HIST_DAYS", "90"))      # daily bars for technicals
POLY_MAX_UNIVERSE = int(_os.environ.get("POLY_MAX_UNIVERSE", "2500")) # liquid names to score
POLY_WARM_DEADLINE= int(_os.environ.get("POLY_WARM_DEADLINE", "150")) # CPU scoring budget (s; more factors now)
# Stocks-only: filter the liquid universe to common stocks + ADRs (drops ETFs,
# ETNs, closed-end funds, units, warrants) via a daily-cached whole-market
# reference set. Set POLY_STOCKS_ONLY=0 to include ETFs/funds again.
POLY_STOCKS_ONLY  = _os.environ.get("POLY_STOCKS_ONLY", "1").strip().lower() not in ("0", "false", "no", "")
POLY_CS_TTL       = int(_os.environ.get("POLY_CS_TTL", "86400"))      # refresh stock-symbol set daily
POLY_SI_TTL       = int(_os.environ.get("POLY_SI_TTL", "21600"))      # refresh short interest every 6h (FINRA is bi-weekly)
POLY_SV_TTL       = int(_os.environ.get("POLY_SV_TTL", "21600"))      # refresh short VOLUME every 6h (daily data)
POLY_PE_TTL       = int(_os.environ.get("POLY_PE_TTL", "604800"))     # EPS cached 7d (fundamentals are quarterly)
POLY_PE_BACKFILL  = int(_os.environ.get("POLY_PE_BACKFILL", "200"))   # EPS fetches per warm (per-ticker; coverage builds over cycles)
# Bulk DIRECTIONAL sentiment: background-fill bull/bear for the buzzed subset so the scan's
# Sentiment component fires in-bulk (else direction stays neutral 50 — resolved lazily on detail).
BULK_SENT_ENABLED  = _os.environ.get("BULK_SENT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
BULK_SENT_BACKFILL = int(_os.environ.get("BULK_SENT_BACKFILL", "120"))  # directional fetches per warm (buzz subset)
BULK_SENT_TTL      = int(_os.environ.get("BULK_SENT_TTL", "21600"))     # cached ~6h
BULK_SENT_MIN_MSGS = int(_os.environ.get("BULK_SENT_MIN_MSGS", str(SENT_MIN_MSGS)))  # buzz floor to bother resolving

# ── SEC EDGAR (free, keyless) config ──
# SEC requires a descriptive User-Agent with a contact. We resolve it from the
# EDGAR_CONTACT secret/env (fall back to a generic mailbox); the adapter reads it
# from the EDGAR_UA env var, which we set once at import below.
EDGAR_ENABLED   = _os.environ.get("EDGAR_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
EDGAR_8K_TTL    = int(_os.environ.get("EDGAR_8K_TTL", "21600"))   # 8-K catalyst set refresh ~6h
EDGAR_8K_DAYS   = int(_os.environ.get("EDGAR_8K_DAYS", "4"))      # 8-K lookback (trading days)
EDGAR_INS_TTL   = int(_os.environ.get("EDGAR_INS_TTL", "43200"))  # insider-buy map refresh ~12h (daily filings)
EDGAR_INS_DAYS  = int(_os.environ.get("EDGAR_INS_DAYS", "3"))     # insider lookback (trading days)
EDGAR_INS_PARSE = int(_os.environ.get("EDGAR_INS_PARSE", "280"))  # max Form-4 filings parsed per refresh (SEC-polite)

# ── FRED macro regime (free, keyless) ──
FRED_ENABLED = _os.environ.get("FRED_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
FRED_TTL     = int(_os.environ.get("FRED_TTL", "21600"))  # macro regime refresh ~6h (daily series)


def _edgar_contact():
    """Contact string for SEC's required User-Agent. From EDGAR_CONTACT secret/env,
    else a generic fallback. SEC just wants a way to reach the requester."""
    try:
        c = (st.secrets.get("EDGAR_CONTACT") if hasattr(st, "secrets") else None)
    except Exception:
        c = None
    return (c or _os.environ.get("EDGAR_CONTACT") or "admin@scanviction.com").strip()

if HAS_EDGAR:
    # The adapter captured EDGAR_UA at import (before this block), so set BOTH the
    # env (for any subprocess) and the live module header dict (functions read the
    # module global at call time, so reassigning it takes effect immediately).
    _ua = f"Scanviction {_edgar_contact()}"
    _os.environ["EDGAR_UA"] = _ua
    try:
        _edgar.EDGAR_UA = {"User-Agent": _ua}
    except Exception:
        pass

# Cached bulk state: trimmed OHLCV frames for the current liquid universe. Guarded
# by _POLY_LOCK. Re-pivoted only on cold start or when last_date rolls to a new
# completed trading day.
_POLY_STATE = {"frames": {}, "universe": [], "last_date": "", "built_at": 0.0,
               "cs_set": None, "cs_built_at": 0.0,
               "si_map": None, "si_built_at": 0.0,
               "sv_map": None, "sv_built_at": 0.0,
               "eps_map": {},
               "sent_map": {},
               "k8_set": None, "k8_built_at": 0.0,
               "ins_map": None, "ins_built_at": 0.0,
               "regime": None, "regime_built_at": 0.0}
_POLY_LOCK  = _threading.Lock()

def _market_regime():
    """Cached (~6h) macro market regime from keyless FRED (VIX + credit spread +
    curve). Returns a dict (see fred_adapter.market_regime); a neutral 'unknown'
    snapshot on failure. Pure backdrop — does not alter scoring."""
    if not (HAS_FRED and FRED_ENABLED):
        return {"regime": "unknown", "label": "", "note": "", "asof": None}
    now = time.time()
    with _POLY_LOCK:
        m = _POLY_STATE.get("regime")
        built = _POLY_STATE.get("regime_built_at", 0.0)
    if m is not None and (now - built) < FRED_TTL:
        return m
    try:
        fresh = _fred.market_regime()
    except Exception as e:
        _record_health("fred", False, err=str(e))
        with _POLY_LOCK:
            return _POLY_STATE.get("regime") or {"regime": "unknown", "label": "", "note": "", "asof": None}
    if fresh and fresh.get("regime") != "unknown":
        with _POLY_LOCK:
            _POLY_STATE["regime"] = fresh
            _POLY_STATE["regime_built_at"] = now
        _record_health("fred", True)
        return fresh
    with _POLY_LOCK:
        return _POLY_STATE.get("regime") or (fresh or {"regime": "unknown", "label": "", "note": "", "asof": None})

def _edgar_8k_set():
    """Set of tickers with a fresh SEC 8-K (material-event catalyst) in the last few
    trading days, cached ~6h. Cheap (daily index only). Empty set gracefully on
    failure → the 8-K catalyst flag just stays off. Confirms gap/social catalysts."""
    if not (HAS_EDGAR and EDGAR_ENABLED):
        return set()
    now = time.time()
    with _POLY_LOCK:
        s = _POLY_STATE.get("k8_set")
        built = _POLY_STATE.get("k8_built_at", 0.0)
    if s is not None and (now - built) < EDGAR_8K_TTL:
        return s
    try:
        fresh = _edgar.recent_8k_tickers(days=EDGAR_8K_DAYS)
    except Exception as e:
        _record_health("edgar", False, err=f"8-K: {e}")
        with _POLY_LOCK:
            return _POLY_STATE.get("k8_set") or set()
    if fresh:
        with _POLY_LOCK:
            _POLY_STATE["k8_set"] = fresh
            _POLY_STATE["k8_built_at"] = now
        _record_health("edgar", True)
        return fresh
    with _POLY_LOCK:
        return _POLY_STATE.get("k8_set") or set()

def _edgar_insider_map(universe):
    """{ticker: {"buys", "value", "last"}} of recent OPEN-MARKET insider PURCHASES
    (SEC Form 4, code P) for names in our universe, cached ~12h. Bounded parse
    (EDGAR_INS_PARSE filings) so it never stalls the warm; SEC-polite. Powers the
    Insider Cluster category + a Conviction component. {} gracefully on failure."""
    if not (HAS_EDGAR and EDGAR_ENABLED):
        return {}
    now = time.time()
    with _POLY_LOCK:
        m = _POLY_STATE.get("ins_map")
        built = _POLY_STATE.get("ins_built_at", 0.0)
    if m is not None and (now - built) < EDGAR_INS_TTL:
        return m
    try:
        fresh = _edgar.recent_insider_buys(days=EDGAR_INS_DAYS, universe=universe,
                                           max_parse=EDGAR_INS_PARSE)
    except Exception as e:
        _record_health("edgar", False, err=f"insider: {e}")
        with _POLY_LOCK:
            return _POLY_STATE.get("ins_map") or {}
    # Even an empty result is a valid "no insider buys right now" snapshot — cache it
    # so we don't re-parse hundreds of filings every warm.
    with _POLY_LOCK:
        _POLY_STATE["ins_map"] = fresh or {}
        _POLY_STATE["ins_built_at"] = now
    _record_health("edgar", True)
    return fresh or {}

def _poly_short_volume(key):
    """Whole-market LATEST daily short-volume ratio (intraday shorting pressure),
    cached ~6h. {} gracefully on failure. Refines the squeeze categories — high
    short-volume ratio + rising price = shorts pressing into strength right now."""
    if not key:
        return {}
    now = time.time()
    with _POLY_LOCK:
        m = _POLY_STATE.get("sv_map")
        built = _POLY_STATE.get("sv_built_at", 0.0)
    if m is not None and (now - built) < POLY_SV_TTL:
        return m
    try:
        fresh = _poly.short_volume_latest(key)
    except Exception as e:
        _record_health("polygon", False, err=f"short-volume: {e}")
        with _POLY_LOCK:
            return _POLY_STATE.get("sv_map") or {}
    if fresh:
        with _POLY_LOCK:
            _POLY_STATE["sv_map"] = fresh
            _POLY_STATE["sv_built_at"] = now
        return fresh
    with _POLY_LOCK:
        return _POLY_STATE.get("sv_map") or {}

def _poly_eps_map(key, universe):
    """Diluted-EPS map (TTM), background-FILLED a few hundred tickers per warm and
    cached 7 days (fundamentals are quarterly, and there's no bulk financials on the
    Starter plan). Coverage of the universe builds up over a handful of warms;
    callers turn EPS into a live P/E = price / EPS. Returns {ticker: eps}. Threaded
    + deadline-bounded so it never stalls the warm; graceful on failure."""
    if not key:
        return {}
    now = time.time()
    with _POLY_LOCK:
        eps_map = dict(_POLY_STATE.get("eps_map") or {})
    todo = [t for t in universe
            if (eps_map.get(t, {}).get("at", 0) or 0) < now - POLY_PE_TTL][:POLY_PE_BACKFILL]
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FTimeout
        ex = ThreadPoolExecutor(max_workers=12)
        try:
            futs = {ex.submit(_poly.diluted_eps_ttm, key, t): t for t in todo}
            try:
                for fu in as_completed(futs, timeout=20):
                    t = futs[fu]
                    try: eps = fu.result()
                    except Exception: eps = None
                    eps_map[t] = {"eps": eps, "at": now}
            except _FTimeout:
                pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        with _POLY_LOCK:
            _POLY_STATE["eps_map"] = eps_map
    return {t: v.get("eps") for t, v in eps_map.items() if v.get("eps")}


def _bulk_sent_map(buzz):
    """Background-fill DIRECTIONAL sentiment (bull/bear %) for the top BUZZED tickers so the scan's
    Sentiment component fires in-bulk. Direction otherwise stays neutral 50 in `_poly_bulk_sent` to
    avoid a per-ticker fetch (resolved lazily on the detail page) — but only the ApeWisdom buzz
    subset (mentions >= BULK_SENT_MIN_MSGS) can score sentiment at all, so a small, cached backfill
    covers exactly the names that matter. Mirrors _poly_eps_map: reuse the EXISTING per-ticker
    resolver (_news_direction / Finnhub → _yf_news_direction / Yahoo+VADER, both keyless-capable),
    ~BULK_SENT_BACKFILL/warm, cached ~6h, threaded + deadline-bounded so it never stalls the warm.
    Returns {TICKER: {"bull","bear"}} for the resolved names (neutral names omitted)."""
    if not (BULK_SENT_ENABLED and buzz):
        return {}
    now = time.time()
    with _POLY_LOCK:
        sent_map = dict(_POLY_STATE.get("sent_map") or {})
    # strongest buzz first; only (re)fetch stale/missing entries — coverage builds over warms
    cand = sorted(((t, (a.get("mentions", 0) or 0)) for t, a in buzz.items()
                   if (a.get("mentions", 0) or 0) >= BULK_SENT_MIN_MSGS),
                  key=lambda x: x[1], reverse=True)
    todo = [t for t, _ in cand
            if (sent_map.get(t, {}).get("at", 0) or 0) < now - BULK_SENT_TTL][:BULK_SENT_BACKFILL]
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FTimeout
        def _dir(t):
            return _news_direction(t) or _yf_news_direction(t)   # (bull, bear, n) or None
        ex = ThreadPoolExecutor(max_workers=8)
        try:
            futs = {ex.submit(_dir, t): t for t in todo}
            try:
                for fu in as_completed(futs, timeout=25):
                    t = futs[fu]
                    try: r = fu.result()
                    except Exception: r = None
                    if r and r[2]:      # at least one scored article → a real direction
                        sent_map[t] = {"bull": int(r[0]), "bear": int(r[1]), "arts": int(r[2]), "at": now}
                    else:               # mark attempted so we don't re-hammer a no-news name until TTL
                        sent_map[t] = {"at": now}
            except _FTimeout:
                pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        with _POLY_LOCK:
            _POLY_STATE["sent_map"] = sent_map
    return {t: {"bull": v["bull"], "bear": v["bear"]}
            for t, v in sent_map.items() if v.get("bull") is not None}

def _poly_short_interest(key):
    """Whole-market LATEST FINRA short interest (days-to-cover), cached ~6h (FINRA
    reports bi-weekly, so that's plenty fresh). Returns {} gracefully if the endpoint
    fails or isn't on the plan — the Short Squeeze category then just stays empty
    rather than breaking. Powers real days-to-cover squeeze scoring."""
    if not key:
        return {}
    now = time.time()
    with _POLY_LOCK:
        m = _POLY_STATE.get("si_map")
        built = _POLY_STATE.get("si_built_at", 0.0)
    if m is not None and (now - built) < POLY_SI_TTL:
        return m
    try:
        fresh = _poly.short_interest_latest(key)
    except Exception as e:
        _record_health("polygon", False, err=f"short-interest: {e}")
        with _POLY_LOCK:
            return _POLY_STATE.get("si_map") or {}
    if fresh:
        with _POLY_LOCK:
            _POLY_STATE["si_map"] = fresh
            _POLY_STATE["si_built_at"] = now
        return fresh
    with _POLY_LOCK:
        return _POLY_STATE.get("si_map") or {}

def _poly_common_stock_set(key):
    """Daily-cached set of common-stock + ADR symbols (the stocks-only whitelist).
    Returns None if the reference fetch fails AND we have no cached set, so the
    caller can skip filtering rather than ship an empty/ETF-laden universe."""
    now = time.time()
    with _POLY_LOCK:
        cs = _POLY_STATE.get("cs_set")
        built = _POLY_STATE.get("cs_built_at", 0.0)
    if cs and (now - built) < POLY_CS_TTL:
        return cs
    try:
        symbols = _poly.list_ticker_symbols(key, types=("CS", "ADRC"))
    except Exception as e:
        _record_health("polygon", False, err=f"stock-list: {e}")
        return cs  # keep stale set on failure (may be None → no filter)
    if symbols:
        with _POLY_LOCK:
            _POLY_STATE["cs_set"] = symbols
            _POLY_STATE["cs_built_at"] = now
        return symbols
    return cs

def _poly_bulk_sent(ticker, buzz, sent_dir=None):
    """Bulk sentiment contract for a ticker from the keyless ApeWisdom buzz map. Social VOLUME
    (msgs) + 24h buzz trend come from ApeWisdom. DIRECTION (bull/bear) is filled from the background
    `_bulk_sent_map` (sent_dir) for buzzed names when resolved, else stays neutral 50 (resolved
    lazily on the detail page)."""
    a = buzz.get(ticker.upper()) if buzz else None
    _d = (sent_dir or {}).get(ticker.upper())
    _bull = int(_d["bull"]) if _d else 50
    _bear = int(_d["bear"]) if _d else 50
    if not a:
        return {"bull": _bull, "bear": _bear, "msgs": 0, "wl": 0, "buzz_trend": 0, "src": "bulk"}
    mentions = a.get("mentions", 0) or 0
    prev = a.get("mentions_24h_ago", 0) or 0
    bt = round(((mentions - prev) / prev) * 100) if prev else 0
    return {"bull": _bull, "bear": _bear, "msgs": mentions, "wl": 0,
            "buzz_trend": bt, "upvotes": a.get("upvotes", 0), "src": "bulk"}

def _poly_quote(ticker, snap, df):
    """Build the app's quote contract from the live snapshot, falling back to the
    last completed daily bar for any field the snapshot omits."""
    s = snap.get(ticker) or {}
    last = df.iloc[-1]
    price = s.get("price")
    if price is None:
        price = float(last["close"])
    price = float(price)
    prev = s.get("prev")
    if prev is None:
        prev = float(df.iloc[-2]["close"]) if len(df) >= 2 else price
    prev = float(prev)
    pct = s.get("pct")
    if pct is None:
        pct = round(((price - prev) / prev) * 100, 2) if prev else 0.0
    return {"price": round(price, 2), "prev": round(prev, 2), "pct": round(float(pct), 2),
            "chg": round(price - prev, 2),
            "open": round(float(s.get("open") or last["open"]), 2),
            "high": round(float(s.get("high") or last["high"]), 2),
            "low":  round(float(s.get("low")  or last["low"]),  2),
            "volume": int(s.get("volume") or last["volume"] or 0),
            "name": ticker}

def _poly_history_frames(key, universe, latest_date, latest_grouped):
    """Walk back POLY_HIST_DAYS trading days from latest_date, keeping ONLY the
    `universe` tickers' bars. Memory-safe: one full grouped day (~12k symbols) is
    held at a time, trimmed to the universe, then discarded. Returns
    {ticker: [chronological OHLCV rows]}."""
    uni = set(universe)
    frames = {}
    def _add_day(date_iso, grouped):
        for sym in uni:
            bar = grouped.get(sym)
            if not bar or bar.get("c") is None:
                continue
            frames.setdefault(sym, []).append({
                "datetime": date_iso, "open": bar.get("o"), "high": bar.get("h"),
                "low": bar.get("l"), "close": bar.get("c"), "volume": bar.get("v")})
    _add_day(latest_date, latest_grouped)          # newest day (already fetched)
    d = datetime.strptime(latest_date, "%Y-%m-%d").date() - timedelta(days=1)
    collected = 1
    scanned = 0
    max_lookback = POLY_HIST_DAYS * 2 + 20         # cap calendar scan past holidays
    while collected < POLY_HIST_DAYS and scanned < max_lookback:
        if d.weekday() < 5:                        # skip weekends without a call
            try:
                grouped = _poly.grouped_daily(key, d.isoformat())
            except Exception:
                grouped = {}
            if grouped:
                _add_day(d.isoformat(), grouped)
                collected += 1
        d -= timedelta(days=1)
        scanned += 1
    for sym in frames:                             # appended newest-first → chronological
        frames[sym].reverse()
    return frames

def _intraday_bar(t, snap, latest_date):
    """Today's forming (delayed) bar for ticker `t` from the snapshot, matching the frame
    schema ({datetime,open,high,low,close,volume}), so it can be appended to the daily history
    and make technicals/categories reflect today's intraday move. Returns None when intraday
    signals are off, the market is closed, the daily bars already cover today, or data is stale.

    VOLUME IS PACE-ADJUSTED. The raw snapshot volume is today's PARTIAL session volume,
    but every volume gate (Breakout Watch's vol_ratio>=1.4, Volatility Expansion's >=2,
    Catalyst/Gap, Capitulation's washout spike…) compares it against a FULL-day average —
    so at 10:30am a stock trading at 2× its normal pace still showed vol_ratio≈0.25 and
    the whole breakout family could only fire in the last hour of the session. We scale
    the partial volume to a full-day PACE estimate (partial ÷ fraction-of-session-elapsed),
    capped at 4× so the noisy first ~100 minutes can't wildly over-extrapolate."""
    if not INTRADAY_SIGNALS or not snap:
        return None
    try:
        _ms = market_status()
        if _ms.get("state") != "open":   # regular hours only
            return None
        # Fraction of the 6.5h session elapsed (seconds counts down to the close).
        _elapsed = 1.0 - (float(_ms.get("seconds", 0)) / 23400.0)
        _pace = 1.0 / max(_elapsed, 0.25)   # cap extrapolation at 4×
    except Exception:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    if today <= (latest_date or ""):
        return None   # grouped daily already includes today → don't double-count
    s = snap.get(t)
    if not s or s.get("price") is None:
        return None
    try:
        px = float(s["price"])
        o = float(s.get("open") or px); hi = float(s.get("high") or px); lo = float(s.get("low") or px)
        return {"datetime": today, "open": o, "high": max(hi, px), "low": min(lo, px),
                "close": px, "volume": float(s.get("volume") or 0) * _pace}
    except Exception:
        return None

def _build_universe_raw_polygon(key):
    """Build + score the whole-market liquid universe from Polygon bulk data.
    Returns (rows, hot) in the SAME row contract as the legacy scan, so every
    downstream consumer (Discover, composites, BI, detail) is unchanged."""
    t0 = time.time()
    # 1. Latest completed trading day's grouped bars (1 call) → liquid universe.
    latest = _poly.recent_grouped_days(key, days=1)
    if not latest:
        raise _poly.PolygonError("no recent grouped trading day with bars")
    latest_date, latest_grouped = latest[-1]
    # Stocks-only: rank/cap among common stocks + ADRs so the liquidity filter
    # isn't "used up" by ETFs/funds. Falls back to the unfiltered grouped if the
    # reference set is unavailable (better a few ETFs than an empty universe).
    grouped_for_uni = latest_grouped
    if POLY_STOCKS_ONLY:
        cs = _poly_common_stock_set(key)
        if cs:
            grouped_for_uni = {k: v for k, v in latest_grouped.items() if k in cs}
    universe = _poly.build_universe(grouped_for_uni, max_n=POLY_MAX_UNIVERSE)
    # Always include curated names that traded that day (guaranteed coverage of
    # the well-known watchlist even if they fall outside the liquidity cap).
    curated = _full_universe_tickers()
    uset = set(universe)
    for t in curated:
        if t in latest_grouped and t not in uset:
            universe.append(t); uset.add(t)
    # 2. Daily-bar history (cached; re-pivot only on cold start or a NEW day).
    with _POLY_LOCK:
        have = bool(_POLY_STATE.get("frames")) and _POLY_STATE.get("last_date") == latest_date
        if have:
            frames = _POLY_STATE["frames"]
            universe = _POLY_STATE["universe"] or universe
    if not have:
        frames = _poly_history_frames(key, universe, latest_date, latest_grouped)
        with _POLY_LOCK:
            _POLY_STATE["frames"] = frames
            _POLY_STATE["universe"] = universe
            _POLY_STATE["last_date"] = latest_date
            _POLY_STATE["built_at"] = time.time()
    # 3. Near-live snapshot for every ticker (1 call). Soft-fail to daily closes.
    try:
        snap = _poly.snapshot_all(key)
    except Exception:
        snap = {}
    # 4. Free keyless social buzz volume (already-cached ApeWisdom; no fan-out).
    try:
        buzz = _apewisdom_map()
    except Exception:
        buzz = {}
    # 4a. Background-fill DIRECTIONAL sentiment for the buzzed subset (cached ~6h) so the Sentiment
    #     component fires in-bulk; {} if disabled/unavailable → direction just stays neutral.
    try:
        sent_dir = _bulk_sent_map(buzz)
    except Exception:
        sent_dir = {}
    hot = _tiered_get("__hot__", "hot", MOD_TTL, lambda _: _raw_hot()) or []
    hotset = set(hot)
    # 4b. Whole-market FINRA short interest / days-to-cover (cached ~6h) — powers the
    #     real Short Squeeze category. {} if unavailable → squeeze just stays empty.
    si_map = _poly_short_interest(key)
    # 4c. Diluted-EPS map (background-filled, cached 7d) → live P/E for Value Momentum.
    eps_map = _poly_eps_map(key, universe)
    # 4d. Daily short-VOLUME ratio (intraday shorting pressure), cached ~6h.
    sv_map = _poly_short_volume(key)
    # 4e. SEC EDGAR (free, keyless): fresh 8-K catalysts (set) + open-market insider
    #     PURCHASES (Form 4 code P) for our universe. Both cached + bounded; empty on
    #     failure → Insider Cluster / the 8-K flag just stay off.
    k8_set = _edgar_8k_set()
    ins_map = _edgar_insider_map(universe)
    # 5. Score in liquidity order (universe is dollar-volume ranked) under a CPU
    #    deadline — if the long tail can't finish, the most-liquid names are done.
    rows = []
    deadline = time.time() + POLY_WARM_DEADLINE
    for _ti, t in enumerate(universe):
        if time.time() > deadline:
            break
        # GIL relief: this tight pandas loop runs ~45s in a background thread of the
        # SAME process serving web requests — without yields it starves the server
        # and pages intermittently time out during every re-score. A tiny sleep
        # every 25 tickers hands the GIL back (~0.5s total added per warm).
        if _ti and _ti % 25 == 0:
            time.sleep(0.005)
        recs = frames.get(t)
        if not recs or len(recs) < 14:
            continue
        try:
            df = pd.DataFrame(recs)
            # Append today's (delayed) forming bar so technicals + categories reflect the intraday
            # move → entries fire through the day, not all at once at EOD. No-op off-hours / when
            # intraday signals are off. Appends to the DF (not the cached `recs`) so the shared
            # per-day frame cache is never mutated.
            _ib = _intraday_bar(t, snap, latest_date)
            if _ib is not None:
                df = pd.concat([df, pd.DataFrame([_ib])], ignore_index=True)
            q = _poly_quote(t, snap, df)
            sent = _poly_bulk_sent(t, buzz, sent_dir)
            # Real fundamentals on the bulk row: days-to-cover (FINRA short interest)
            # and P/E (price / TTM diluted EPS). Ignore the DTC≈1000 junk illiquid OTC
            # names report (not in our liquid universe anyway, but cap defensively).
            info = {}
            si = si_map.get(t)
            if si:
                _dtc = si.get("days_to_cover")
                if _dtc is not None and 0 < _dtc < 100:
                    info["dtc"] = float(_dtc)
                if si.get("short_interest") is not None:
                    info["si_shares"] = si.get("short_interest")
            _eps = eps_map.get(t)
            if _eps and _eps > 0 and q.get("price"):
                info["pe"] = round(q["price"] / _eps, 1)
            # SEC EDGAR signals: fresh 8-K material-event flag + insider open-market buys.
            if t in k8_set:
                info["has_8k"] = True
            _ins = ins_map.get(t)
            if _ins and (_ins.get("buys") or 0) > 0:
                info["insider_buys"] = int(_ins.get("buys") or 0)
                info["insider_value"] = float(_ins.get("value") or 0.0)
                info["insider_last"] = _ins.get("last") or ""
            ind = precompute_indicators(df)   # RSI/MA/MACD once, shared by both calls below
            sc, bd, op, risk, conf = compute_scores(df, info or None, sent, ind=ind)
            factors = compute_factors(df, ind=ind)   # rich factors for categories + scorecard
            factors["svr"] = sv_map.get(t, 0.0) or 0.0   # intraday short-volume ratio
            # ig (plain-English insight badges) stays empty in the bulk warm — cards
            # are intentionally minimal; the detail page computes full insights lazily.
            rows.append({"t": t, "q": q, "sc": sc, "bd": bd, "ig": [], "op": op,
                         "risk": risk, "conf": conf, "hot": t in hotset, "df": df,
                         "info": info, "sent": sent, "factors": factors})
        except Exception:
            continue
    with _UNIVERSE_LOCK:
        _UNIVERSE_CACHE["market_wide"] = True
        _UNIVERSE_CACHE["scanned"] = len(universe)
        _UNIVERSE_CACHE["ok"] = len(rows)
    _record_health("polygon", True, int((time.time() - t0) * 1000))
    try:
        sys.stderr.write(f"[polygon] warm: scored {len(rows)}/{len(universe)} "
                         f"tickers @ {latest_date} in {int((time.time()-t0)*1000)}ms\n")
    except Exception:
        pass
    return rows, hot

def _build_universe_raw():
    """Score the universe via the tiered cache (no Streamlit dependency).

    CURATED-FIRST strategy: we always score the curated ~85 names first so the
    app becomes usable within seconds even when an FMP key is set. Market-wide
    FMP-discovered tickers (capped at FMP_MAX_POOL) are appended AFTER, so they
    enrich results without delaying the initial warm. Scoring is bounded so we
    never fire thousands of yfinance calls (which get rate-limited and stall).
    Returns (rows, hot, scanned_count).
    """
    # Polygon whole-market path (preferred when a key is configured): scores
    # thousands of liquid tickers from bulk endpoints. On ANY failure we fall
    # through to the legacy curated yfinance/FMP scan so the app always renders.
    if _POLYGON_KEY_CAPTURED:
        try:
            return _build_universe_raw_polygon(_POLYGON_KEY_CAPTURED)
        except Exception as e:
            _record_health("polygon", False, err=e)
    hot = _tiered_get("__hot__", "hot", MOD_TTL, lambda _: _raw_hot())
    curated = _full_universe_tickers()
    # Discover market-wide candidates (cached); these come AFTER curated.
    extra = []
    market_wide = False
    key = _FMP_KEY_CAPTURED
    if key:
        try:
            pool = fmp_universe_tickers(key)
            if pool:
                market_wide = True
                # only names not already in curated, capped
                cset = set(curated)
                extra = [t for t in pool if t not in cset][:FMP_MAX_POOL]
        except Exception:
            market_wide = False
    universe = list(dict.fromkeys(curated + (hot or [])[:10] + extra))
    # Score tickers CONCURRENTLY. _tiered_get releases _DATA_LOCK during the
    # network fetch, so a bounded thread pool cuts the first warm from ~2 min
    # (sequential) to ~15-20s. Keep the pool modest: on Streamlit Cloud's shared
    # egress IP, too many simultaneous yfinance calls invite rate-limiting — tune
    # via WARM_MAX_WORKERS if you move to a dedicated IP / paid data tier.
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FuturesTimeout
    _hot = hot or []
    _workers = max(1, int(_os.environ.get("WARM_MAX_WORKERS", "8")))
    rows = []
    # Bound the warm with a hard deadline. yfinance's .info/.history can hang or
    # back off for a long time on delisted/rate-limited tickers (dead meme names
    # like MULN/FFIE/NKLA), and a single stuck future would otherwise freeze the
    # whole warm — the ThreadPoolExecutor 'with' block blocks on shutdown until
    # every future finishes — so the UI sticks on 'Preparing live market data…'
    # forever. Collect whatever scored before the deadline and abandon stragglers
    # (the next refresh cycle retries them).
    _deadline = int(_os.environ.get("WARM_DEADLINE_SEC", "45"))
    ex = ThreadPoolExecutor(max_workers=_workers)
    try:
        futs = [ex.submit(_tiered_score_ticker, t, _hot) for t in universe]
        try:
            for fu in as_completed(futs, timeout=_deadline):
                try:
                    r = fu.result()
                    if r:
                        rows.append(r)
                except Exception:
                    continue
        except _FuturesTimeout:
            pass  # deadline hit — proceed with partial rows
    finally:
        # Don't block on hung futures; cancel anything not yet started.
        ex.shutdown(wait=False, cancel_futures=True)
    with _UNIVERSE_LOCK:
        _UNIVERSE_CACHE["market_wide"] = market_wide
        _UNIVERSE_CACHE["scanned"] = len(universe)
        _UNIVERSE_CACHE["ok"] = len(rows)
    return rows, (hot or [])

# ── Category-entry detection → signal events (feeds the notifications) ──
_PREV_CATS = {}            # ticker -> last primary_cat the worker saw (baseline)
_PREV_CATS_READY = False   # the first full re-score only establishes the baseline
CATEGORY_ENTRY_FIT = float(_os.environ.get("CATEGORY_ENTRY_FIT", "18"))  # min fit to count as a real entry
CATEGORY_ENTRY_MAX = int(_os.environ.get("CATEGORY_ENTRY_MAX", "40"))    # cap events/cycle — keeps it non-spammy

# ── Volatility-based suggested stop-loss ───────────────────────────────────────
STOP_ATR_MULT = float(_os.environ.get("STOP_ATR_MULT", "2.5"))  # stop distance = mult × daily ATR%

def recommended_stop(price, atr_pct, direction="long"):
    """A suggested stop sized by VOLATILITY: distance = STOP_ATR_MULT × the stock's daily ATR%, so a
    more volatile name gets a WIDER stop (more breathing room) and a calm one a tighter stop. Clamped
    to a practical 3–25% band. Long → stop BELOW price; short → stop ABOVE. Returns {price, pct,
    atr_pct} or None. (No take-profit — that's intentionally left to the user.)"""
    try:
        price = float(price or 0); atr = float(atr_pct or 0)
        if price <= 0:
            return None
        pct = atr * STOP_ATR_MULT
        if pct <= 0:
            pct = 6.0   # no ATR (thin / short history) → a neutral default
        pct = max(3.0, min(25.0, pct))
        stop = price * (1 - pct / 100.0) if direction != "short" else price * (1 + pct / 100.0)
        return {"price": round(stop, 2), "pct": round(pct, 1), "atr_pct": round(atr, 1)}
    except Exception:
        return None

# ── Instant Telegram delivery of freshly-recorded signals ──────────────────────
# The app RECORDS signals; historically only a separate alerts_worker cron DELIVERED them, so
# with no cron running (e.g. local dev) nothing ever reached Telegram. Deliver inline instead:
# the moment the universe worker records NEW composite signals, push them to entitled Telegram
# subscribers, once each. Runs in the worker THREAD (no Streamlit session) → reads users from
# the shared store and uses the token captured on the main thread. A dedup store makes each
# (user, signal) send exactly once. The cron's own composite delivery is gated OFF by default
# (alerts_worker: WORKER_DELIVERS_SIGNALS) so the two never double-send.
def _tg_send_raw(token, chat_id, text):
    """Thread-safe Telegram send (no st.secrets / session needed). True on HTTP 200.
    Failures are recorded to Data Health ('telegram_alerts') — a rejected send
    (blocked bot, bad chat id, HTML parse error) used to vanish without a trace."""
    try:
        import requests as _rq
        r = _rq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}, timeout=10)
        ok = r.status_code == 200
        try:
            _record_health("telegram_alerts", ok,
                           err=None if ok else f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception:
            pass
        return ok
    except Exception as e:
        try: _record_health("telegram_alerts", False, err=f"{type(e).__name__}: {str(e)[:120]}")
        except Exception: pass
        return False

def _signal_stop_line(tkr, cat, price):
    """ATR-based suggested-stop line for a signal alert (direction-aware). '' on any failure."""
    try:
        _sdir = "short" if category_dir(cat) == "bear" else "long"
        _wrow = _warm_row_for(tkr)
        _atr = ((_wrow or {}).get("factors") or {}).get("atr_pct", 0) if _wrow else 0
        _stop = recommended_stop(price, _atr, _sdir)
        if _stop:
            return (f"\n🛑 Suggested stop: <b>${_stop['price']:,.2f}</b> ({_stop['pct']:.1f}% "
                    f"{'above' if _sdir == 'short' else 'below'})")
    except Exception:
        pass
    return ""

def _deliver_new_signals(added):
    """Push newly-recorded signals to subscribers, once each, in TWO lanes over TWO channels:

      • EVENT alerts (insider / 8-K / short-interest) are time-sensitive filings → sent
        IMMEDIATELY, one message each ("look now" layer).
      • Category/technical ENTRIES are batched into ONE ranked digest per user per cycle
        (top-N by score×edge) — they all land together on a new daily bar, so individual
        pings would flood; one "here are the new setups" summary reads better.

    Channels: TELEGRAM (premium, opt-in chat id + telegram_enabled) AND EMAIL (any tier with
    email_enabled that has opted into category_alerts — Resend, in-process so it works on
    Streamlit Cloud without the external cron). Both narrow to the user's category_alerts.
    Deduped per (user, signal) across channels via the shared delivered store."""
    if not added:
        return
    token = _TG_TOKEN_CAPTURED
    try:
        import msp_store as _ms
        _dkey = _ms._data_path("msp_tg_delivered.json", "TG_DELIVERED_PATH")
        users = _ms.read_json(_ms.USERS_DB_PATH, {}) or {}
        delivered = _ms.read_json(_dkey, {}) or {}
        changed = False

        events  = [e for e in added if e.get("ticker") and e.get("category") in EVENT_ALERT_TYPES]
        signals = [e for e in added if e.get("ticker") and e.get("category") not in EVENT_ALERT_TYPES]
        # Strongest first for the digest: score weighted by the category's backtested edge.
        signals.sort(key=lambda e: (float(e.get("score_at_trigger", 0) or 0)
                                    * category_edge(e.get("category", ""))), reverse=True)
        DIGEST_MAX = int(_os.environ.get("DIGEST_MAX", "12"))

        for email, u in users.items():
            prefs = u.get("notif_prefs") or {}
            is_prem = u.get("role", "free") in ("premium", "admin", "owner")
            # Master pause: the Settings "Auto-Signals" toggle actually stops delivery
            # (previously it was saved but never checked here).
            if not u.get("prop_signals_enabled", True):
                continue
            if not prefs.get("category_alerts", True):
                continue
            cid = u.get("telegram_chat_id", "")
            wanted = u.get("category_alerts") or []
            do_tg = bool(token and cid and is_prem and prefs.get("telegram_enabled", True))
            # Email digest: premium users are IN by default (all categories — matching what
            # the Settings page shows as the default coverage); free users opt in by picking
            # categories. The email_enabled master toggle always wins.
            do_email = bool(prefs.get("email_enabled", True) and (wanted or is_prem))
            if not (do_tg or do_email):
                continue

            # ── Lane 1: EVENT alerts — immediate, one message each ──
            for ev in events:
                cat = ev.get("category", ""); tkr = ev["ticker"]
                if wanted and cat not in wanted:
                    continue
                eid = ev.get("id") or f"{tkr}_{cat}"
                dk = f"{email}:{eid}"
                if dk in delivered:
                    continue
                price = float(ev.get("trigger_price", 0) or 0)
                rec = ev.get("recommendation", "") or ""
                sent = False
                if do_tg:
                    tg = (f"📊 <b>{tkr}</b> — <b>{cat}</b>\n\n{rec}\n"
                          f"Price: <b>${price:,.2f}</b>{_signal_stop_line(tkr, cat, price)}\n"
                          f"Open Scanviction for the full breakdown.")
                    if _tg_send_raw(token, cid, tg): sent = True
                if do_email:
                    if _alert_email(email, f"📊 {cat}: {tkr}",
                            [f"<b>{_esc(tkr)}</b> — <b>{_esc(cat)}</b>", _esc(rec),
                             f"Price: <b>${price:,.2f}</b>"]): sent = True
                # Native Web Push (premium, opted-in devices) — instant, self-hosted.
                if (_webpush is not None and is_prem and prefs.get("push_enabled", True)
                        and u.get("push_subscriptions")):
                    try:
                        _n, _ = _webpush.send_to_user(u, f"📊 {cat}: {tkr}",
                                                      f"{rec} · ${price:,.2f}")
                        if _n: sent = True
                    except Exception:
                        pass
                if sent:
                    delivered[dk] = time.time(); changed = True

            # ── Lane 2: category ENTRIES — one ranked digest ──
            mine = []
            for ev in signals:
                cat = ev.get("category", "")
                if wanted and cat not in wanted:
                    continue
                eid = ev.get("id") or f"{ev['ticker']}_{cat}"
                if f"{email}:{eid}" not in delivered:
                    mine.append(ev)
            if mine:
                shown = mine[:DIGEST_MAX]
                more = len(mine) - len(shown)
                tg_lines, em_lines = [], []
                for ev in shown:
                    _sc = ev.get("score_at_trigger", "?"); _rec = ev.get("recommendation", "") or ""
                    _cat = ev.get("category", "")
                    tg_lines.append(f"• <b>{ev['ticker']}</b> · {_cat} · <b>{_sc}/100</b>"
                                    + (f" · {_rec}" if _rec else ""))
                    em_lines.append(f"• <b>{_esc(ev['ticker'])}</b> · {_esc(_cat)} · <b>{_sc}/100</b>"
                                    + (f" · {_esc(_rec)}" if _rec else ""))
                tail = f"\n…and {more} more in the app." if more > 0 else ""
                hd = f"{len(mine)} new setup{'s' if len(mine) != 1 else ''}"
                sent = False
                if do_tg:
                    digest = (f"📊 <b>{hd}</b> — Scanviction\n\n" + "\n".join(tg_lines) + tail
                              + "\n\nOpen Scanviction for the full multi-factor breakdowns.")
                    if _tg_send_raw(token, cid, digest): sent = True
                if do_email:
                    if _alert_email(email, f"📊 {hd}",
                            [f"<b>{hd}</b>"] + em_lines
                            + ([f"…and {more} more in the app."] if more > 0 else [])): sent = True
                # Native Web Push digest (one notification per cycle, premium devices)
                if (_webpush is not None and is_prem and prefs.get("push_enabled", True)
                        and u.get("push_subscriptions")):
                    try:
                        _top = ", ".join(f"{e_['ticker']} {e_.get('score_at_trigger','?')}/100"
                                         for e_ in shown[:4])
                        _n, _ = _webpush.send_to_user(u, f"📊 {hd} — Scanviction", _top)
                        if _n: sent = True
                    except Exception:
                        pass
                if sent:
                    for ev in mine:   # mark ALL delivered (incl. beyond the shown top-N)
                        eid = ev.get("id") or f"{ev['ticker']}_{ev.get('category','')}"
                        delivered[f"{email}:{eid}"] = time.time()
                    changed = True

        if changed:
            cutoff = time.time() - 7 * 86400   # bound the dedup store to ~7 days
            delivered = {k: v for k, v in delivered.items() if (v or 0) > cutoff}
            _ms.write_json(_dkey, delivered)
        # Cycle summary → Data Health, so "are notifications going out?" is
        # answerable from Admin instead of guesswork.
        try:
            _record_health("alerts", True,
                           err=f"{len(added)} new event(s), {len(users)} user(s) checked, "
                               f"tg={'on' if token else 'OFF'}, email={'on' if _RESEND_KEY_CAPTURED else 'key?'}")
        except Exception:
            pass
    except Exception as e:
        # This used to be a bare pass — ONE unexpected error silently killed ALL
        # Telegram/email/push delivery with nothing in any log.
        try:
            _record_health("alerts", False, err=f"{type(e).__name__}: {str(e)[:140]}")
            import traceback as _tb
            sys.stderr.write(f"[deliver_signals] {_tb.format_exc()}\n")
        except Exception:
            pass

# ── Market-regime gate on signal generation ────────────────────────────────────
# Backtest finding: the signal is strong in trending tape and goes quiet in chop / stress. So in a
# RISK-OFF regime demand a stronger category fit for LONG setups (fewer, higher-quality longs) and
# relax it for SHORTs; RISK-ON is the mirror. Tunable / disable-able via REGIME_GATE.
REGIME_GATE = _os.environ.get("REGIME_GATE", "1").lower() in ("1", "true", "yes")

def _regime_bias():
    """Current macro risk regime ('risk_on' / 'risk_off' / 'neutral') from the cached FRED read."""
    try:
        r = (_market_regime() or {}).get("regime")
        return r if r in ("risk_on", "risk_off") else "neutral"
    except Exception:
        return "neutral"

def _regime_entry_fit(direction):
    """Regime-aware minimum category-fit to RECORD an entry. Neutral/unknown or REGIME_GATE off →
    the base CATEGORY_ENTRY_FIT."""
    base = CATEGORY_ENTRY_FIT
    if not REGIME_GATE:
        return base
    reg = _regime_bias(); is_bear = (direction == "bear")
    if reg == "risk_off":
        return base * (0.8 if is_bear else 1.4)
    if reg == "risk_on":
        return base * (1.4 if is_bear else 0.85)
    return base

def _record_category_entries(rows):
    """Detect stocks that NEWLY entered a composite category since the last FULL
    re-score and log a signal event for the strongest ones (deduped 20h by
    signal_engine). The first re-score only establishes the baseline, so we never
    flood on cold start. Leader-only (multi-replica safe). These events feed the
    in-app Signals feed AND the alerts worker's push/telegram/email delivery —
    categories here are the SAME ones Discover shows (both use scoring.py)."""
    global _PREV_CATS, _PREV_CATS_READY
    if not HAS_SIGNAL_ENGINE:
        return
    try: is_leader = _worker_is_leader()
    except Exception: is_leader = True
    new_map = {}; fresh = []
    for r in rows:
        t = r.get("t"); cat = r.get("primary_cat")
        if not t:
            continue
        if cat:
            new_map[t] = cat
        if cat and cat != _PREV_CATS.get(t) and (r.get("comp", 0) or 0) >= _regime_entry_fit(category_dir(cat)):
            fresh.append(r)
    if _PREV_CATS_READY and is_leader and fresh:
        # strongest first, weighted by the category's backtested edge → the highest-edge setups win
        # the per-cycle alert slots (lean into what works).
        fresh.sort(key=lambda r: (r.get("comp", 0) or 0) * category_edge(r.get("primary_cat", "")), reverse=True)
        specs = []
        for r in fresh[:CATEGORY_ENTRY_MAX]:
            try:
                q = r.get("q") or {}
                try: rec_lbl, _, _ = get_recommendation(r.get("sc", 0), r.get("bd", {}), r.get("info"))
                except Exception: rec_lbl = "WATCH"
                specs.append(dict(
                    ticker=r["t"], category=r["primary_cat"], score=int(r.get("sc", 0) or 0),
                    score_components=r.get("bd", {}), price=float(q.get("price", 0) or 0),
                    info=r.get("info"), sent=r.get("sent"), recommendation=rec_lbl,
                ))
            except Exception:
                pass
        if specs:
            try:
                _added = record_signal_events_bulk(specs)   # one history read+write, not one per event
                _deliver_new_signals(_added)                # instant Telegram push for the NEW ones
            except Exception: pass
    _PREV_CATS = new_map
    _PREV_CATS_READY = True

# ── Event-driven alerts: insider Form-4 buys, fresh 8-K, short-interest surge ──
# These are DISTINCT from category entries — they fire on a filing/data change, not
# a technical setup. They reuse the same signal-event pipeline (feed + worker), so
# they get the same delivery + outcome tracking for free.
EVT_INSIDER = "🏛️ Insider Buy"
EVT_8K      = "📰 8-K Filing"
EVT_SHORT   = "📊 Short Interest"
EVENT_ALERT_TYPES = (EVT_INSIDER, EVT_8K, EVT_SHORT)
SI_ALERT_DTC   = float(_os.environ.get("SI_ALERT_DTC", "7"))    # days-to-cover that counts as "notable"
EVENT_ALERT_MAX = int(_os.environ.get("EVENT_ALERT_MAX", "12")) # cap per type per cycle (non-spammy)
_PREV_INSIDER = set()      # tickers with open-market insider buys last warm
_PREV_8K = set()           # tickers with a fresh 8-K last warm
_PREV_SI_HOT = set()       # tickers with notable days-to-cover last warm
_PREV_EVENTS_READY = False  # first warm only establishes the baseline (no flood)

def _record_event_signals(rows):
    """Detect NEW filing / data events on the freshly-warmed rows — open-market
    insider buys (SEC Form 4), fresh 8-K catalysts, and short-interest surges — and
    log them as signal events so they flow into the Signals feed AND the alerts
    worker's delivery. Baselines on the first warm (never floods); 20h-deduped by
    signal_engine; leader-only (multi-replica safe). No-op without the EDGAR/short-
    interest data (legacy path) — graceful."""
    global _PREV_INSIDER, _PREV_8K, _PREV_SI_HOT, _PREV_EVENTS_READY
    if not HAS_SIGNAL_ENGINE:
        return
    try: is_leader = _worker_is_leader()
    except Exception: is_leader = True

    ins_now, k8_now, si_now = set(), set(), set()
    new_ins, new_8k, new_si = [], [], []
    for r in rows:
        t = r.get("t"); info = r.get("info") or {}
        if not t:
            continue
        if int(info.get("insider_buys", 0) or 0) > 0:
            ins_now.add(t)
            if t not in _PREV_INSIDER: new_ins.append(r)
        if info.get("has_8k"):
            k8_now.add(t)
            if t not in _PREV_8K: new_8k.append(r)
        if float(info.get("dtc", 0) or 0) >= SI_ALERT_DTC:
            si_now.add(t)
            if t not in _PREV_SI_HOT: new_si.append(r)

    if _PREV_EVENTS_READY and is_leader:
        specs = []
        def _rec(r, cat, detail):
            try:
                q = r.get("q") or {}
                specs.append(dict(
                    ticker=r["t"], category=cat,
                    score=int(r.get("conviction") or r.get("sc", 0) or 0),
                    score_components=r.get("bd", {}), price=float(q.get("price", 0) or 0),
                    info=r.get("info"), sent=r.get("sent"), recommendation=detail))
            except Exception:
                pass
        for r in sorted(new_ins, key=lambda x: x.get("conviction", 0) or 0, reverse=True)[:EVENT_ALERT_MAX]:
            info = r.get("info") or {}
            n = int(info.get("insider_buys", 0) or 0); val = float(info.get("insider_value", 0.0) or 0.0)
            vs = fmt_money(val)
            _rec(r, EVT_INSIDER, f"{n} open-market insider buy{'s' if n>1 else ''} ({vs}, SEC Form 4)")
        for r in sorted(new_8k, key=lambda x: x.get("conviction", 0) or 0, reverse=True)[:EVENT_ALERT_MAX]:
            _rec(r, EVT_8K, "Fresh SEC 8-K filing — possible material catalyst")
        for r in sorted(new_si, key=lambda x: (x.get("info") or {}).get("dtc", 0) or 0, reverse=True)[:EVENT_ALERT_MAX]:
            dtc = float((r.get("info") or {}).get("dtc", 0) or 0)
            _rec(r, EVT_SHORT, f"Days-to-cover at {dtc:.1f} — short-squeeze fuel building")
        if specs:
            try:
                _added = record_signal_events_bulk(specs)   # one history read+write for all event types
                _deliver_new_signals(_added)                # instant Telegram push for the NEW ones
            except Exception: pass

    _PREV_INSIDER, _PREV_8K, _PREV_SI_HOT = ins_now, k8_now, si_now
    _PREV_EVENTS_READY = True

def _refresh_universe_now():
    """Rebuild the warm universe snapshot. ALWAYS marks the attempt complete so
    the UI's warming state terminates even if scoring yields nothing (e.g. the
    data provider is rate-limiting) — preventing the infinite 'Preparing…' loop.
    """
    err = ""
    try:
        rows, hot = _build_universe_raw()
        assign_categories(rows)   # fill rel-strength + assign each row its unique primary category
    except Exception as e:
        rows, hot, err = [], [], str(e)[:200]
    with _UNIVERSE_LOCK:
        _UNIVERSE_CACHE["attempted"] = True
        _UNIVERSE_CACHE["last_error"] = err
        if rows:  # never overwrite a good snapshot with an empty one
            _UNIVERSE_CACHE["rows"] = rows
            _UNIVERSE_CACHE["hot"] = hot
            _UNIVERSE_CACHE["built_at"] = time.time()
    # Persist snapshots for everything currently scored (leader only).
    if rows and _worker_is_leader():
        try:
            record_recommendations_bulk("__universe__", [
                (r.get("t",""), (r.get("q") or {}).get("price", 0),
                 r.get("sc"), r.get("op"), r.get("why")) for r in rows
            ])
        except Exception:
            pass
    # Log NEW category entries + filing/data events (feeds the Signals feed + notifications).
    if rows:
        try: _record_category_entries(rows)
        except Exception: pass
        try: _record_event_signals(rows)
        except Exception: pass
    # SEO snapshot for the standalone static-site generator (leader only; never blocks the warm).
    if rows and _worker_is_leader() and seo_export.ENABLED:
        try: seo_export.write_snapshot(rows)
        except Exception: pass
    return rows

def _refresh_prices_only():
    """Cheap intraday refresh: re-pull just the Polygon snapshot (ONE call) and
    update the displayed price / % / volume on the already-scored rows in place.
    Daily-bar technicals and category assignments are intentionally NOT touched
    (they only move on a new daily bar), so we skip the expensive re-score and the
    Discover list still feels live. No-op without a Polygon key (legacy path relies
    on the full re-warm cadence instead)."""
    key = _POLYGON_KEY_CAPTURED
    if not key:
        return
    try:
        snap = _poly.snapshot_all(key)
    except Exception as e:
        _record_health("polygon", False, err=f"price-refresh: {e}")
        return
    if not snap:
        return
    updated = 0
    with _UNIVERSE_LOCK:
        for r in _UNIVERSE_CACHE.get("rows", []):
            s = snap.get(r.get("t"))
            if not s or s.get("price") is None:
                continue
            q = dict(r.get("q") or {})
            price = float(s["price"]); prev = float(s.get("prev") or q.get("prev") or price)
            q["price"] = round(price, 2)
            q["prev"]  = round(prev, 2)
            q["pct"]   = round(s["pct"] if s.get("pct") is not None else (((price-prev)/prev*100) if prev else 0.0), 2)
            q["chg"]   = round(price - prev, 2)
            if s.get("volume") is not None:
                q["volume"] = int(s["volume"])
            r["q"] = q
            updated += 1
        _UNIVERSE_CACHE["built_at"] = time.time()
    _record_health("polygon_price", True, err=f"{updated} prices")

# ── Leader election for multi-replica deploys ──
# Only ONE process should do the heavy snapshot/outcome writing, or replicas
# would duplicate work and race. We elect a leader via a short-lived lock row in
# the shared store; without a DB (single replica) every process is the leader.
_LEADER_ID = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=12))
LEADER_TTL = 180  # seconds; leader must renew within this window

_LAST_OUTCOMES_REFRESH = [0.0]   # worker-thread-only state (thread keeps its namespace)
OUTCOMES_REFRESH_SEC = int(_os.environ.get("OUTCOMES_REFRESH_SEC", "3600"))

def _refresh_signal_outcomes_inline():
    """Hourly, leader-only: re-price tracked signal events (forward returns, max
    upside/drawdown, success/failure labels) so the Track Record + Signal Engine
    stats stay current WITHOUT the retired cron worker. Prices from Polygon."""
    if not HAS_SIGNAL_ENGINE:
        return
    now = time.time()
    if now - _LAST_OUTCOMES_REFRESH[0] < OUTCOMES_REFRESH_SEC:
        return
    try:
        if not _worker_is_leader():
            return
    except Exception:
        pass
    _LAST_OUTCOMES_REFRESH[0] = now

    def _pf(t, days):
        try:
            if _poly is not None and _POLYGON_KEY_CAPTURED:
                bars = _poly.daily_bars(_POLYGON_KEY_CAPTURED, t, days=max(int(days), 30))
                if bars:
                    return pd.DataFrame(bars).rename(
                        columns={"o": "open", "h": "high", "l": "low",
                                 "c": "close", "v": "volume", "date": "datetime"})
        except Exception:
            pass
        return None

    try:
        update_signal_outcomes(_pf)
    except Exception:
        pass


def _worker_is_leader():
    """True if this process currently holds (or can take) the worker lease."""
    if storage_backend() != "postgres":
        return True  # single-replica / JSON mode: this process is the leader
    try:
        lease = _read_json("__worker_leader__", {})
        now = time.time()
        if (not lease) or (now - lease.get("ts", 0) > LEADER_TTL) or (lease.get("id") == _LEADER_ID):
            _write_json("__worker_leader__", {"id": _LEADER_ID, "ts": now})
            # re-read to confirm we won (last-write-wins is acceptable here)
            return _read_json("__worker_leader__", {}).get("id") == _LEADER_ID
        return False
    except Exception:
        return True  # fail open — better to write than to stall tracking

def _alert_email(to, subject, body_lines):
    """Send an alert/digest email via Resend. `body_lines` is a list of already-safe HTML
    lines (or a string). No-op (returns False) without RESEND_API_KEY. Runs in-process so
    email alerts work on Streamlit Cloud WITHOUT the external cron worker."""
    # Captured-on-main-thread values FIRST — this often runs on the worker thread,
    # where a st.secrets read can fail and silently disable all email alerts.
    key = _RESEND_KEY_CAPTURED
    if not key:
        try: key = st.secrets.get("RESEND_API_KEY", "") or ""
        except Exception: key = ""
    if not key or not to:
        return False
    frm = _EMAIL_FROM_CAPTURED
    if not frm:
        try: frm = st.secrets.get("EMAIL_FROM", "Scanviction <noreply@scanviction.com>")
        except Exception: frm = "Scanviction <noreply@scanviction.com>"
    app_url = _APP_URL_CAPTURED
    if not app_url:
        try: app_url = st.secrets.get("APP_URL", "https://scanviction.com")
        except Exception: app_url = "https://scanviction.com"
    body = body_lines if isinstance(body_lines, str) else "<br>".join(body_lines)
    html = (f'<div style="font-family:Inter,Arial,sans-serif;background:#07090f;padding:32px;">'
            f'<div style="max-width:560px;margin:0 auto;background:#0d1525;border:1px solid rgba(99,102,241,.3);'
            f'border-radius:12px;padding:24px;">'
            f'<div style="font-size:18px;font-weight:800;color:#e2e8f0;margin-bottom:14px;">'
            f'Scan<span style="color:#f59e0b;">viction</span></div>'
            f'<div style="font-size:14px;color:#d1d9e6;line-height:1.7;">{body}</div>'
            f'<a href="{_esc(app_url)}" style="display:inline-block;margin-top:18px;padding:10px 18px;'
            f'background:#6366f1;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:13px;">'
            f'Open Scanviction &rarr;</a>'
            f'<div style="font-size:11px;color:#2a3a52;margin-top:16px;">Educational signals only. Not financial advice.</div>'
            f'</div></div>')
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": frm, "to": [to], "subject": subject, "html": html}, timeout=12)
        ok = resp.status_code in (200, 201)
        # Surface delivery outcomes in Admin → Data Health instead of failing silently.
        try: _record_health("email_alerts", ok, err=None if ok else f"HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception: pass
        return ok
    except Exception as e:
        try: _record_health("email_alerts", False, err=f"{type(e).__name__}: {str(e)[:120]}")
        except Exception: pass
        return False

def _deliver_user_alert(email, u, tkr, msg, price, pct, channels):
    """Deliver one triggered user price/RSI/volume alert. Email allowed for all tiers;
    Telegram is premium + connected + enabled."""
    prefs = u.get("notif_prefs") or {}
    if "email" in channels and prefs.get("email_enabled", True):
        _alert_email(email, f"⚡ Alert: {tkr}",
                     [f"<b>{_esc(msg)}</b>", f"Price: <b>${price:,.2f}</b> ({pct:+.2f}%)"])
    cid = u.get("telegram_chat_id", "")
    if ("telegram" in channels and cid and u.get("role", "free") in ("premium", "admin", "owner")
            and prefs.get("telegram_enabled", True) and _TG_TOKEN_CAPTURED):
        _tg_send_raw(_TG_TOKEN_CAPTURED, cid,
                     f"🔔 <b>Scanviction Alert</b>\n\n{_esc(msg)}\n\nPrice: <b>${price:,.2f}</b> ({pct:+.2f}%)")
    # Native Web Push for custom alerts (channels stored as "browser"/"push")
    if (_webpush is not None and ("browser" in channels or "push" in channels)
            and u.get("role", "free") in ("premium", "admin", "owner")
            and prefs.get("push_enabled", True) and u.get("push_subscriptions")):
        try:
            _webpush.send_to_user(u, f"⚡ Alert: {tkr}", f"{msg} — ${price:,.2f} ({pct:+.2f}%)")
        except Exception:
            pass

def _process_price_alerts():
    """Evaluate user price/%/volume/RSI alerts against the warm universe (per-ticker Polygon
    fallback for names outside it) and deliver inline. Runs each worker cycle so alerts fire
    on Streamlit Cloud WITHOUT the external cron. Leader-only; deduped per alert per hour."""
    try:
        if not _worker_is_leader():
            return
        import msp_store as _ms
        alerts_db = _ms.read_json(_ms.ALERTS_DB_PATH, {}) or {}
        if not alerts_db:
            return
        users = _ms.read_json(_ms.USERS_DB_PATH, {}) or {}
        fpath = _ms._data_path("msp_alert_fired.json", "ALERT_FIRED_PATH")
        fired = _ms.read_json(fpath, {}) or {}
        now = time.time()
        fired = {k: v for k, v in fired.items() if (now - (v or 0)) < 4 * 3600}
        warm = {}
        with _UNIVERSE_LOCK:
            for r in _UNIVERSE_CACHE.get("rows", []):
                if r.get("t"):
                    warm[r["t"]] = r
        hourslug = datetime.now().strftime("%Y-%m-%d-%H")
        changed = False
        for email, ualerts in alerts_db.items():
            u = users.get(email, {"role": "free"})
            for a in (ualerts or []):
                if not a.get("active", True):
                    continue
                tkr = (a.get("ticker", "") or "").upper()
                if not tkr:
                    continue
                fk = f"{email}:{a.get('id', tkr)}:{hourslug}"
                if fk in fired:
                    continue
                r = warm.get(tkr)
                if r:
                    q = r.get("q") or {}; f = r.get("factors") or {}
                    price = float(q.get("price") or 0); pct = float(q.get("pct") or 0)
                    vr = float(f.get("vol_ratio") or q.get("vol_ratio") or 0)
                    rsi = float(f.get("rsi") if f.get("rsi") is not None else 50)
                    have_tech = True
                else:
                    qq = _raw_quote(tkr) or {}
                    price = float(qq.get("price") or 0); pct = float(qq.get("pct") or 0)
                    vr = 0.0; rsi = 50.0; have_tech = False
                if not price:
                    continue
                atype = a.get("type", ""); thr = float(a.get("threshold", 0) or 0)
                trig = False; msg = ""
                if atype == "price_above" and price >= thr:
                    trig = True; msg = f"🎯 {tkr} hit ${price:,.2f} — above your ${thr:,.2f} target"
                elif atype == "price_below" and price <= thr:
                    trig = True; msg = f"⚠️ {tkr} at ${price:,.2f} — below your ${thr:,.2f} level"
                elif atype == "pct_change" and abs(pct) >= thr:
                    trig = True; msg = f"📈 {tkr} moved {'▲ up' if pct > 0 else '▼ down'} {abs(pct):.1f}% today"
                elif atype == "volume_spike" and have_tech and vr >= thr:
                    trig = True; msg = f"🔊 {tkr} volume is {vr:.1f}× average"
                elif atype == "rsi_oversold" and have_tech and rsi <= thr:
                    trig = True; msg = f"📉 {tkr} RSI {rsi:.0f} — oversold"
                elif atype == "rsi_overbought" and have_tech and rsi >= thr:
                    trig = True; msg = f"📈 {tkr} RSI {rsi:.0f} — overbought"
                if trig:
                    _deliver_user_alert(email, u, tkr, msg, price, pct, a.get("channels", ["email"]))
                    fired[fk] = now; changed = True
        if changed:
            _ms.write_json(fpath, fired)
    except Exception:
        pass

def _universe_worker():
    """Daemon loop: keep the warm universe fresh on the FAST cadence. This is
    the SINGLE source of universe builds — there's no separate 'kick' thread to
    get out of sync with. A force-refresh just sets a flag this loop reads.

    Records its own ok/fail to _DATA_HEALTH under the 'worker' key, so the
    System → Data Health panel can show whether the worker is alive and what
    its last error was (previously, a silent crash here made the panel stay
    empty forever, leaving you blind to the actual problem)."""
    global _WARM_IN_PROGRESS
    _record_health("worker", True, err="started")
    last_full = 0.0   # wall-clock of the last FULL re-score
    while True:
        cycle_start = time.time()
        # Liveness heartbeat: ensure_universe_worker uses this to tell a RUNNING
        # worker from a HUNG one (thread alive but stuck in a network call). It's
        # refreshed here and every second of the sleep loop below, so any gap
        # longer than _WORKER_STALL_SECS means the thread is genuinely stuck.
        with _UNIVERSE_LOCK:
            _UNIVERSE_CACHE["worker_beat"] = cycle_start
        # Decide this cycle: a FULL re-score is due on cold start, on a force-refresh, or every
        # re-score interval. DURING MARKET HOURS with intraday signals on, that interval drops to
        # INTRADAY_RESCORE and the re-score appends today's forming bar (see _intraday_bar) — so
        # category entries spread out through the session. Off-hours it's the slower UNIVERSE_REFRESH
        # and cycles just do the cheap snapshot price refresh.
        try: _mkt_now = market_status().get("state", "open")
        except Exception: _mkt_now = "open"
        _rescore_every = INTRADAY_RESCORE if (INTRADAY_SIGNALS and _mkt_now == "open") else UNIVERSE_REFRESH
        with _UNIVERSE_LOCK:
            _forced = _UNIVERSE_CACHE.get("force", False)
            _have_rows = bool(_UNIVERSE_CACHE.get("rows"))
        full_due = _forced or (not _have_rows) or (cycle_start - last_full >= _rescore_every)
        try:
            if full_due:
                with _UNIVERSE_LOCK:
                    _WARM_IN_PROGRESS = True
                _refresh_universe_now()
                last_full = time.time()
                _record_health("worker", True, int((time.time()-cycle_start)*1000))
            else:
                _refresh_prices_only()   # cheap: one snapshot call, prices updated in place
            # After prices are fresh, fire any user price/RSI/volume alerts inline (leader-only),
            # so alerts work on Streamlit Cloud without the external cron worker.
            try: _process_price_alerts()
            except Exception: pass
            # Keep tracked-signal outcomes (forward returns / win-loss labels) fresh
            # automatically — the retired cron used to do this; now it's inline + hourly.
            try: _refresh_signal_outcomes_inline()
            except Exception: pass
        except Exception as e:
            import traceback as _tb
            _record_health("worker", False, err=f"{type(e).__name__}: {str(e)[:120]}")
            # Also log to stderr so it shows in Streamlit Cloud logs.
            try: sys.stderr.write(f"[universe_worker] {_tb.format_exc()}\n")
            except Exception: pass
        finally:
            with _UNIVERSE_LOCK:
                _WARM_IN_PROGRESS = False
                _UNIVERSE_CACHE["force"] = False
        # ── Market-hour-aware sleep ──────────────────────────────────────
        # During market hours we wake every POLY_PRICE_REFRESH (a few minutes) to
        # push fresh prices; the heavier full re-score only fires every
        # UNIVERSE_REFRESH. Off-hours/weekends we back off hard (prices aren't
        # moving) to avoid pointless work.
        try:
            _state = market_status().get("state", "open")
        except Exception:
            _state = "open"
        if _state == "closed":  # weekend
            _sleep_for = UNIVERSE_REFRESH_WEEKEND or 86400
        elif _state in ("pre", "after"):
            _sleep_for = max(POLY_PRICE_REFRESH * 5, 900)
        else:
            _sleep_for = POLY_PRICE_REFRESH
        # Never sleep PAST the opening bell. Off-hours sleeps are long (up to 24h on
        # weekends), which could overshoot Monday's open by hours and made the first
        # scan of the day land whenever the sleep happened to expire. Cap the sleep at
        # seconds-until-open so the next cycle fires right at 9:30 ET and the day's
        # first re-score runs immediately at the open.
        if _state in ("closed", "pre", "after"):
            try:
                _ms = market_status()
                if _ms.get("target") == "opens":
                    _sleep_for = min(_sleep_for, max(int(_ms.get("seconds", 0)) + 5, 30))
            except Exception:
                pass
        # Sleep in short slices so a force-refresh is picked up quickly (and keep
        # the liveness heartbeat fresh — a long off-hours sleep must not read as a
        # stall to the watchdog in ensure_universe_worker).
        slept = 0
        while slept < _sleep_for:
            time.sleep(1)
            slept += 1
            with _UNIVERSE_LOCK:
                _UNIVERSE_CACHE["worker_beat"] = time.time()
                if _UNIVERSE_CACHE.get("force"):
                    break

# A worker heartbeat older than this = the thread is HUNG (stuck network call /
# deadlock), not just sleeping — the sleep loop refreshes the beat every second,
# and even the heaviest full warm finishes well inside this window.
_WORKER_STALL_SECS = int(_os.environ.get("MSP_WORKER_STALL_SECS", "1800"))

def ensure_universe_worker():
    """Start the background refresh thread once per PROCESS (survives reruns) —
    and REVIVE it if it's dead or hung. Called on every session script run, so
    any visit / self-kick / keep-awake ping doubles as a watchdog check."""
    if _os.environ.get("MSP_DISABLE_WORKER") == "1":
        return   # tests / tooling import app without spinning up the live scanner
    # Telegram getUpdates listener: ONE background consumer that answers /start instantly
    # (auto-links deep-link taps, replies with the chat id otherwise). Idempotent; no-op
    # without a bot token. Started here so it shares the worker's once-per-process lifecycle.
    try:
        import telegram_link as _tgl
        _tgl.start_listener(st.secrets.get("TELEGRAM_BOT_TOKEN", "") or "")
    except Exception:
        pass
    with _WORKER_LOCK:
        alive = any(_th.name.startswith("msp-universe") for _th in _threading.enumerate())
        if alive:
            # Thread exists — but is it actually MAKING PROGRESS? A hung thread
            # (stuck in a no-timeout network call) can't be killed, but we can
            # start a replacement so scanning and alert delivery resume. The old
            # name-only check meant a hung worker silently stopped all scans and
            # notifications until the next full restart.
            with _UNIVERSE_LOCK:
                beat = _UNIVERSE_CACHE.get("worker_beat", 0) or 0
            if not beat or (time.time() - beat) <= _WORKER_STALL_SECS:
                return
            with _UNIVERSE_LOCK:   # reset so each session doesn't spawn another
                _UNIVERSE_CACHE["worker_beat"] = time.time()
            _record_health("worker", False, err=f"stalled >{_WORKER_STALL_SECS}s — starting replacement thread")
            try: sys.stderr.write("[msp] universe worker STALLED — starting replacement thread\n")
            except Exception: pass
        th = _threading.Thread(target=_universe_worker,
                               name=f"msp-universe-{int(time.time())}", daemon=True)
        th.start()
    try: sys.stderr.write("[msp] universe worker thread started\n")
    except Exception: pass

def build_scored_universe():
    """Return the warm, fully-scored universe (instant — served from cache)."""
    ensure_universe_worker()
    with _UNIVERSE_LOCK:
        return list(_UNIVERSE_CACHE["rows"])

_WARM_KICK_STARTED = False
_WARM_IN_PROGRESS = False
def _kick_background_warm(force=False):
    """Ensure the worker is running and, if force=True, ask it to rebuild now.
    The worker is the only builder, so this just flips flags it watches —
    eliminating the dual-builder flag conflicts that caused the stuck spinner."""
    ensure_universe_worker()
    if force:
        with _UNIVERSE_LOCK:
            _UNIVERSE_CACHE["attempted"] = False
            _UNIVERSE_CACHE["force"] = True

def universe_is_warming():
    """True only while the FIRST warm attempt hasn't completed yet. Once an
    attempt has run (success OR failure), this returns False so the UI never
    loops forever — if the attempt produced no data, the caller shows a clear
    'no data / retry' state instead of spinning."""
    with _UNIVERSE_LOCK:
        if _UNIVERSE_CACHE["rows"]:
            return False
        return not _UNIVERSE_CACHE.get("attempted", False)

def universe_warm_failed():
    """True if a warm attempt completed but produced no scored rows (data
    provider unreachable / rate-limited). Lets the UI distinguish 'still
    warming' from 'tried and got nothing'."""
    with _UNIVERSE_LOCK:
        return _UNIVERSE_CACHE.get("attempted", False) and not _UNIVERSE_CACHE["rows"]

@st.cache_data(ttl=300, show_spinner=False)
def build_scored_universe_legacy():
    """Kept for reference: the pre-threaded cached builder."""
    hot = st_hot()
    universe = list(dict.fromkeys(BROAD_UNI + hot[:8]))[:32]
    rows = []
    for t in universe:
        r = score_ticker(t)
        if r:
            r = dict(r); r["hot"] = t in hot
            rows.append(r)
    return rows

def _refresh_stale_quotes_batched(tickers, max_age_seconds=300):
    """Batched TD fetch for any of these tickers whose cached quote is older
    than max_age_seconds. One HTTP call regardless of ticker count (but still
    1 TD credit per symbol — see https://support.twelvedata.com/en/articles/5203360).

    This is the 'fresh data on user interaction' path: when a user opens a
    category, we top up the prices that are stale (default >5 min old). Names
    already fresh are skipped, so common navigation costs ~0 credits."""
    if not tickers:
        return 0
    now = time.time()
    stale = []
    with _DATA_LOCK:
        for t in tickers:
            cached = _DATA_CACHE.get(t, {}).get("quote")
            if not cached:
                stale.append(t); continue
            _val, fetched_at = cached
            if (now - fetched_at) > max_age_seconds:
                stale.append(t)
    if not stale:
        return 0
    # Get TD key
    td_key = ""
    try: td_key = st.secrets.get("TWELVE_DATA_API_KEY", "") or ""
    except Exception: pass
    if not td_key:
        td_key = _os.environ.get("TWELVE_DATA_API_KEY", "") or ""
    if not td_key:
        return 0
    # Cap batch size at TD's documented 120 symbols/call
    stale = stale[:120]
    # Pre-flight credit check — refuse to fire if we can't cover the whole batch
    if not _td_usage_check_and_increment(len(stale)):
        return 0
    try:
        url = f"https://api.twelvedata.com/quote?symbol={','.join(stale)}&apikey={td_key}"
        r = requests.get(url, timeout=8, headers={"User-Agent":"Scanviction/1.0"})
        _td_sync_from_headers(r.headers)
        if r.status_code != 200:
            _record_health("twelvedata", False, err=f"batch HTTP {r.status_code}")
            return 0
        data = r.json()
        # Single-symbol response is a dict; multi is {SYM: {...}}
        if isinstance(data, dict) and "symbol" in data and "close" in data:
            data = {data["symbol"]: data}
        if not isinstance(data, dict):
            return 0
        updated = 0
        for sym, d in data.items():
            if not isinstance(d, dict) or "close" not in d: continue
            try:
                p = float(d["close"]); pv = float(d.get("previous_close", p) or p)
                quote = {
                    "price": round(p, 2), "prev": round(pv, 2),
                    "pct": float(d.get("percent_change", 0) or 0),
                    "name": d.get("name", sym),
                    "open": float(d.get("open", p) or p),
                    "high": float(d.get("high", p) or p),
                    "low": float(d.get("low", p) or p),
                    "volume": int(float(d.get("volume", 0) or 0)),
                }
                with _DATA_LOCK:
                    _DATA_CACHE.setdefault(sym, {})["quote"] = (quote, now)
                # Also patch the universe cache row so the next render shows it
                with _UNIVERSE_LOCK:
                    for row in _UNIVERSE_CACHE.get("rows", []):
                        if row.get("t") == sym:
                            row["q"] = quote
                            break
                updated += 1
            except Exception:
                continue
        _record_health("twelvedata", True, err=f"batch {len(stale)}→{updated} updated")
        return updated
    except Exception as e:
        _record_health("twelvedata", False, err=f"batch: {e}")
        return 0

def _composite_filter(cat_name, row, hot):
    """Pure category membership test. Given a precomputed `row`, returns
    (include: bool, comp: float, why: str) for the named composite category.
    No network or scoring — just arithmetic over already-computed signals."""
    t = row["t"]; sc = row["sc"]; bd = row["bd"]; sent = row["sent"]; info = row["info"]; df = row.get("df")
    in_hot = row.get("hot", t in hot)
    sf = (info.get("sf", 0) or 0) * 100
    bull = sent.get("bull", 50)
    include = False; comp = sc; why = "Scanviction composite signal"
    if cat_name == "🔥💥 Squeeze + Buzz":
        comp = sf*1.5 + (30 if in_hot else 0) + (bull-50)*0.4 + bd.get("Volume",0)
        include = sf >= 8 and (in_hot or bull >= 60)
        why = f"Short float {sf:.0f}% + {'🔥 trending' if in_hot else f'{bull}% bullish'}"
    elif cat_name == "⚡📈 Volume Breakout":
        vs = bd.get("Volume",0); ts = bd.get("Trend",0)
        comp = vs*2 + ts + bd.get("MACD",0); include = vs >= 7 and ts >= 12
        why = "Volume surge + breaking above key averages"
    elif cat_name == "🎯 Smart Reversal":
        ms = bd.get("Momentum",0); ms2 = bd.get("MACD",0)
        comp = ms + ms2 + (bull-50)*0.3; include = ms >= 20 and ms2 >= 9
        why = "RSI oversold + MACD turning positive = bounce setup"
    elif cat_name == "💡 Hidden Movers":
        wl = sent.get("wl",0)
        comp = sc - (30 if in_hot else 0) - min(wl/100,15)
        include = sc >= 45 and not in_hot and bull < 65
        why = f"Score {sc} with low social attention — early discovery"
    elif cat_name == "🌊 Momentum Leaders":
        comp = bd.get("Momentum",0) + bd.get("Trend",0) + bd.get("MACD",0) + bull*0.08
        include = (bd.get("Momentum",0) >= 12 and bd.get("Trend",0) >= 16 and bd.get("MACD",0) >= 9)
        why = "RSI + trend + MACD all bullish simultaneously"
    elif cat_name == "🎭 Social Catalyst":
        vs = bd.get("Volume",0); msgs = sent.get("msgs",0)
        comp = vs*1.5 + (50 if in_hot else 0) + bull*0.3 + min(msgs*2,30)
        include = (in_hot or msgs >= 5) and vs >= 4
        why = f"{'🔥 StockTwits trending' if in_hot else f'{msgs} posts'} + volume surge"
    elif cat_name == "🌡️ Sentiment Flip":
        comp = bull*0.8 + bd.get("Momentum",0)*0.5 + bd.get("Volume",0)*0.3
        include = bull >= 62 and bd.get("Momentum",0) >= 10
        why = f"Bullish sentiment at {bull}% — sentiment sharply reversing"
    elif cat_name == "📉→📈 Fallen Angels":
        mom = bd.get("Momentum",0)
        comp = mom*2 + bd.get("Sentiment",0) + bd.get("Volume",0)
        include = mom >= 20
        why = "Deep pullback + RSI oversold = recovery candidate forming"
    elif cat_name == "🔬 Micro-Cap Movers":
        mc = info.get("mktcap",0) or 0; vs = bd.get("Volume",0)
        comp = vs*2 + bd.get("Momentum",0) + (20 if mc < 500e6 else 10 if mc < 2e9 else 0)
        include = mc < 2e9 and vs >= 4 and bd.get("Momentum",0) >= 8
        mc_s = fmt_mktcap(mc)
        why = f"Micro/small-cap ({mc_s}) + volume surge = early move potential"
    elif cat_name == "💎 Value Momentum":
        pe = info.get("pe",None); tr_s = bd.get("Trend",0)
        comp = (15 if pe and 5 < pe < 20 else 5) + tr_s + bd.get("Momentum",0)
        include = tr_s >= 10 and bd.get("Momentum",0) >= 10 and (pe is None or pe < 25)
        why = f"Low P/E ({pe:.1f}×)" if pe else "Value setup + rising momentum"
    elif cat_name == "🏆 Relative Strength":
        comp = bd.get("Trend",0)*1.5 + bd.get("Momentum",0) + bd.get("MACD",0)
        include = bd.get("Trend",0) >= 16 and bd.get("Momentum",0) >= 12 and sc >= 55
        why = f"Score {sc} — outperforming on trend, momentum, and MACD"
    elif cat_name == "🎪 Earnings Catalyst":
        vs = bd.get("Volume",0); msgs = sent.get("msgs",0)
        comp = vs*2 + (50 if in_hot else 0) + bull*0.3 + min(msgs*3,40)
        include = vs >= 11 and (in_hot or bull >= 65)
        why = "High volume + social spike = likely catalyst in play"
    elif cat_name == "🔁 Mean Reversion":
        sq = bd.get("Squeeze",0); mom = bd.get("Momentum",0)
        comp = mom + sq*2 + bd.get("Sentiment",0)
        include = mom >= 18 and (sq >= 2 or sf >= 10)
        why = f"Oversold + {sf:.0f}% short float = compression before expansion"
    elif cat_name == "⚡🧲 Smart Money Signal":
        vs = bd.get("Volume",0); mac = bd.get("MACD",0); tr_s = bd.get("Trend",0)
        comp = vs*2 + mac*1.5 + tr_s; include = vs >= 11 and mac >= 9 and tr_s >= 12
        why = "3×+ volume + MACD bullish + above MAs = institutional accumulation"
    elif cat_name == "🌪️ Volatility Squeeze":
        mom = bd.get("Momentum",0); vs = bd.get("Volume",0); sq = bd.get("Squeeze",0)
        bb_low = False
        try:
            if df is not None:
                bb = ta.volatility.BollingerBands(df["close"].copy())
                bb_low = bb.bollinger_wband().iloc[-1] < bb.bollinger_wband().rolling(90).mean().iloc[-1]*0.7
        except Exception:
            bb_low = False
        comp = (30 if bb_low else 0) + vs + mom + sq*2; include = vs >= 4 and (bb_low or sq >= 2)
        why = "Bollinger compressing + volume building = coiled spring"
    elif cat_name == "🎯📊 Triple Lock":
        mom = bd.get("Momentum",0); mac = bd.get("MACD",0); tr_s = bd.get("Trend",0); vs = bd.get("Volume",0)
        comp = mom + mac + tr_s + vs; include = (mom >= 12 and mac >= 9 and tr_s >= 16 and vs >= 4)
        why = "RSI + MACD + 50d trend + volume ALL bullish = maximum conviction"
    elif cat_name == "🦈 Sustained Strength":
        vs = bd.get("Volume",0); tr_s = bd.get("Trend",0); mac = bd.get("MACD",0)
        comp = vs*1.5 + tr_s + mac + bd.get("Sentiment",0)*0.5
        include = tr_s >= 16 and vs >= 7 and mac >= 9
        why = "Multi-session above-avg volume + holding MAs = institutional interest"
    else:
        include = True; comp = sc; why = "Scanviction scoring engine"
    return include, comp, why

def get_composite_stocks(cat_name, limit=10):
    """Stocks whose PRIMARY (single best-fit) category is cat_name. Assignment runs
    once per warm (assign_categories), so this is a pure in-memory filter and the
    categories never overlap. Computes a row's primary on the fly only if it
    predates assignment (legacy/edge)."""
    universe = build_scored_universe()
    results = []
    for row in universe:
        pc = row.get("primary_cat", "__unset__")
        if pc == "__unset__":   # row never went through assign_categories
            feat = _feat_from_row(row)
            cat, fit = category_for_feat(feat)
            row = dict(row); row["primary_cat"] = cat; row["comp"] = fit
            row["why"] = _category_why(cat, feat) if cat else ""
            pc = cat
        if pc == cat_name:
            results.append(dict(row))
    results.sort(key=lambda x: x["comp"], reverse=True)
    return results[:limit]

def get_standard_stocks(cat_name, limit=10):
    """Fast: filter the precomputed warm universe to a standard category's
    tickers. Since the universe now pre-scores ALL category tickers, this is a
    pure in-memory lookup — no live scoring on click. Falls back to live
    scoring only for tickers not yet in the warm set (rare/edge)."""
    universe = build_scored_universe()
    with _UNIVERSE_LOCK:
        hot = list(_UNIVERSE_CACHE.get("hot", []))
    by_t = {r["t"]: r for r in universe}
    if cat_name == "🔥 Trending Now":
        tickers = hot or [r["t"] for r in universe if r.get("hot")]
    else:
        tickers = list(CATEGORIES.get(cat_name, []))
    if not tickers:
        return []
    rows = []
    for t in tickers[:max(limit*2, limit)]:
        r = by_t.get(t)
        if r is None:
            # Not in the warm set yet — score it directly (cached). Only happens
            # for tickers outside the pre-scored union, which should be none now.
            r = score_ticker(t)
            if r:
                r = dict(r); r["hot"] = t in hot
        if r:
            r = dict(r); r["comp"] = r.get("sc", 0); r["why"] = ""
            rows.append(r)
    rows.sort(key=lambda x: x.get("sc", 0), reverse=True)
    return rows[:limit]

def _get_composite_stocks_LEGACY(cat_name,limit=10):
    hot=st_hot(); universe=list(set(BROAD_UNI+hot[:8]))[:32]
    results=[]
    prog_container=st.empty()
    for i,t in enumerate(universe[:limit*3]):
        pct=(i+1)/(limit*3)
        prog_container.markdown(f'''<div style="background:#0d1525;border:1px solid rgba(99,102,241,0.2);border-radius:10px;padding:12px 16px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                <span style="font-size:12px;font-weight:600;color:#818cf8;">⚡ {cat_name}</span>
                <span style="font-size:11px;color:#374f6e;">{int(pct*100)}% · Scanning {t}…</span>
            </div>
            <div style="background:rgba(255,255,255,0.06);border-radius:4px;height:4px;">
                <div style="background:linear-gradient(90deg,#4f46e5,#6366f1);width:{int(pct*100)}%;height:4px;border-radius:4px;"></div>
            </div>
        </div>''', unsafe_allow_html=True)
        try:
            q=get_quote(t); df=yf_ohlcv(t,60); info=yf_fund(t); sent=st_sent(t)
            sc,bd,op,risk,conf=compute_scores(df,info,sent); ig=get_insights(df,info)
            if not q: continue
            sf=(info.get("sf",0) or 0)*100; bull=sent.get("bull",50); in_hot=t in hot
            include=False; comp=sc; why="Scanviction composite signal"
            if cat_name=="🔥💥 Squeeze + Buzz":
                comp=sf*1.5+(30 if in_hot else 0)+(bull-50)*0.4+bd.get("Volume",0)
                include=sf>=8 and (in_hot or bull>=60)
                why=f"Short float {sf:.0f}% + {'🔥 trending' if in_hot else f'{bull}% bullish'}"
            elif cat_name=="⚡📈 Volume Breakout":
                vs=bd.get("Volume",0); ts=bd.get("Trend",0)
                comp=vs*2+ts+bd.get("MACD",0); include=vs>=7 and ts>=12
                why="Volume surge + breaking above key averages"
            elif cat_name=="🎯 Smart Reversal":
                ms=bd.get("Momentum",0); ms2=bd.get("MACD",0)
                comp=ms+ms2+(bull-50)*0.3; include=ms>=20 and ms2>=9
                why="RSI oversold + MACD turning positive = bounce setup"
            elif cat_name=="💡 Hidden Movers":
                wl=sent.get("wl",0)
                comp=sc-(30 if in_hot else 0)-min(wl/100,15)
                include=sc>=45 and not in_hot and bull<65
                why=f"Score {sc} with low social attention — early discovery"
            elif cat_name=="🌊 Momentum Leaders":
                comp=bd.get("Momentum",0)+bd.get("Trend",0)+bd.get("MACD",0)+bull*0.08
                include=(bd.get("Momentum",0)>=12 and bd.get("Trend",0)>=16 and bd.get("MACD",0)>=9)
                why="RSI + trend + MACD all bullish simultaneously"
            elif cat_name=="🎭 Social Catalyst":
                vs=bd.get("Volume",0); msgs=sent.get("msgs",0)
                comp=vs*1.5+(50 if in_hot else 0)+bull*0.3+min(msgs*2,30)
                include=(in_hot or msgs>=5) and vs>=4
                why=f"{'🔥 StockTwits trending' if in_hot else f'{msgs} posts'} + volume surge"
            elif cat_name=="🌡️ Sentiment Flip":
                comp=bull*0.8+bd.get("Momentum",0)*0.5+bd.get("Volume",0)*0.3
                include=bull>=62 and bd.get("Momentum",0)>=10
                why=f"Bullish sentiment at {bull}% — sentiment sharply reversing"
            elif cat_name=="📉→📈 Fallen Angels":
                mom=bd.get("Momentum",0)
                comp=mom*2+bd.get("Sentiment",0)+bd.get("Volume",0)
                include=mom>=20
                why="Deep pullback + RSI oversold = recovery candidate forming"
            elif cat_name=="🔬 Micro-Cap Movers":
                mc=info.get("mktcap",0) or 0; vs=bd.get("Volume",0)
                comp=vs*2+bd.get("Momentum",0)+(20 if mc<500e6 else 10 if mc<2e9 else 0)
                include=mc<2e9 and vs>=4 and bd.get("Momentum",0)>=8
                mc_s=fmt_mktcap(mc)
                why=f"Micro/small-cap ({mc_s}) + volume surge = early move potential"
            elif cat_name=="💎 Value Momentum":
                pe=info.get("pe",None); tr_s=bd.get("Trend",0)
                comp=(15 if pe and 5<pe<20 else 5)+tr_s+bd.get("Momentum",0)
                include=tr_s>=10 and bd.get("Momentum",0)>=10 and (pe is None or pe<25)
                why=f"Low P/E ({pe:.1f}×)" if pe else "Value setup + rising momentum"
            elif cat_name=="🏆 Relative Strength":
                comp=bd.get("Trend",0)*1.5+bd.get("Momentum",0)+bd.get("MACD",0)
                include=bd.get("Trend",0)>=16 and bd.get("Momentum",0)>=12 and sc>=55
                why=f"Score {sc} — outperforming on trend, momentum, and MACD"
            elif cat_name=="🎪 Earnings Catalyst":
                vs=bd.get("Volume",0); msgs=sent.get("msgs",0)
                comp=vs*2+(50 if in_hot else 0)+bull*0.3+min(msgs*3,40)
                include=vs>=11 and (in_hot or bull>=65)
                why=f"High volume + social spike = likely catalyst in play"
            elif cat_name=="🔁 Mean Reversion":
                sq=bd.get("Squeeze",0); mom=bd.get("Momentum",0)
                comp=mom+sq*2+bd.get("Sentiment",0)
                include=mom>=18 and (sq>=2 or sf>=10)
                why=f"Oversold + {sf:.0f}% short float = compression before expansion"
            elif cat_name=="⚡🧲 Smart Money Signal":
                vs=bd.get("Volume",0); mac=bd.get("MACD",0); tr_s=bd.get("Trend",0)
                comp=vs*2+mac*1.5+tr_s; include=vs>=11 and mac>=9 and tr_s>=12
                why="3×+ volume + MACD bullish + above MAs = institutional accumulation"
            elif cat_name=="🌪️ Volatility Squeeze":
                mom=bd.get("Momentum",0); vs=bd.get("Volume",0); sq=bd.get("Squeeze",0)
                try:
                    bb=ta.volatility.BollingerBands(df["close"].copy())
                    bb_low=bb.bollinger_wband().iloc[-1]<bb.bollinger_wband().rolling(90).mean().iloc[-1]*0.7
                except: bb_low=False
                comp=(30 if bb_low else 0)+vs+mom+sq*2; include=vs>=4 and (bb_low or sq>=2)
                why="Bollinger compressing + volume building = coiled spring"
            elif cat_name=="🎯📊 Triple Lock":
                mom=bd.get("Momentum",0); mac=bd.get("MACD",0); tr_s=bd.get("Trend",0); vs=bd.get("Volume",0)
                comp=mom+mac+tr_s+vs; include=(mom>=12 and mac>=9 and tr_s>=16 and vs>=4)
                why="RSI + MACD + 50d trend + volume ALL bullish = maximum conviction"
            elif cat_name=="🦈 Sustained Strength":
                vs=bd.get("Volume",0); tr_s=bd.get("Trend",0); mac=bd.get("MACD",0)
                comp=vs*1.5+tr_s+mac+bd.get("Sentiment",0)*0.5
                include=tr_s>=16 and vs>=7 and mac>=9
                why="Multi-session above-avg volume + holding MAs = institutional interest"
            else:
                include=True; comp=sc; why="Scanviction scoring engine"
            if include:
                results.append({"t":t,"q":q,"sc":sc,"bd":bd,"ig":ig,"op":op,"risk":risk,"conf":conf,
                                 "hot":in_hot,"df":df,"info":info,"sent":sent,"comp":comp,"why":why})
        except: continue
    prog_container.empty()
    results.sort(key=lambda x:x["comp"],reverse=True)
    return results[:limit]

# ─────────────────────────────────────────────────────────────
# SHARED UI COMPONENTS
# ─────────────────────────────────────────────────────────────
def gold_btn(label, key, help_text=None):
    """Render a gold premium upgrade button."""
    st.markdown('<div class="gold-btn">', unsafe_allow_html=True)
    clicked = st.button(f"👑 {label}", key=key, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
    return clicked

def render_sr(s, cat_key="", show_why=False, cat_name="", snap=None):
    t=s["t"]; q=s["q"]; sc=s["sc"]; ig=s["ig"]
    info=s.get("info",{}); sent=s.get("sent",{}); hot=s.get("hot",False)
    bd=s.get("bd",{}); op=s.get("op",""); risk=s.get("risk",""); why_str=s.get("why","")
    if not q: return
    pct=q.get("pct",0); price=q.get("price",0)
    cc=GREEN if pct>0 else RED if pct<0 else "#6b7fa0"; ar="▲" if pct>0 else "▼" if pct<0 else "•"
    rc=risk_color(risk)
    hot_b='<span class="b b-hot">🔥 HOT</span>' if hot else ""
    sigs="".join([f'<span class="b b-{"bull" if sv=="bull" else "bear" if sv=="bear" else "neu"}">{lv[:16]}</span>' for lv,_,sv,_ in ig[:2]])
    rec_lbl,rec_clr,rec_txt=get_recommendation(sc,bd,info)
    display_why=why_str if (show_why and why_str) else rec_txt

    # ── Performance since signal (snapshot is passed in by the caller, which
    #    loads the store once for the whole category — avoids a per-card read) ──
    perf_html = ""
    if snap is None and cat_name:
        snap = get_recommendation_snapshot(cat_name, t)
    if snap:
        entry = snap.get("entry_price", 0)
        disp, is_short = _since_signal_pct(cat_name or s.get("primary_cat", ""), entry, price)
        ts = snap.get("triggered_at", 0)
        stamp = _signal_stamp(ts)
        age = _humanize_age(ts)
        when = f"{stamp} ({age})" if stamp else f"Signal {age}"
        if disp is not None:
            pcol = GREEN if disp >= 0 else RED
            sign = "+" if disp >= 0 else ""
            lbl = "% since (short)" if is_short else "% since"
            perf_html = (
                '<div style="display:flex;gap:14px;align-items:center;margin-top:8px;'
                'padding-top:8px;border-top:1px solid rgba(255,255,255,0.05);flex-wrap:wrap;">'
                f'<div style="font-size:10px;color:#4a5e7a;">Signaled {when} @ ${entry:,.2f}</div>'
                f'<div style="font-size:11px;font-weight:700;color:{pcol};font-family:\'JetBrains Mono\',monospace;">{sign}{disp:.2f}{lbl}</div>'
                '</div>'
            )

    col_main,col_btn=st.columns([6,1.2],gap="small")
    with col_main:
        st.markdown(f"""<div class="sr">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                <div style="flex:1;min-width:0;">
                    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px;">
                        <span class="sr-tick">{t}</span>{hot_b}
                        <span style="display:inline-block;padding:3px 10px;border-radius:5px;
                            font-size:11px;font-weight:800;background:{rec_clr}22;
                            color:{rec_clr};border:1px solid {rec_clr}44;">{rec_lbl}</span>
                    </div>
                    <div class="sr-name">{q.get('name','')[:32]}</div>
                    <div class="sr-why">→ {display_why[:80]}{"…" if len(display_why)>80 else ""}</div>
                    <div style="margin-top:5px;">{sigs}</div>
                </div>
                <div style="text-align:right;min-width:110px;flex-shrink:0;padding-left:12px;">
                    <div class="sr-price">${price:,.2f}</div>
                    <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:{cc};">{ar}{abs(pct):.2f}%</div>
                    <div style="display:flex;align-items:center;gap:5px;justify-content:flex-end;margin-top:4px;">
                        <span style="font-size:10px;color:{rc};">⚡{risk}</span>
                        {sc_pill(sc)}
                    </div>
                </div>
            </div>{perf_html}
        </div>""", unsafe_allow_html=True)
    with col_btn:
        wl=st.session_state.get("watchlist",[]) or []
        in_wl=t in wl
        st.markdown('<div style="height:2px;"></div>', unsafe_allow_html=True)
        # Open the full detail page + multi-factor scorecard. We pass the already-
        # scored row so detail reuses its price-history df and lazily enriches the
        # fundamentals/sentiment on arrival (see page_detail).
        if st.button("📊 Details", key=f"view_{t}_{cat_key}", use_container_width=True, type="primary"):
            st.session_state.detail_ticker = t
            st.session_state.detail_data = dict(s)
            nav("stock_detail")
        if st.button("✅ Saved" if in_wl else "➕ Watch",key=f"wl_{t}_{cat_key}",use_container_width=True,type="secondary"):
            _toggle_watchlist(t)
            st.rerun()
        # ── User-on-demand fresh quote (real-time on interaction) ──
        # The background warm uses cached prices to stay within the TD daily
        # budget, so when a user wants the LATEST number for a specific ticker
        # they click here — costs 1 credit, returns fresh data, updates inline.
        if st.button("↻ Refresh", key=f"rt_{t}_{cat_key}", use_container_width=True,
                     help="Fetch the very latest price for this ticker right now"):
            with st.spinner(f"Fetching live {t}…"):
                fetch_realtime_ticker(t)
            st.rerun()

def render_cat(cat,limit=10,show_why=False):
    is_comp=cat in COMPOSITE_CATS
    if is_comp:
        _,tier=COMPOSITE_CATS[cat]
        if tier=="premium" and not is_premium(): render_lock(cat); return
        stocks=get_composite_stocks(cat,limit)
    else:
        stocks=get_standard_stocks(cat,limit)
    # ── On-demand real-time refresh on category open ──
    # When the user just switched to this category, batch-refresh any stale
    # prices. Names already fresh (<5 min) cost 0 credits, so navigating back
    # to the same category is free.
    if stocks:
        _last_cat = st.session_state.get("_last_refreshed_cat", "")
        if _last_cat != cat:
            st.session_state["_last_refreshed_cat"] = cat
            try:
                _n_refreshed = _refresh_stale_quotes_batched([s.get("t") for s in stocks if s.get("t")], max_age_seconds=600)
                # Re-pull ONLY when something was actually refreshed — with the worker
                # keeping quotes fresh this is usually 0, and skipping the second
                # scoring pass shaves the category-open time.
                if _n_refreshed:
                    if is_comp:
                        stocks=get_composite_stocks(cat,limit)
                    else:
                        stocks=get_standard_stocks(cat,limit)
            except Exception:
                pass
    if not stocks:
        # Three distinct states, driven by the SINGLE worker's flags:
        #  (1) still warming  → worker hasn't finished its first attempt; poll
        #  (2) warm failed    → attempt done, no data (provider down/limited)
        #  (3) no match       → universe is warm, this category just has 0 hits
        _in_progress = False
        try: _in_progress = _WARM_IN_PROGRESS
        except Exception: pass
        if universe_is_warming() or _in_progress:
            # Show a STATIC preparing message. We deliberately do NOT auto-rerun
            # here. The old time.sleep()+st.rerun() poll caused the whole page to
            # blank and repopulate every 1.5s ("popping in and out"), and on an
            # exhausted data tier (where warming never completes) it looped
            # forever. The background worker scores the universe on its own; the
            # user can tap "Check for data" or just reopen the category once it's
            # ready. Stable > flickering.
            st.markdown('''<div style="background:#0d1525;border:1px solid rgba(99,102,241,0.25);
                               border-radius:12px;padding:30px;text-align:center;">
                <div style="display:flex;align-items:center;justify-content:center;gap:12px;">
                  <div style="width:18px;height:18px;border:2px solid rgba(99,102,241,0.25);border-top-color:#818cf8;border-radius:50%;animation:spin 0.8s linear infinite;"></div>
                  <div style="text-align:left;">
                    <div style="font-size:15px;font-weight:700;color:#e2e8f0;">Preparing live market data…</div>
                    <div style="font-size:12px;color:#374f6e;">Scoring the universe in the background — this runs once.</div>
                  </div>
                </div></div><style>@keyframes spin{to{transform:rotate(360deg);}}</style>''', unsafe_allow_html=True)
            if st.button("↻ Check for data", key=f"warm_check_{cat[:8]}", use_container_width=True):
                st.session_state["_warm_attempts"] = 0
                try: st.rerun(scope="fragment")
                except Exception:
                    try: st.rerun()
                    except Exception: pass
            # Auto-advance: poll on a BOUNDED schedule so the card flips to live
            # data on its own the moment the background warm finishes — the user
            # shouldn't have to click. We rerun ONLY this fragment (scope="fragment"),
            # so the page chrome stays put (no full-page blank/flicker — the reason
            # the old time.sleep()+st.rerun() poll was removed). Bounded by an
            # attempt budget so a stuck/empty data tier can't loop forever; after
            # the budget we fall back to the manual button above.
            _att = st.session_state.get("_warm_attempts", 0)
            if _att < 60:  # ~60 × 2s ≈ 2 min, ample for a cold market-wide warm
                st.session_state["_warm_attempts"] = _att + 1
                time.sleep(2)
                try: st.rerun(scope="fragment")
                except Exception:
                    try: st.rerun()
                    except Exception: pass
            return
        st.session_state["_warm_attempts"] = 0
        if universe_warm_failed():
            with _UNIVERSE_LOCK:
                _scanned = _UNIVERSE_CACHE.get("scanned", 0); _err = _UNIVERSE_CACHE.get("last_error","")
            st.markdown(f'''<div style="background:#0d1525;border:1px solid rgba(251,191,36,0.3);
                               border-radius:12px;padding:30px;text-align:center;">
                <div style="font-size:24px;margin-bottom:10px;">⏳</div>
                <div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">Couldn't load market data</div>
                <div style="font-size:13px;color:#6b7fa0;max-width:460px;margin:0 auto;">The data provider (Yahoo Finance) returned nothing for any ticker — it may be rate-limiting or temporarily down. This is a data-source issue, not a bug. Try again in a moment.</div>
                {f'<div style="font-size:10px;color:#374f6e;margin-top:8px;">scanned {_scanned} tickers · {_err}</div>' if _err else ''}
            </div>''', unsafe_allow_html=True)
            if st.button("🔄 Retry now", key=f"warm_retry_{cat[:8]}", use_container_width=True):
                try: _kick_background_warm(force=True)
                except Exception: pass
                st.rerun()
            return
        # Warm succeeded, but this specific category has no current matches.
        st.markdown('''<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);
                           border-radius:12px;padding:32px;text-align:center;">
            <div style="font-size:24px;margin-bottom:10px;">🔍</div>
            <div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">No stocks matching right now</div>
            <div style="font-size:13px;color:#374f6e;">Market conditions may not meet this category's criteria at the moment. Check back later.</div>
        </div>''', unsafe_allow_html=True)
        return
    # Universe is warm and this category has matches — clear any warm counter.
    st.session_state["_warm_attempts"] = 0
    # ── Persist snapshots for surfaced signals in ONE batched write ──
    # (Previously this looped record_recommendation() per stock = ~10 store
    # round-trips per click. Batching makes a click cost a single write.)
    try:
        record_recommendations_bulk(cat, [
            (s.get("t",""), (s.get("q") or {}).get("price", 0),
             s.get("sc"), s.get("op"), s.get("why")) for s in stocks
        ])
    except Exception:
        pass
    # Load the snapshot store ONCE and pass each card its snapshot, so render_sr
    # doesn't re-read the whole store per stock.
    try:
        _all_snaps = _load_recs()
    except Exception:
        _all_snaps = {}
    for s in stocks:
        _snap = _all_snaps.get(_rec_key(cat, s.get("t","")))
        render_sr(s,cat.replace(" ","_").replace("+","p").replace("→","r"),show_why=is_comp,cat_name=cat,snap=_snap)

    # ── Auto-record signal events for composite categories ──
    if is_comp and HAS_SIGNAL_ENGINE:
        try:
            # Get current market regime for context
            try:
                secs_ctx = get_sectors() or {}
                movers_ctx = get_bi_movers() or []
                avg_pct_ctx = sum(m.get("pct",0) for m in movers_ctx)/max(1,len(movers_ctx))
                squeeze_ct = sum(1 for s in stocks if (s.get("info",{}).get("sf",0) or 0)*100 >= 15)
                regime_info = detect_market_regime(secs_ctx, avg_pct_ctx, squeeze_ct)
                current_regime = regime_info.get("regime", "mixed")
            except Exception:
                current_regime = "mixed"

            # Record each stock as a signal event
            for s in stocks[:5]:  # top 5 only to limit storage
                try:
                    ticker_s = s.get("t","")
                    score_s = s.get("sc",0)
                    if not ticker_s or score_s < 50: continue  # Only record meaningful signals
                    q_s = s.get("q") or {}
                    price_s = q_s.get("price",0)
                    if price_s <= 0: continue
                    bd_s = s.get("bd",{})
                    info_s = s.get("info",{})
                    sent_s = s.get("sent",{})
                    df_s = s.get("df")
                    rec_s = s.get("op","WATCH")
                    conf_data = compute_confidence(bd_s, info_s, sent_s, df_s) if df_s is not None else {}
                    record_signal_event(
                        ticker=ticker_s, category=cat, score=score_s,
                        score_components=bd_s, price=price_s,
                        info=info_s, sent=sent_s, recommendation=rec_s,
                        confidence=conf_data, regime=current_regime
                    )
                except Exception:
                    pass
        except Exception:
            pass

def render_lock(name=""):
    st.markdown(f"""<div style="background:linear-gradient(135deg,#120d00,#0d1525);
        border:1px solid rgba(245,158,11,0.3);border-radius:14px;padding:40px 32px;text-align:center;">
        <div style="font-size:36px;margin-bottom:14px;">👑</div>
        <div style="font-size:20px;font-weight:800;color:#e2e8f0;margin-bottom:8px;">{name}</div>
        <div style="display:inline-block;background:rgba(245,158,11,0.1);color:{GOLD};
                    font-size:10px;font-weight:700;padding:3px 12px;border-radius:20px;
                    border:1px solid rgba(245,158,11,0.3);margin-bottom:14px;">PREMIUM FEATURE</div>
        <div style="font-size:13px;color:#374f6e;margin-bottom:6px;line-height:1.7;">
            Upgrade to Premium to unlock all 23 composite signal categories (incl. bear/short),<br>
            plus advanced Brief filters, signal charts, the short-squeeze scanner, and unlimited alerts.
        </div>
        <div style="font-size:12px;color:#2a3a52;margin-bottom:20px;">Founding price: $19/month · Cancel anytime · No contracts</div>
    </div>""", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)
    _,lc,_=st.columns([1,2,1])
    with lc:
        if gold_btn("👑 Unlock Premium", f"lock_{name[:20].replace(' ','_')}"): nav("pricing")
        st.markdown("<br>", unsafe_allow_html=True)
        if is_authed() and st.button("Already subscribed? Refresh →", key=f"lock_ref_{name[:10]}", use_container_width=True):
            st.rerun()

# ─────────────────────────────────────────────────────────────
# NAV CSS + TOPBAR
# ─────────────────────────────────────────────────────────────
NAV_CSS = """<style>
.msp-logo-click-target{display:inline-flex;align-items:center;height:44px;cursor:pointer;padding:0;line-height:1;}
.msp-logo-text{font-family:'JetBrains Mono',monospace;font-size:19px;font-weight:800;letter-spacing:-0.8px;white-space:nowrap;}
.msp-logo-market,.msp-logo-pro{color:#e2e8f0;}
.msp-logo-signal{color:#f59e0b;}
.element-container:has(.msp-logo-click-target)+.element-container{height:0px !important;overflow:visible !important;margin:0 !important;padding:0 !important;}
.element-container:has(.msp-logo-click-target)+.element-container .stButton>button{position:relative !important;top:-44px !important;left:0 !important;width:200px !important;height:44px !important;min-height:44px !important;opacity:0 !important;cursor:pointer !important;z-index:999 !important;background:transparent !important;border:none !important;box-shadow:none !important;}
.sw-divider{border:none;border-top:1px solid rgba(255,255,255,0.06);margin:0 0 24px 0;}
/* Pusher toast notification */
#sw-push-toast{position:fixed;top:80px;right:20px;z-index:9999;display:none;
  background:#0d1525;border:1px solid rgba(99,102,241,0.5);border-radius:12px;
  padding:14px 18px;box-shadow:0 8px 32px rgba(99,102,241,0.3);
  min-width:280px;max-width:360px;animation:slideIn 0.3s ease;}
@keyframes slideIn{from{transform:translateX(120%);opacity:0;}to{transform:translateX(0);opacity:1;}}
#sw-push-toast .toast-ticker{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;color:#818cf8;}
#sw-push-toast .toast-msg{font-size:12px;color:#374f6e;margin-top:4px;line-height:1.5;}
#sw-push-toast .toast-close{position:absolute;top:8px;right:12px;cursor:pointer;color:#4a5e7a;font-size:16px;}
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type{align-items:center !important;min-height:56px !important;}
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type>[data-testid="column"]{display:flex !important;align-items:center !important;padding-top:0 !important;padding-bottom:0 !important;}
[data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"]:first-of-type>[data-testid="column"]>div{width:100% !important;}
.sw-nav .stButton>button{font-size:13px !important;font-weight:500 !important;padding:6px 12px !important;min-height:38px !important;height:38px !important;border:1px solid rgba(255,255,255,0.15) !important;background:rgba(255,255,255,0.04) !important;color:#a8bdd4 !important;border-radius:7px !important;white-space:nowrap !important;width:100% !important;}
.sw-nav .stButton>button:hover{border-color:rgba(99,102,241,0.5) !important;background:rgba(99,102,241,0.1) !important;color:#a5b4fc !important;}
.sw-nav .stButton>button[kind="primary"]{background:#6366f1 !important;border-color:#6366f1 !important;color:#fff !important;font-weight:700 !important;}
/* Suppress Streamlit's transient "Missing Submit Button" RED FLASH inside forms: on a nav rerun the
   submit button streams in a beat after the inputs, so the frontend briefly renders that error, then
   removes it (<~0.4s). Keep alerts INSIDE any form hidden for the first ~0.4s, then fade in the ones
   that PERSIST (genuine validation errors). Every form here has a submit button, so nothing real is
   lost — the persistent error just fades in a hair later. Applies app-wide (login errors render
   OUTSIDE their form, so they stay instant). */
[data-testid="stForm"] [data-testid="stAlert"]{animation:mspFormAlert 0.45s ease;}
@keyframes mspFormAlert{0%,88%{opacity:0;}100%{opacity:1;}}
</style>"""

LOGO_HTML = (
    '<div class="msp-logo-click-target" style="gap:8px;">'
    + brand_logomark(22)
    + '<span class="msp-logo-text">'
      '<span class="msp-logo-market">Scan</span><span class="msp-logo-signal">viction</span>'
      '</span></div>'
)

def render_logo_click(key="msp_logo_home", dest="landing"):
    st.markdown(LOGO_HTML, unsafe_allow_html=True)
    if st.button(" ", key=key):
        nav(dest)

def _render_bottom_nav(active=""):
    """Native-style bottom tab bar — appears only when app is launched in PWA standalone mode (installed)."""
    # Use a unique URL param to navigate via the bottom bar
    nav_items = [
        ("home", "🏠", "Market", "dashboard"),
        ("perf", "📊", "Results", "performance"),
        ("disc", "🔍", "Discover", "discover"),
        ("watch", "⭐", "Watchlist", "watchlist"),
        ("more", "⚙️", "Settings", "settings"),
    ]

    # Build HTML buttons with form-submit links via streamlit query params.
    # These are raw <a href> reloads, so they must carry ?sid= or a PWA user
    # gets logged out on every bottom-tab tap (URL token is the session carrier).
    _sidq = ""
    try:
        _tok = st.session_state.get("_sid", "")
        if _tok:
            _sidq = f"&sid={_tok}"
    except Exception:
        _sidq = ""
    nav_html = ['<nav class="sw-bottom-nav">']
    for key, icon, label, page in nav_items:
        is_active = (active == page)
        active_cls = " active" if is_active else ""
        nav_html.append(
            f'<a href="?bottom_nav={page}{_sidq}" class="sw-bnav-item{active_cls}">'
            f'<span class="sw-bnav-icon">{icon}</span>'
            f'<span class="sw-bnav-label">{label}</span>'
            f'</a>'
        )
    nav_html.append('</nav>')

    # CSS for bottom nav - only shown in PWA standalone mode
    bottom_nav_css = """
    <style>
    .sw-bottom-nav {
        display: none;  /* Hidden by default - only show in standalone PWA */
        position: fixed;
        bottom: 0; left: 0; right: 0;
        background: rgba(8, 11, 20, 0.96);
        backdrop-filter: saturate(180%) blur(20px);
        -webkit-backdrop-filter: saturate(180%) blur(20px);
        border-top: 1px solid rgba(255,255,255,0.06);
        padding: 6px 0 calc(6px + env(safe-area-inset-bottom, 0px));
        z-index: 999990;
        justify-content: space-around;
        align-items: stretch;
    }
    @media (display-mode: standalone) {
        .sw-bottom-nav { display: flex; }
        /* Push main content up so nothing hides behind bottom nav */
        .main .block-container { padding-bottom: 90px !important; }
        /* On standalone, hide the top navigation row since bottom nav is primary */
        .sw-nav { display: none !important; }
        .sw-divider { display: none !important; }
        /* Hide the entire sidebar in standalone — bottom nav handles everything */
        [data-testid="stSidebar"] { display: none !important; }
        [data-testid="collapsedControl"],
        [data-testid="stSidebarCollapseButton"] { display: none !important; }
    }
    .sw-bnav-item {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 8px 4px 4px;
        text-decoration: none !important;
        color: #6b7fa0 !important;
        transition: color 0.15s ease-out, transform 0.1s ease-out;
        min-height: 52px;
    }
    .sw-bnav-item:active {
        transform: scale(0.94);
    }
    .sw-bnav-item.active {
        color: #818cf8 !important;
    }
    .sw-bnav-icon {
        font-size: 22px;
        line-height: 1;
        margin-bottom: 3px;
    }
    .sw-bnav-label {
        font-size: 10px;
        font-weight: 600;
        font-family: 'Inter', sans-serif;
        letter-spacing: 0.2px;
    }
    </style>
    """
    st.markdown(bottom_nav_css + "".join(nav_html), unsafe_allow_html=True)

    # Handle bottom nav clicks via URL param
    try:
        params = st.query_params
        if "bottom_nav" in params:
            target = params.get("bottom_nav", "")
            st.query_params.clear()
            if target in {"dashboard","discover","watchlist","signal_track","performance","settings","screener","bi_dashboard"}:
                nav(target)
    except Exception:
        pass


def render_topbar(active=None):
    # When the caller doesn't pass the active page explicitly, derive it from the
    # router state — several pages (Contact, Features, detail, …) called
    # render_topbar() bare, so their nav pill lost its held/active highlight.
    if active is None:
        active = st.session_state.get("page", "") or ""
    st.markdown(NAV_CSS, unsafe_allow_html=True)
    if is_authed():
        _render_bottom_nav(active)

    BLUE_LOC = "#6366f1"
    st.markdown(f"""
<style>
/* ════ TOPBAR BUTTON STYLES ════ */
/* Logo — amber glow on hover */
.sw-tb-logo .stButton>button {{
    background:transparent !important; border:none !important; box-shadow:none !important;
    padding:0 4px !important; height:40px !important; min-height:40px !important;
    font-family:'Inter',sans-serif !important; font-size:20px !important; font-weight:900 !important;
    letter-spacing:-0.8px !important; color:#e2e8f0 !important; white-space:nowrap !important;
    width:auto !important; transition:filter 0.2s ease !important;
}}
.sw-tb-logo .stButton>button:hover {{
    filter:drop-shadow(0 0 10px rgba(245,158,11,0.45)) !important; color:#e2e8f0 !important;
}}

/* make each nav button fill its equal-width column → even spacing */
[class*="st-key-navtb_"] {{ width:100% !important; }}
[class*="st-key-navtb_"] [data-testid="stButton"],
[class*="st-key-navtb_"] .stButton {{ width:100% !important; }}
                
/* Nav items — crisp indigo-glass pill (Linear-style: thin border, restrained, no
   gimmicky shimmer). Consistent with the app-wide .stButton system. */
[class*="st-key-navtb_"] button {{
    position:relative !important; overflow:hidden !important;
    background:rgba(129,140,248,0.05) !important;
    border:1px solid rgba(129,140,248,0.16) !important;
    color:#aeb9cf !important;
    font-size:12.5px !important; font-weight:600 !important; letter-spacing:0.2px !important;
    height:38px !important; min-height:38px !important; padding:0 14px !important;
    border-radius:10px !important; white-space:nowrap !important; width:100% !important;
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.04) !important;
    transition:all 0.16s cubic-bezier(.4,0,.2,1) !important;
}}
[class*="st-key-navtb_"] button > * {{ position:relative !important; z-index:1 !important; }}
/* Dead-center the label: the button's inner markdown <p> carries its own margins/
   line-height, which floated the text high/low inside the 38px pill (visibly
   misaligned between tabs). Flex-center the button and zero the label's box. */
[class*="st-key-navtb_"] button {{
    display:inline-flex !important; align-items:center !important; justify-content:center !important;
}}
/* The label must be a BLOCK, not a flex box. It was `display:flex` + `overflow:hidden`,
   and an overflowing centred flex item is clipped at BOTH ends — which is why every tab
   read "arket Overvie" / "erformanc" rather than truncating with an ellipsis. As a block
   with nowrap the ellipsis actually applies, and the button (already flex) does the
   centring. min-width:0 lets it shrink below its content width instead of overflowing. */
[class*="st-key-navtb_"] button [data-testid="stMarkdownContainer"] {{
    min-width:0 !important; max-width:100% !important; overflow:hidden !important;
}}
[class*="st-key-navtb_"] button [data-testid="stMarkdownContainer"] p {{
    margin:0 !important; padding:0 !important; line-height:1.2 !important;
    display:block !important; white-space:nowrap !important;
    overflow:hidden !important; text-overflow:ellipsis !important;
}}
/* The Brief pill carries a live count ("🔔 Brief (12)") — tighter padding so the
   number never clips inside the fixed-height pill. */
[class*="st-key-navtb_signals"] button {{ padding:0 8px !important; }}

[class*="st-key-navtb_"] button:hover {{
    background:rgba(99,102,241,0.16) !important;
    border-color:rgba(129,140,248,0.5) !important; color:#eaf1ff !important;
    transform:translateY(-1px) !important;
    box-shadow:0 4px 14px rgba(99,102,241,0.22) !important;
}}

/* Active page — filled indigo + clean gold underline accent */
[class*="st-key-navtb_"][class*="_active"] button {{
    background:linear-gradient(180deg, rgba(99,102,241,0.30), rgba(99,102,241,0.14)) !important;
    border:1px solid rgba(129,140,248,0.7) !important;
    color:#fff !important; font-weight:700 !important;
    box-shadow:0 0 0 1px rgba(129,140,248,0.25), 0 4px 14px rgba(99,102,241,0.26) !important;
}}
[class*="st-key-navtb_"][class*="_active"] button::after {{
    content:"" !important; position:absolute !important;
    bottom:5px !important; left:50% !important; transform:translateX(-50%) !important;
    width:36% !important; height:2px !important; border-radius:2px !important;
    background:linear-gradient(90deg, transparent, {GOLD}, transparent) !important;
}}

/* Active page — strong blue glow + gold underline accent */
.sw-tb-active .stButton>button {{
    position:relative !important; overflow:hidden !important;
    background:linear-gradient(180deg, rgba(99,102,241,0.40), rgba(99,102,241,0.18)) !important;
    border:1px solid rgba(129,140,248,0.85) !important;
    color:#fff !important; font-weight:700 !important;
    font-size:12.5px !important; height:38px !important; min-height:38px !important;
    padding:0 14px !important; border-radius:10px !important; white-space:nowrap !important; width:100% !important;
    box-shadow:0 0 18px rgba(99,102,241,0.45), inset 0 1px 0 rgba(255,255,255,0.15) !important;
}}
.sw-tb-active .stButton>button::after {{
    content:"" !important; position:absolute !important;
    bottom:5px !important; left:50% !important; transform:translateX(-50%) !important;
    width:42% !important; height:2px !important; border-radius:2px !important;
    background:linear-gradient(90deg, transparent, {GOLD}, transparent) !important;
}}

/* Primary CTA (guest Sign Up) */
.sw-tb-primary .stButton>button {{
    background:linear-gradient(135deg,#6366f1,#6366f1) !important;
    border:1px solid rgba(129,140,248,0.6) !important; color:#fff !important; font-weight:700 !important;
    font-size:13px !important; height:36px !important; min-height:36px !important; padding:0 16px !important;
    border-radius:9px !important; box-shadow:0 4px 18px rgba(99,102,241,0.45) !important;
    transition:all 0.18s cubic-bezier(0.16,1,0.3,1) !important;
}}
.sw-tb-primary .stButton>button:hover {{
    background:linear-gradient(135deg,#6366f1,#4f46e5) !important;
    transform:translateY(-1px) !important; box-shadow:0 8px 26px rgba(99,102,241,0.6) !important;
}}

/* Icon buttons */
.sw-tb-icon .stButton>button {{
    background:rgba(129,140,248,0.05) !important;
    border:1px solid rgba(129,140,248,0.18) !important; color:#aab8cc !important;
    height:36px !important; min-height:36px !important; padding:0 !important; border-radius:10px !important;
    font-size:16px !important; width:38px !important; min-width:38px !important;
    transition:all 0.16s cubic-bezier(.4,0,.2,1) !important;
}}
.sw-tb-icon .stButton>button:hover {{
    background:linear-gradient(180deg, rgba(99,102,241,0.18), rgba(99,102,241,0.08)) !important;
    border-color:rgba(129,140,248,0.5) !important; color:#dbeafe !important;
    transform:translateY(-1px) !important; box-shadow:0 4px 14px rgba(99,102,241,0.25) !important;
}}                

/* ===== User-menu dropdown panel ===== */
[data-testid="stPopoverBody"] {{
    background:linear-gradient(180deg,#0f1a2e,#0a1018) !important;
    border:1px solid rgba(165,180,252,0.20) !important;
    border-radius:16px !important;
    box-shadow:0 24px 64px rgba(0,0,0,0.70) !important;
    padding:10px !important; min-width:272px !important;
}}
/* header card */
.um-head {{
    display:flex !important; align-items:center !important; gap:12px !important;
    padding:8px 8px 12px !important; margin-bottom:6px !important;
    border-bottom:1px solid rgba(255,255,255,0.08) !important;
}}
.um-avatar {{
    width:44px !important; height:44px !important; border-radius:50% !important; flex:0 0 44px !important;
    background:linear-gradient(135deg,#6366f1,#7c3aed) !important;
    display:flex !important; align-items:center !important; justify-content:center !important;
    font-size:18px !important; font-weight:800 !important; color:#fff !important;
    box-shadow:0 4px 14px rgba(99,102,241,0.45) !important;
}}
.um-id {{ min-width:0 !important; }}
.um-name {{ font-size:14px !important; font-weight:700 !important; color:#f1f5f9 !important; line-height:1.2 !important; }}
.um-email {{ font-size:11.5px !important; color:#7e90ac !important; white-space:nowrap !important; overflow:hidden !important; text-overflow:ellipsis !important; }}
.um-role {{
    display:inline-block !important; margin-top:5px !important;
    font-size:9.5px !important; font-weight:700 !important; letter-spacing:0.5px !important;
    text-transform:uppercase !important; padding:2px 9px !important; border-radius:20px !important;
}}

/* menu rows — full width, left aligned, icon + label */
[data-testid="stPopoverBody"] .stButton {{ width:100% !important; }}
[data-testid="stPopoverBody"] .stButton>button {{
    width:100% !important; background:transparent !important; border:1px solid transparent !important;
    color:#c7d4e6 !important; justify-content:flex-start !important; text-align:left !important;
    font-size:13.5px !important; font-weight:500 !important;
    padding:11px 14px !important; height:auto !important; min-height:0 !important;
    border-radius:10px !important; transition:all 0.15s ease !important;
}}
[data-testid="stPopoverBody"] .stButton>button:hover {{
    background:linear-gradient(90deg, rgba(99,102,241,0.24), rgba(99,102,241,0.08)) !important;
    color:#fff !important; padding-left:18px !important;
}}
/* hairline between rows */
[data-testid="stPopoverBody"] .stButton:not(:last-of-type) {{
    border-bottom:1px solid rgba(255,255,255,0.05) !important;
}}
/* Log out — red accent */
[class*="st-key-um_logout"] button {{ color:#f4a3a3 !important; }}
[class*="st-key-um_logout"] button:hover {{
    background:linear-gradient(90deg, rgba(239,68,68,0.20), rgba(239,68,68,0.06)) !important;
    color:#fff !important;
}}

/* ════ MOBILE TOPBAR REFLOW ════
   The real topbar is st.columns + st.button; on narrow screens Streamlit stacks
   those columns full-width, so the nav sprawls into a giant vertical list. This
   collapses it into a tidy wrapped row (logo on its own top line, nav pills below).
   Scoped via :has() to ONLY the guest topbar (contains tb_g_signup) and the authed
   topbar (contains usermenu) — no other page columns are affected — and gated behind
   a max-width media query so the desktop/web layout is untouched. */
@media (max-width:900px) {{
  /* Streamlit renamed the column test-id from "column" → "stColumn" across versions,
     and an earlier global rule forces [data-testid="column"]{{min-width:100%}} to stack
     everything. Match BOTH test-ids so this works regardless of version and outweighs
     that stacking rule. */
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]),
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) {{
      display:flex !important; flex-direction:row !important; flex-wrap:wrap !important;
      align-items:center !important; gap:6px !important; row-gap:6px !important; min-height:0 !important;
  }}
  /* logo gets its own top row, left-aligned */
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]) > [data-testid="stColumn"]:first-child,
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]) > [data-testid="column"]:first-child,
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) > [data-testid="stColumn"]:first-child,
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) > [data-testid="column"]:first-child {{
      flex:0 0 100% !important; width:100% !important; min-width:0 !important; margin-bottom:2px !important;
  }}
  /* remaining columns share the wrapped rows evenly instead of each taking 100% */
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]) > [data-testid="stColumn"]:not(:first-child),
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]) > [data-testid="column"]:not(:first-child),
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) > [data-testid="stColumn"]:not(:first-child),
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) > [data-testid="column"]:not(:first-child) {{
      flex:1 1 auto !important; width:auto !important; min-width:64px !important; max-width:none !important;
  }}
  /* kill inner vertical-block gaps + the empty guest wrapper divs that pad the height */
  [data-testid="stHorizontalBlock"]:has([class*="st-key-tb_g_signup"]) [data-testid="stVerticalBlock"],
  [data-testid="stHorizontalBlock"]:has([class*="st-key-usermenu"]) [data-testid="stVerticalBlock"] {{ gap:0 !important; }}
  .sw-tb-btn {{ display:none !important; }}
  /* compact the pills themselves */
  [class*="st-key-tb_g_"] button,
  [class*="st-key-navtb_"] button {{
      height:34px !important; min-height:34px !important; padding:0 8px !important;
      font-size:12px !important; width:100% !important;
  }}
}}
</style>
""", unsafe_allow_html=True)

    MSP_LOGO = ('<span style="display:inline-flex;align-items:center;gap:9px;cursor:pointer;">'
                + brand_logomark(26)
                + '<span style="font-family:\'Inter\',sans-serif;font-size:20px;font-weight:900;letter-spacing:-0.8px;">'
                  '<span style="color:#e2e8f0;">Scan</span><span style="color:#f59e0b;">viction</span></span>'
                + '</span>')

    if is_authed():
        _unseen_list = _unseen_signals()
        _unseen = min(len(_unseen_list), 99)
        # "New signals" toast — fires on LOGIN (first render of the session) if you have unseen
        # entitled signals, and again whenever new ones arrive mid-session. Names a couple
        # tickers so it's actionable; suppressed while you're actually on the feed.
        try:
            _prev_unseen = st.session_state.get("_unseen_shown")   # None on the first render
            _base = 0 if _prev_unseen is None else _prev_unseen
            if _unseen > _base and active != "signals":
                _names = ", ".join(e.get("ticker", "") for e in _unseen_list[:3] if e.get("ticker"))
                _more = f" +{_unseen - 3} more" if _unseen > 3 else ""
                st.toast(f"🔔 {_unseen} new in your Daily Brief"
                         + (f": {_names}{_more}" if _names else ""), icon="🔔")
            st.session_state["_unseen_shown"] = _unseen
        except Exception:
            pass
        _sig_lbl = "🔔 Brief" + (f" ({_unseen})" if _unseen else "")
        # Nav order tells the product story: the overall MARKET, then how the model's calls
        # worked out (Results), then what it's calling now (Discover) and what just changed
        # (Brief) — then the two marketing tabs.
        #
        # Deliberately NOT here:
        #  • Refine — filtering belongs with the feed you're filtering, so it moved inside
        #    the Brief. A separate tab for narrowing the same data was a surface users had
        #    to remember existed.
        #  • Watchlist — personal state, not a place you browse. It's in the account menu,
        #    which already carried an entry for it.
        # Both removals bought width the remaining labels needed: with an owner signed in
        # there were nine tabs plus logo and account menu, and every pill was truncating to
        # "Ma…" / "Disco…". Same reason "Market Overview" is just "Market" here — the page
        # still titles itself in full, and the PWA bottom nav already said "Market".
        pages = [("Market","dashboard"), ("Results","performance"),
                 ("Discover","discover"), (_sig_lbl,"signals"),
                 ("Pricing","pricing"), ("Contact","contact")]
        if is_admin(): pages.append(("Admin","admin"))

        ri = {"owner":"👑","admin":"🛡️","premium":"⭐","free":"👤"}.get(st.session_state.role,"👤")
        first = (st.session_state.user.get("name","") or "").split()[0]

        # Pill widths are DERIVED from the label, not hand-tuned per page: a per-page
        # lookup table drifts the moment a label changes (which is how "Performance" and
        # "Watchlist" ended up clipped). ~7 characters fit in one ratio unit at 12.5px,
        # and the floor keeps short labels from collapsing to a sliver. Emoji count double
        # because they render roughly two characters wide.
        def _nav_ratio(label):
            width = sum(2 if ord(ch) > 0x2100 else 1 for ch in label)
            return max(0.92, round(width / 7.0, 2))
        nav_ratios = [_nav_ratio(_lbl) for (_lbl, _) in pages]
        ratios = [2.2] + nav_ratios + [1.5]
        cols = st.columns(ratios, gap="small")

        with cols[0]:
            render_logo_click("tb_logo_auth", "dashboard")

        for i, (lbl, pg) in enumerate(pages):
            with cols[i + 1]:
                is_active = (active == pg)
                btn_key = f"navtb_{pg}_active" if is_active else f"navtb_{pg}"
                if st.button(lbl, key=btn_key):
                    if pg == "discover":
                        # Topbar "Discover" ALWAYS returns to the all-categories home —
                        # previously, with a category drilled in, clicking Discover was a
                        # no-op (page unchanged, category still selected).
                        st.session_state.discover_cat = DISCOVER_HOME
                        st.session_state["_disc_scroll_top"] = True
                    nav(pg)

# ── User menu ──
        with cols[len(pages) + 1]:
            _email = st.session_state.user.get("email","")
            _initial = (first[:1] or "U").upper()
            _role = st.session_state.get("role","free")
            _role_label = {"owner":"Owner","admin":"Admin","premium":"Premium ⭐","free":"Free Plan"}.get(_role,"Free Plan")
            _role_color = {"owner":GOLD,"admin":"#a5b4fc","premium":"#a78bfa","free":"#6b7fa0"}.get(_role,"#6b7fa0")

            st.markdown(f"""<style>
            /* ---- trigger pill + avatar ---- */
            /* This in-column <style> injection adds a 0-height phantom element to the
               column's vertical block; its 16px flex gap pushes the popover ~8px below
               the nav pills. Collapse that gap so the 38px trigger centers level with them. */
            [data-testid="stVerticalBlock"]:has(> [class*="st-key-usermenu"]) {{ gap:0 !important; }}
            [class*="st-key-usermenu"] [data-testid="stPopover"] {{
                width:100% !important; margin:0 !important;
                display:flex !important; align-items:center !important; height:38px !important;
            }}
            [class*="st-key-usermenu"] button {{
                display:flex !important; align-items:center !important; justify-content:flex-start !important;
                background:linear-gradient(180deg, rgba(124,58,237,0.18), rgba(99,102,241,0.10)) !important;
                border:1px solid rgba(165,180,252,0.32) !important;
                color:#e8eef9 !important; font-weight:700 !important; font-size:13px !important;
                height:38px !important; min-height:38px !important; margin:0 !important; padding:0 12px !important;
                border-radius:10px !important; white-space:nowrap !important; width:100% !important;
                box-shadow:inset 0 1px 0 rgba(255,255,255,0.08), 0 2px 8px rgba(0,0,0,0.4) !important;
                transition:all 0.2s cubic-bezier(0.16,1,0.3,1) !important;
            }}
            [class*="st-key-usermenu"] button:hover {{
                border-color:rgba(167,139,250,0.65) !important;
                background:linear-gradient(180deg, rgba(124,58,237,0.30), rgba(99,102,241,0.16)) !important;
                transform:translateY(-1px) !important;
                box-shadow:0 6px 18px rgba(124,58,237,0.38), 0 0 0 1px rgba(167,139,250,0.28) !important;
            }}
            [class*="st-key-usermenu"] button::before {{
                content:"{_initial}" !important;
                display:inline-flex !important; align-items:center !important; justify-content:center !important;
                width:24px !important; height:24px !important; border-radius:50% !important;
                margin-right:9px !important; flex-shrink:0 !important;
                background:linear-gradient(135deg,#6366f1,#7c3aed) !important;
                color:#fff !important; font-weight:800 !important; font-size:11px !important;
                box-shadow:0 2px 8px rgba(99,102,241,0.45) !important;
            }}
            [class*="st-key-usermenu"] button svg {{ margin-left:auto !important; opacity:0.7 !important; }}

            /* ---- dropdown rows (fill width, left-align, dividers, red logout) ---- */
            [class*="st-key-um_"],
            [class*="st-key-um_"] [data-testid="stButton"],
            [class*="st-key-um_"] .stButton {{ width:100% !important; }}
            [class*="st-key-um_"] button,
            [data-testid="stPopoverBody"] [data-testid="stButton"] button {{
                width:100% !important; background:transparent !important; border:1px solid transparent !important;
                color:#c7d4e6 !important; justify-content:flex-start !important; text-align:left !important;
                font-size:13.5px !important; font-weight:500 !important;
                padding:11px 14px !important; height:auto !important; min-height:0 !important;
                border-radius:10px !important; transition:all 0.15s ease !important;
            }}
            [class*="st-key-um_"] button:hover,
            [data-testid="stPopoverBody"] [data-testid="stButton"] button:hover {{
                background:linear-gradient(90deg, rgba(99,102,241,0.24), rgba(99,102,241,0.08)) !important;
                color:#fff !important; padding-left:18px !important;
            }}
            [class*="st-key-um_"]:not([class*="um_logout"]) {{
                border-bottom:1px solid rgba(255,255,255,0.06) !important;
            }}
            [class*="st-key-um_logout"] button {{ color:#f4a3a3 !important; }}
            [class*="st-key-um_logout"] button:hover {{
                background:linear-gradient(90deg, rgba(239,68,68,0.20), rgba(239,68,68,0.06)) !important;
                color:#fff !important;
            }}
            </style>""", unsafe_allow_html=True)

            with st.popover(f"{first}", use_container_width=True, key="usermenu"):
                st.markdown(
                    f'<div class="um-head">'
                    f'<div class="um-avatar">{_initial}</div>'
                    f'<div class="um-id">'
                    f'<div class="um-name">{_esc(first)}</div>'
                    f'<div class="um-email">{_email}</div>'
                    f'<div class="um-role" style="color:{_role_color};border:1px solid {_role_color}55;">{_role_label}</div>'
                    f'</div></div>', unsafe_allow_html=True)
                if st.button("⭐\u2002Watchlist", key="um_watch", use_container_width=True):
                    nav("watchlist")
                if st.button("🔔\u2002Alerts", key="um_alerts", use_container_width=True):
                    nav("settings")
                if st.button("⚙️\u2002Account Settings", key="um_settings", use_container_width=True):
                    nav("settings")
                if st.button("↪\u2002Log out", key="um_logout", use_container_width=True):
                    logout()

        # ── High-conviction "new signal" banner (loudest in-app surface) ──
        # A strong UNSEEN setup gets a one-line banner with a direct View, at the top of every
        # page except the feed itself. Only entitled signals at/above the bar, only until the
        # user dismisses it (per signal id) or opens the feed (which clears the watermark).
        try:
            if active != "signals":
                _dismissed = st.session_state.setdefault("_dismissed_banners", set())
                _hot = next((e for e in _unseen_list
                             if (e.get("score_at_trigger", 0) or 0) >= SIGNAL_BANNER_MIN
                             and e.get("id") not in _dismissed), None)
                if _hot:
                    _bt = _hot.get("ticker", ""); _bc = _hot.get("category", "")
                    _bs = int(_hot.get("score_at_trigger", 0) or 0)
                    _br = _hot.get("recommendation", "") or ""
                    _bid = _hot.get("id", "")
                    _bverb = "·" if _bc in EVENT_ALERT_TYPES else "just entered"   # 8-K/insider aren't "entered"
                    bn1, bn2, bn3 = st.columns([8, 1.15, 0.7], gap="small")
                    with bn1:
                        st.markdown(
                            f'<div style="background:linear-gradient(90deg,{GOLD}1f,transparent);'
                            f'border:1px solid {GOLD}55;border-left:3px solid {GOLD};border-radius:10px;'
                            f'padding:9px 16px;margin:4px 0 2px;display:flex;align-items:center;gap:9px;flex-wrap:wrap;">'
                            f'<span style="font-size:15px;">🔔</span>'
                            f'<span style="font-family:\'JetBrains Mono\',monospace;font-weight:800;color:#e2e8f0;">{_esc(_bt)}</span>'
                            f'<span style="font-size:12px;color:#94a3b8;">{_bverb}</span>'
                            f'<span style="font-size:12px;font-weight:700;color:{GOLD};">{_esc(_bc)}</span>'
                            f'<span style="font-size:11px;color:#6b7fa0;">· Score {_bs}{(" · " + _esc(_br)) if _br else ""}</span>'
                            f'</div>', unsafe_allow_html=True)
                    with bn2:
                        if st.button("View →", key=f"sigban_view_{_bid}", use_container_width=True):
                            st.session_state.detail_ticker = _bt
                            st.session_state.detail_data = {}
                            _dismissed.add(_bid)
                            nav("stock_detail")
                    with bn3:
                        if st.button("✕", key=f"sigban_x_{_bid}", use_container_width=True, help="Dismiss"):
                            _dismissed.add(_bid); st.rerun()
        except Exception:
            pass

    else:  # ← now attached to `if is_authed()`, not the for loop
        ratios = [2.5, 0.85, 0.85, 0.85, 0.85, 1.2]
        cols = st.columns(ratios, gap="small")

        with cols[0]:
            render_logo_click("tb_logo_guest", "landing")

        for i, (lbl, pg) in enumerate([("Features","features"),("Pricing","pricing"),("Contact","contact"),("Login","login")]):
            with cols[i + 1]:
                st.markdown('<div class="sw-tb-btn">', unsafe_allow_html=True)
                if st.button(lbl, key=f"tb_g_{pg}"):
                    nav(pg)
                st.markdown('</div>', unsafe_allow_html=True)

        with cols[5]:
            st.markdown('<div class="sw-tb-primary">', unsafe_allow_html=True)
            if st.button("Sign Up →", key="tb_g_signup"):
                nav("signup")
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<hr class="sw-divider">', unsafe_allow_html=True)
# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
def render_sidebar():
    with st.sidebar:
        st.markdown(f'<div style="padding:20px 18px 10px;">{LOGO_HTML}<div style="font-size:10px;color:rgba(255,255,255,.2);margin-top:4px;">Market Intelligence Platform</div></div>',unsafe_allow_html=True)
        st.divider()
        if is_authed():
            st.markdown('<div style="font-size:10px;font-weight:700;color:rgba(255,255,255,.2);letter-spacing:1.5px;text-transform:uppercase;padding:12px 18px 5px;">Discover</div>',unsafe_allow_html=True)
            for cat_key,(desc,tier) in COMPOSITE_CATS.items():
                is_locked=tier=="premium" and not is_premium()
                safe=cat_key.replace(" ","_").replace("+","p").replace("→","r").replace("🌡️","T").replace("📉","D")[:28]
                label=cat_key+(" ⭐" if is_locked else "")
                if st.button(label,key=f"sb_c_{safe}",use_container_width=True):
                    if is_locked: nav("pricing")
                    else: st.session_state.discover_cat=cat_key; nav("discover")
            st.markdown('<div style="font-size:10px;font-weight:700;color:rgba(255,255,255,.2);letter-spacing:1.5px;text-transform:uppercase;padding:12px 18px 5px;">Categories</div>',unsafe_allow_html=True)
            for cat in CATEGORIES:
                if st.button(cat,key=f"sb_s_{cat[:20].replace(' ','_')}",use_container_width=True):
                    st.session_state.discover_cat=cat; nav("discover")
            st.markdown('<div style="font-size:10px;font-weight:700;color:rgba(255,255,255,.2);letter-spacing:1.5px;text-transform:uppercase;padding:12px 18px 5px;">Tools</div>',unsafe_allow_html=True)
            _tools = [("📊","Market Overview","dashboard"),("📈","Results","performance"),("⭐","Watchlist","watchlist")]
            # "Full Track Record", not "Signal Track Record": this is the per-horizon audit
            # (1/3/5/10/30-day vs the S&P) that the Results page summarises. Naming it as the
            # deep version of Results says which to open; two "track record" pages did not.
            _tools += [("📉","Full Track Record","signal_track"),("💰","Pricing","pricing"),("🔔","Alerts & Settings","settings"),("💬","Contact & Help","contact")]
            for icon,label,pg in _tools:
                if st.button(f"{icon} {label}",key=f"sb_{pg}",use_container_width=True): nav(pg)
            if is_admin():
                st.markdown('<div style="font-size:10px;font-weight:700;color:rgba(255,255,255,.2);letter-spacing:1.5px;text-transform:uppercase;padding:12px 18px 5px;">Admin</div>',unsafe_allow_html=True)
                if st.button("🛠️ Admin Panel",key="sb_admin",use_container_width=True): nav("admin")
            st.divider()
            if not is_premium():
                st.markdown('<div style="padding:4px 10px 10px;">', unsafe_allow_html=True)
                if gold_btn("Go Premium","sb_gold"): nav("pricing")
                st.markdown('</div>', unsafe_allow_html=True)
            ri={"owner":"👑","admin":"🛡️","premium":"⭐","free":"👤"}.get(st.session_state.role,"👤")
            role_color={"owner":GOLD,"admin":"#a5b4fc","premium":"#a78bfa","free":"#4a5e7a"}.get(st.session_state.role,"#4a5e7a")
            plan_label={"owner":"Owner","admin":"Admin","premium":"Premium ⭐","free":"Free Plan"}.get(st.session_state.role,"Free")
            db_sb = st.session_state.users_db.get(st.session_state.user.get("email",""),{})
            verified = db_sb.get("verified",False)
            v_badge = ' <span style="color:#4ade80;font-size:9px;">✓ verified</span>' if verified else ''
            st.markdown(f'''<div style="padding:8px 14px;background:rgba(255,255,255,0.03);border-top:1px solid rgba(255,255,255,0.06);margin-top:4px;">
                <div style="font-size:12px;font-weight:600;color:{role_color};">{ri} {st.session_state.user["name"]}{v_badge}</div>
                <div style="font-size:10px;color:#2a3a52;margin-top:2px;">{plan_label}</div>
            </div>''',unsafe_allow_html=True)
            if st.button("Log Out",key="sb_logout",use_container_width=True): logout()
        else:
            st.markdown('<div style="padding:12px 18px;font-size:12px;color:#374f6e;margin-bottom:8px;">Sign in to access Scanviction.</div>',unsafe_allow_html=True)
            if st.button("🚀 Sign Up Free",key="sb_signup",use_container_width=True,type="primary"): nav("signup")
            if st.button("Login →",key="sb_login",use_container_width=True): nav("login")

            st.markdown("""<div style="margin:14px 10px;background:#080c18;border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:12px 14px;">
                <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,.2);letter-spacing:1px;text-transform:uppercase;margin-bottom:7px;">Free Includes</div>
                <div style="font-size:12px;color:#2a3a52;line-height:2.2;">✅ Live market data<br>✅ 8 composite categories<br>✅ Social sentiment<br>✅ Plain-English insights<br>✅ Watchlist</div>
            </div>""",unsafe_allow_html=True)
        st.markdown('<div style="padding:8px 18px;font-size:10px;color:rgba(255,255,255,.1);">© 2026 Scanviction</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────────────────────
_FOOTER_STATE = {"done": False}

def render_footer(force=False):
    # Idempotent per script run: a global call after the router renders the footer on
    # EVERY page, but pages that still call render_footer() inline won't double it.
    if _FOOTER_STATE.get("done") and not force:
        return
    _FOOTER_STATE["done"] = True
    # Footer links are RAW <a href> navigations (full page reload). They must carry the
    # session token — a bare "?page=contact" replaces the whole query string, dropping
    # ?sid=… and logging the user out on click (cookies are disabled; the URL token is
    # the only session carrier). Append &sid= when authed.
    _sidq = ""
    try:
        _tok = st.session_state.get("_sid", "")
        if _tok:
            _sidq = f"&sid={_tok}"
    except Exception:
        _sidq = ""
    st.markdown(f"""
    <div class="sw-footer-wrap">
        <div style="max-width:1400px;margin:0 auto;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:24px;margin-bottom:24px;">
                <div>
                    <span style="display:inline-flex;align-items:center;gap:9px;font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;letter-spacing:-.5px;">
                        {brand_logomark(24)}<span><span style="color:#e2e8f0;">Scan</span><span style="color:{GOLD};">viction</span></span>
                    </span>
                    <div style="font-size:12px;color:rgba(255,255,255,.2);margin-top:6px;">Market Intelligence Platform</div>
                </div>
                <div style="display:flex;gap:32px;font-size:12px;">
                    <a href="?page=privacy{_sidq}" target="_self" style="color:rgba(255,255,255,.4);text-decoration:none;">Privacy Policy</a>
                    <a href="?page=terms{_sidq}" target="_self" style="color:rgba(255,255,255,.4);text-decoration:none;">Terms of Service</a>
                    <a href="?page=terms{_sidq}" target="_self" style="color:rgba(255,255,255,.4);text-decoration:none;">Risk Disclaimer</a>
                    <a href="?page=contact{_sidq}" target="_self" style="color:rgba(255,255,255,.4);text-decoration:none;">Contact</a>
                </div>
            </div>
            <div class="disc">⚠️ <strong style="color:#4a5e7a;">Risk Disclaimer:</strong> Trading stocks involves substantial risk of financial loss. Scanviction provides algorithmic, educational content only — not financial, investment, legal, or tax advice. All signals may be inaccurate or delayed. Past performance does not guarantee future results. Always consult a licensed financial professional before making investment decisions.</div>
            <div style="font-size:10px;color:rgba(255,255,255,.1);margin-top:10px;text-align:right;">© 2026 Scanviction. All rights reserved.</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: LANDING
# ─────────────────────────────────────────────────────────────
# ── "Proof" track-record stat (landing + Market Overview) ─────────────────────
SIGNAL_PROOF_MIN_AVG = float(_os.environ.get("SIGNAL_PROOF_MIN_AVG", "0"))  # hide the band below this avg %

@st.cache_data(ttl=120, show_spinner=False)
def _signal_track_summary():
    """Aggregate the MEASURED move since signal across tracked signals for the landing / Market
    Overview proof strip. Uses each signal's resolved outcome (outcomes.current_pct, filled by the
    outcome-updater as horizons elapse) — NOT a live mark on minutes-old signals, which is noisy
    and makes the stat flicker in/out. Stable + a truer track record. None if <3 resolved.

    Direction-aware: a SHORT/bear signal is right when price FALLS, so its return is the INVERSE of
    the price move (a −8% drop = +8% for the short). The average is the honest headline — equal $ in
    every signal → your return is the mean of the per-signal moves (a raw sum of %s would overstate).
    """
    if not HAS_SIGNAL_ENGINE:
        return None
    try:
        pcts = []
        for e in get_recent_signal_events(limit=500):
            cp = (e.get("outcomes") or {}).get("current_pct")
            if cp is None:          # only count signals that have actually been measured
                continue
            pct = float(cp)
            try:
                if category_dir(e.get("category", "")) == "bear":
                    pct = -pct       # a correctly-called short (price down) is a positive return
            except Exception:
                pass
            pcts.append(pct)
        if len(pcts) < 3:
            return None
        wins = sum(1 for p in pcts if p > 0)
        return {"count": len(pcts), "avg": sum(pcts) / len(pcts),
                "win_rate": round(wins / len(pcts) * 100), "best": max(pcts), "total": sum(pcts)}
    except Exception:
        return None

def render_signal_proof(context="overview"):
    """Live 'track record' proof strip (tracked · win rate · avg move since signal · best).
    Social proof for the landing page + Market Overview. Hidden when there's too little data OR
    the aggregate isn't favorable (SIGNAL_PROOF_MIN_AVG) — we never advertise a losing number.

    Returns True when the band actually rendered. Callers that lead with this band (the
    Results page, whose whole premise IS the track record) check the return and render
    their own honest empty state instead of silently showing nothing.

    The favourability filter is for the MARKETING surfaces only (landing, Market Overview).
    The Results page is the model's own accountability page — it states outright that
    nothing is curated — so it shows the real aggregate whatever it is, and hides only when
    there genuinely isn't a measured record yet."""
    s = _signal_track_summary()
    if not s:
        return False
    if context != "performance" and s["avg"] < SIGNAL_PROOF_MIN_AVG:
        return False
    avg = s["avg"]; wr = s["win_rate"]; n = s["count"]; best = s["best"]
    avg_s = f"+{avg:.1f}%" if avg >= 0 else f"{avg:.1f}%"
    best_s = f"+{best:.1f}%" if best >= 0 else f"{best:.1f}%"
    _ac = GREEN if avg >= 0 else RED
    _wc = GREEN if wr >= 50 else GOLD
    st.markdown(f'''<div style="background:linear-gradient(135deg,{GOLD}12,transparent);border:1px solid {GOLD}33;border-radius:14px;padding:16px 20px;margin:8px 0 6px;">
        <div style="font-size:10px;font-weight:800;color:{GOLD};letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;">📈 Track Record · to date</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:14px;">
            <div><div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:#e2e8f0;">{n:,}</div><div style="font-size:11px;color:#4a5e7a;margin-top:2px;">Signals tracked</div></div>
            <div><div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:{_wc};">{wr}%</div><div style="font-size:11px;color:#4a5e7a;margin-top:2px;">Correct direction</div></div>
            <div><div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:{_ac};">{avg_s}</div><div style="font-size:11px;color:#4a5e7a;margin-top:2px;">Avg move since signal</div></div>
            <div><div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:{GREEN};">{best_s}</div><div style="font-size:11px;color:#4a5e7a;margin-top:2px;">Best signal</div></div>
        </div>
        <div style="font-size:10px;color:#2a3a52;margin-top:10px;">Equal-weight average of each tracked signal's measured move in its called direction (a short counts a decline as a gain). Educational only · not financial advice · past performance ≠ future results.</div>
    </div>''', unsafe_allow_html=True)
    return True

HERO_MIN_PRICE = float(os.environ.get("HERO_MIN_PRICE", "3"))  # marketing hero skips sub-$ penny names

def _hero_snaps(rows):
    """READ-ONLY per-ticker snapshots for the landing hero's real % since signal (cached per warm).
    The warm worker already RECORDS __universe__ snapshots, so the public landing only READS here —
    no writes on a page hit by crawlers. Prefers the category snapshot ('since signal'), falls back to
    the universe one. Returns {ticker: snap or None}."""
    try:
        with _UNIVERSE_LOCK:
            bid = _UNIVERSE_CACHE.get("built_at", 0)
    except Exception:
        bid = 0
    ck = (bid, tuple(r.get("t") for r in rows))
    if bid and st.session_state.get("_hero_snap_key") == ck and "_hero_snap" in st.session_state:
        return st.session_state["_hero_snap"]
    snaps = {}
    try:
        alls = _load_recs()
        for r in rows:
            t = r.get("t"); cat = r.get("primary_cat")
            snaps[t] = (alls.get(_rec_key(cat, t)) if cat else None) or alls.get(_rec_key("__universe__", t))
    except Exception:
        pass
    if bid:
        st.session_state["_hero_snap"] = snaps; st.session_state["_hero_snap_key"] = ck
    return snaps


def page_landing():
    # NAV_CSS is injected by render_topbar() below — injecting it here too added an extra empty
    # stMarkdown element whose vertical-block gap (~16px) pushed the landing's topbar 16px lower
    # than every other page, causing the header to jump/flash on landing<->page navigation.
    # ── Landing page uses render_topbar for consistent in-app navigation ──
    render_topbar()  # guest state (not authed), shows Features/Pricing/Login/Sign Up
    track_once("_lv_tracked", "landing_view")   # funnel top (once per session)

    # ── HERO — tighter layout, left-aligned copy + right preview close together ──
    st.markdown(f"""
    <style>
    .hero-wrap {{
        max-width:1280px; width:100%; margin:0 auto;
        padding:32px 24px 0;
    }}
    /* Remove Streamlit column gap artifacts; TOP-align so the copy and the preview
       line up at the headline rather than floating centered against each other. */
    .hero-wrap [data-testid="stHorizontalBlock"] {{ gap:0 !important; align-items:flex-start !important; }}
    .hero-wrap [data-testid="column"]:first-child {{ padding-right:8px !important; }}
    .hero-wrap [data-testid="column"]:last-child  {{ padding-left:8px !important; padding-top:30px !important; }}
    @media(max-width:900px){{ .hero-wrap [data-testid="column"]:last-child {{ padding-top:6px !important; }} }}
    /* Hero typography. Weights are deliberately one step lighter than they were: the
       headline sat at 900 with -2px tracking, which crushed the letters together, and the
       sub-copy inherited a semibold from Streamlit's base so the whole block read as one
       undifferentiated slab of bold. 800/400 with looser tracking gives the headline its
       emphasis back BY CONTRAST with the body text, rather than by shouting alongside it. */
    .hero-eyebrow {{ font-size:10px; font-weight:700; color:{BLUE}; letter-spacing:2.2px; text-transform:uppercase; margin-bottom:14px; }}
    .hero-h1 {{ font-size:44px !important; font-weight:800; color:#f1f5f9; line-height:1.1; letter-spacing:-1.1px; margin:0 0 16px; }}
    .hero-h1 .hi {{ color:{BLUE}; }}
    .hero-h1 .hg {{ color:{GOLD}; }}
    .hero-sub {{ font-size:15.5px; font-weight:400 !important; color:#8195b3; line-height:1.72;
        letter-spacing:0; margin:0 0 24px; max-width:430px; }}
    .hero-sub b, .hero-sub strong {{ font-weight:600 !important; color:#a5b4fc; }}
    /* CTA buttons - constrained width */
    .hero-cta-wrap .stButton>button {{
        max-width:400px !important;
        font-size:14px !important;
        font-weight:700 !important;
        height:46px !important;
        min-height:46px !important;
        border-radius:9px !important;
    }}
    .hero-cta-secondary .stButton>button {{
        background:rgba(255,255,255,0.05) !important;
        border:1px solid rgba(255,255,255,0.15) !important;
        color:#b8cce0 !important;
        max-width:400px !important;
        height:42px !important;
        min-height:42px !important;
    }}
    /* Trust line */
    .hero-trust {{ font-size:11px; color:#4a5e7a; display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; align-items:center; }}
    /* Mobile */
    @media(max-width:900px) {{
        .hero-wrap {{ padding:18px 14px 0; }}
        .hero-h1 {{ font-size:30px !important; letter-spacing:-1px !important; }}
        .hero-sub {{ font-size:13px; }}
    }}
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="hero-wrap">', unsafe_allow_html=True)
    hl, hr = st.columns([4, 6], gap="small")

    with hl:
        st.markdown(f"""
        <div style="padding:12px 0 16px;">
            <div class="hero-eyebrow">Whole-Market Signal Engine</div>
            <div class="hero-h1">Every setup in<br>the market,<br><span class="hi">ranked by</span> <span class="hg">conviction.</span></div>
            <div class="hero-sub">Every session we score ~2,500 of the most liquid U.S. stocks on price action, SEC insider filings, real short interest and money flow. Each one gets a single <b>Conviction Score</b> from 0 to 100, so the strongest setups surface first.</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div class="hero-cta-wrap">', unsafe_allow_html=True)
        if st.button("Start free →", key="h_su", type="primary", use_container_width=True):
            nav("signup")
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div style="text-align:left;font-size:12px;color:#6b7fa0;padding:10px 0 4px;">Already have an account?</div>', unsafe_allow_html=True)

        st.markdown('<div class="hero-cta-secondary">', unsafe_allow_html=True)
        if st.button("Sign in", key="h_login", use_container_width=True):
            nav("login")
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("""
        <div class="hero-trust">
            <span>✓ Free forever</span>
            <span style="color:#2a3a52;">·</span>
            <span>✓ No credit card</span>
            <span style="color:#2a3a52;">·</span>
            <span>✓ Live market + SEC data</span>
        </div>
        """, unsafe_allow_html=True)

    with hr:
        # LIVE Top Signals preview — real, conviction-ranked rows from the warm universe.
        # NOTHING is fabricated: conviction is live, a sub-stat shows ONLY when the data is
        # real (insider Form-4 buys or FINRA days-to-cover), and "% since signal" is the TRUE
        # move from the locked entry snapshot. Honest "warming up" state when the scan hasn't
        # completed — never placeholder cards with made-up numbers.
        #
        # Three deliberate choices here, all visible to logged-out visitors and crawlers:
        #  1. TICKERS ARE REDACTED — masked to block glyphs server-side. Crisp, not blurred:
        #     a blurred symbol reads as a rendering fault, solid blocks read as deliberate.
        #     The names ARE the product; the landing proves the engine works without giving
        #     the day's picks away, and because the symbol never reaches the DOM, view-source
        #     and crawlers can't lift it either.
        #  2. FOUR SECTIONS — long and short, each split into "best since signal" (the
        #     accumulated move from the locked entry, which may be weeks old) and "moving
        #     today" (this session's move). Those are different claims and the panel must not
        #     blur them together. The header pill carries the day's overall long/short split
        #     across every signal, not just the rows shown.
        #  3. EACH SIGNAL APPEARS ONCE. Tickers are de-duped ACROSS all four sections, and the
        #     list is static — it used to be a CSS marquee that rendered the cards twice over
        #     to fake a seamless loop, so every signal visibly appeared twice.
        def _sigrow(mask, metric, metric_col, cat, sub, sc, accent):
            """One compact signal row. Ten of these have to fit the panel, so it's a single
            line: redacted ticker, category, conviction bar + score, then the metric for
            whichever section it sits in (% since signal, or % today)."""
            return (f'<div class="sig">'
                    f'<span class="tlock">{_lock_svg(11)}</span>'
                    f'<span class="tick">{mask}</span>'
                    f'<span class="tag" style="color:{accent};">{cat_icon(cat, 13)}'
                    f'<span>{_clean_name(cat)}<span class="sub">{sub}</span></span></span>'
                    f'<div class="bar"><div class="fill" style="width:{max(4, min(100, sc))}%;'
                    f'background:linear-gradient(90deg,{accent}88,{accent});"></div></div>'
                    f'<span class="num" style="color:{accent};">{sc}</span>'
                    f'<span class="met" style="color:{metric_col};">{metric}</span></div>')
        def _hero_sub(r):
            # Days-to-cover is deliberately NOT shown here. On the single-line hero rows it
            # was long enough to push the category name into an ellipsis ("Breakdown · 16.1
            # days to c…"), and it's jargon that means nothing to a first-time visitor. It
            # still appears on the in-app cards, where there's room and context for it.
            info = r.get("info") or {}
            ib = int(info.get("insider_buys") or 0)
            return f" · {ib} insider buys" if ib >= 2 else ""
        _HERO_SINCE_N, _HERO_TODAY_N = 3, 2      # rows per "since signal" / "today" section
        _sec_long_since = _sec_long_today = _sec_short_since = _sec_short_today = []
        _snaps = {}
        _n_long = _n_short = 0
        try:
            _warm = build_scored_universe()          # instant (cache-served, non-blocking)
            if _warm:
                # A "signal" is any warm row the engine assigned a primary category. The
                # overall split below counts EVERY one of them — it's the day's real
                # long/short breakdown, not a summary of the handful of rows on screen.
                _sig_rows = [r for r in _warm if r.get("primary_cat")]
                _short_rows = [r for r in _sig_rows if category_dir(r.get("primary_cat", "")) == "bear"]
                _long_rows  = [r for r in _sig_rows if category_dir(r.get("primary_cat", "")) != "bear"]
                _n_short, _n_long = len(_short_rows), len(_long_rows)

                def _by_cat(rows):
                    g = {}
                    for r in rows:
                        g.setdefault(r.get("primary_cat"), []).append(r)
                    return g

                # Sample every category before ranking (see _signal_candidates), then drop
                # penny names — the hero is marketing and a $0.40 name reads badly.
                def _cands(rows, per_cat, cap):
                    return [r for r in _signal_candidates(_by_cat(rows), per_cat=per_cat, cap=cap)
                            if ((r.get("q") or {}).get("price", 0) or 0) >= HERO_MIN_PRICE]
                _cand_l = _cands(_long_rows, 3, 90)
                _cand_s = _cands(_short_rows, 8, 40)   # only 3 bear categories exist — go deeper
                _snaps = _hero_snaps(_cand_l + _cand_s)

                # One shared `seen` set across all four sections: a pick that is both a top
                # performer since signal AND a big mover today must appear once, not twice.
                _seen_t = set()
                _sec_long_since  = _top_since_signal(_cand_l, _snaps, _HERO_SINCE_N,
                                                     per_cat=1, seen_tickers=_seen_t)
                _sec_long_today  = _top_movers_today(_cand_l, _HERO_TODAY_N,
                                                     per_cat=1, seen_tickers=_seen_t)
                _sec_short_since = _top_since_signal(_cand_s, _snaps, _HERO_SINCE_N,
                                                     per_cat=2, seen_tickers=_seen_t)
                _sec_short_today = _top_movers_today(_cand_s, _HERO_TODAY_N,
                                                     per_cat=2, seen_tickers=_seen_t)
        except Exception:
            _sec_long_since = _sec_long_today = _sec_short_since = _sec_short_today = []

        def _hero_row(r, mode):
            """Row → _sigrow args. `mode` decides which number the row carries, and the two
            are never interchangeable: 'since' is the accumulated move from the locked entry
            snapshot, 'today' is this session's move. Both are measured in the signal's own
            direction, so a short's decline reads positive. Nothing is fabricated — a row
            can only reach the 'since' sections if it HAS a snapshot to measure."""
            cat = r.get("primary_cat", "") or ""
            conv = int(r.get("conviction") or r.get("sc") or 0)
            mask = "█" * max(3, min(5, len(r.get("t", "") or "")))
            if mode == "since":
                snap = _snaps.get(r.get("t")) or {}
                val, _ = _since_signal_pct(cat, snap.get("entry_price", 0),
                                           (r.get("q") or {}).get("price", 0))
                suffix = "since"
            else:
                val, suffix = _today_pct(r), "today"
            val = 0.0 if val is None else val
            metric = f'{"+" if val >= 0 else ""}{val:.1f}% {suffix}'
            metric_col = "#34d399" if val >= 0 else "#fb7185"
            return (mask, metric, metric_col, cat, _hero_sub(r), conv, cat_accent(cat))

        def _hero_section(rows, label, tone, mode, count_html="", empty_msg="", cls=""):
            """One labelled block: a rule + its rows, or an honest note when it's empty."""
            head = (f'<div class="sec {cls}"><span class="sec-l" style="color:{tone};">{label}</span>'
                    f'<span class="sec-r"></span><span class="sec-n">{count_html}</span></div>')
            if not rows:
                return head + f'<div class="sec-empty">{empty_msg}</div>'
            return head + "".join(_sigrow(*_hero_row(r, mode)) for r in rows)

        _is_live = bool(_sec_long_since or _sec_long_today or _sec_short_since or _sec_short_today)
        if _is_live:
            _dot = '<span class="ldot"></span>'
            _tot = _n_long + _n_short
            # The header pill IS the overall split — today's whole signal set, both books.
            _ppill = (f'{round(_n_long * 100 / _tot)}% long · {round(_n_short * 100 / _tot)}% short'
                      if _tot else 'Live · Conviction-ranked')
            _feed = ('<div class="feedwrap">'
                     + _hero_section(_sec_long_since, "▲ Long · best since signal", "#34d399", "since",
                                     f"{_n_long:,} long today",
                                     "No long pick has a measured gain since its signal yet.")
                     + _hero_section(_sec_long_today, "▲ Long · moving today", "#34d399", "today", "",
                                     "The long book is flat so far this session.", "s2")
                     + _hero_section(_sec_short_since, "▼ Short · best since signal", "#fb7185", "since",
                                     f"{_n_short:,} short today",
                                     "No short pick has a measured gain since its signal yet.", "s2")
                     + _hero_section(_sec_short_today, "▼ Short · moving today", "#fb7185", "today", "",
                                     "No short setups are working today — the engine only flags "
                                     "the bear book when the setups are actually there.", "s2")
                     + '</div>')
        else:
            _dot = '<span class="ldot" style="background:#5d6b86;animation:none;"></span>'; _ppill = 'Warming up…'
            _feed = ('<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;'
                     'color:#5d6b86;font-size:12px;text-align:center;gap:8px;padding:24px;"><div style="font-size:22px;">⏳</div>'
                     '<div>Today\'s scan is warming up.<br>Live signals appear here in a moment.</div></div>')
        hero_comp = f"""
        <style>
        html,body{{margin:0;padding:0;height:100%;background:transparent;font-family:Inter,-apple-system,sans-serif;overflow:hidden;}}
        .panel{{background:linear-gradient(160deg,#0e1530,#0a0e1c);border:1px solid #1c2440;border-radius:16px;
            padding:15px 15px 13px;box-shadow:0 26px 64px rgba(0,0,0,.5);height:100%;box-sizing:border-box;
            display:flex;flex-direction:column;}}
        .phead{{display:flex;align-items:center;justify-content:space-between;margin-bottom:11px;}}
        .ptitle{{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:800;color:#f4f7ff;letter-spacing:.2px;}}
        .ldot{{width:8px;height:8px;border-radius:50%;background:#34d399;animation:pulse 2s infinite;}}
        @keyframes pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(52,211,153,.55);}}50%{{box-shadow:0 0 0 6px rgba(52,211,153,0);}}}}
        .ppill{{font-size:10px;font-weight:700;color:#a5b4fc;background:rgba(99,102,241,.12);
            border:1px solid rgba(99,102,241,.28);border-radius:999px;padding:3px 10px;}}
        /* Static list — no marquee. The old version animated a DOUBLED copy of the cards
           to fake a seamless loop, which showed every signal twice. */
        .feedwrap{{flex:1;min-height:0;overflow:hidden;}}
        /* ── Section header (one per direction × timeframe) ── */
        .sec{{display:flex;align-items:center;gap:9px;margin:0 0 6px;}}
        .sec.s2{{margin-top:10px;}}
        .sec-l{{font-size:9.5px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;flex-shrink:0;}}
        .sec-r{{flex:1;height:1px;background:rgba(255,255,255,.08);}}
        .sec-n{{font-size:9.5px;font-weight:600;color:#5d6b86;flex-shrink:0;}}
        .sec-empty{{font-size:10.5px;color:#5d6b86;line-height:1.5;background:#0c1322;
            border:1px dashed #1a2740;border-radius:9px;padding:7px 11px;margin-bottom:6px;}}
        /* ── Signal row — one line, so ten fit. Surfaces match the in-app conviction cards
              (same gradient, border and radius) so the hero reads as the same product. ── */
        .sig{{display:flex;align-items:center;gap:9px;
            background:linear-gradient(135deg,#0d1525,#0a0f1a);border:1px solid #1c2942;
            border-radius:10px;padding:7px 11px;margin-bottom:6px;}}
        /* Redacted ticker: block glyphs, NOT the real symbol (see _hero_row). Rendered
           crisp — a blurred symbol reads as a rendering fault or a half-loaded skeleton,
           whereas solid blocks read as a deliberate redaction. */
        .tick{{font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:800;
            color:#35455f;letter-spacing:1.5px;user-select:none;flex-shrink:0;}}
        .tlock{{display:inline-flex;color:#41527a;flex-shrink:0;}}
        .tag{{display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;
            min-width:0;flex:0 1 172px;}}
        .tag>span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
        .tag .msp-ic{{flex-shrink:0;}}   /* colour inherits the per-theme accent */
        .sub{{color:#5d6b86;font-weight:600;}}
        .bar{{flex:1 1 auto;height:7px;background:rgba(255,255,255,.06);border-radius:4px;
            overflow:hidden;min-width:26px;}}
        .fill{{height:7px;border-radius:4px;}}
        .num{{font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:800;
            min-width:20px;text-align:right;flex-shrink:0;}}
        .met{{font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:800;
            min-width:80px;text-align:right;flex-shrink:0;}}
        .pfoot{{font-size:10.5px;color:#5d6b86;text-align:center;margin-top:9px;}}
        .msp-ic{{vertical-align:-3px;}}
        </style>
        <div class="panel">
          <div class="phead">
            <div class="ptitle">{_dot} Top Signals</div>
            <div class="ppill">{_ppill}</div>
          </div>
          {_feed}
          <div class="pfoot">🔒 Tickers hidden on the public preview · scored from live market
          data, SEC filings &amp; short interest</div>
        </div>
        """
        # Sized for the worst case: 4 section headers + 10 single-line rows. The panel no
        # longer scrolls, so anything that overflows is silently clipped rather than reachable.
        _html_iframe(hero_comp, 560)   # isolated iframe (sets html/body + generic classes)

    st.markdown('</div>', unsafe_allow_html=True)  # close hero-wrap

    # ── Trust bar ──
    st.markdown(f"""
    <style>
    .sw-trust-bar {{
        background:#080b14;
        border-top:1px solid {BORDER};
        border-bottom:1px solid {BORDER};
        padding:20px 48px;
        display:grid;
        grid-template-columns: repeat(4, 1fr) auto;
        gap:24px;
        align-items:center;
    }}
    .sw-trust-stat {{
        display:flex;
        align-items:center;
        gap:10px;
    }}
    .sw-trust-stat-icon {{ font-size:18px; flex-shrink:0; }}
    .sw-trust-stat-num {{
        font-family:'JetBrains Mono',monospace;
        font-size:20px;
        font-weight:700;
        color:#e2e8f0;
        line-height:1.1;
    }}
    .sw-trust-stat-lbl {{
        font-size:11px;
        color:#2a3a52;
        line-height:1.3;
    }}
    .sw-trust-traders {{
        display:flex;
        align-items:center;
        gap:8px;
        justify-self:end;
    }}
    /* Mobile: 2x2 grid, centered, traders moves below */
    @media (max-width:992px) {{
        .sw-trust-bar {{
            grid-template-columns: 1fr 1fr !important;
            padding:18px 16px !important;
            gap:16px !important;
        }}
        .sw-trust-stat {{
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 10px;
            padding: 12px 14px;
            justify-content: flex-start;
        }}
        .sw-trust-stat-num {{ font-size: 18px !important; }}
        .sw-trust-traders {{
            grid-column: 1 / -1;
            justify-self: center !important;
            background: rgba(245,158,11,0.08);
            border: 1px solid rgba(245,158,11,0.2);
            border-radius: 10px;
            padding: 8px 14px;
            margin-top: 4px;
        }}
    }}
    </style>
    <div class="sw-trust-bar">
        <div class="sw-trust-stat"><div>
            <div class="sw-trust-stat-num">~2,500</div>
            <div class="sw-trust-stat-lbl">Stocks scored each session</div>
        </div></div>
        <div class="sw-trust-stat"><div>
            <div class="sw-trust-stat-num">20</div>
            <div class="sw-trust-stat-lbl">Unique signal categories</div>
        </div></div>
        <div class="sw-trust-stat"><div>
            <div class="sw-trust-stat-num">0–100</div>
            <div class="sw-trust-stat-lbl">Conviction Score per stock</div>
        </div></div>
        <div class="sw-trust-stat"><div>
            <div class="sw-trust-stat-num">$0</div>
            <div class="sw-trust-stat-lbl">To get started</div>
        </div></div>
        <div class="sw-trust-traders">
            <span style="font-size:11px;color:#2a3a52;">Powered by</span>
            <span style="font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:700;color:#a5b4fc;">Polygon · SEC EDGAR · FINRA · FRED</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("<br>",unsafe_allow_html=True)

    # ── Live signal track-record proof, right under the trust bar (social proof) ──
    render_signal_proof("landing")

    # ── Auto-scrolling feature ticker (continuous CSS marquee; advertises the edges) ──
    _tk = [
        ("🌊 Momentum Leaders", "20 unique signals"),
        ("🏆 Relative Strength", "~2,500 stocks scored each session"),
        ("🏛️ Insider Cluster", "SEC insider cluster buys"),
        ("🔥 Short Squeeze", "Real FINRA days-to-cover"),
        ("🎪 Catalyst / Gap", "Fresh SEC 8-K catalysts"),
        ("💥 Volatility Expansion", "Live market-regime read"),
        ("🦈 Quiet Accumulation", "Money-flow accumulation"),
        ("💎 Value Momentum", "Conviction-ranked Top Signals"),
    ]
    _tk_html = "".join(
        f'<span class="ftk-item">{cat_icon(c,15)}<span>{lbl}</span></span><span class="ftk-dot"></span>'
        for c, lbl in _tk)
    st.markdown(f"""
    <style>
    .ftk-wrap{{overflow:hidden;border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};
        background:#080b14;padding:13px 0;margin:4px 0 6px;
        -webkit-mask:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent);
        mask:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent);}}
    .ftk{{display:flex;align-items:center;width:max-content;animation:ftkscroll 34s linear infinite;}}
    .ftk-wrap:hover .ftk{{animation-play-state:paused;}}
    @keyframes ftkscroll{{from{{transform:translateX(0);}}to{{transform:translateX(-50%);}}}}
    .ftk-item{{display:inline-flex;align-items:center;gap:9px;font-size:13px;font-weight:700;color:#aebbd0;white-space:nowrap;margin-right:12px;}}
    .ftk-item svg{{color:#818cf8;flex-shrink:0;}}
    .ftk-dot{{width:5px;height:5px;border-radius:50%;background:rgba(99,102,241,.45);margin-right:12px;flex-shrink:0;}}
    </style>
    <div class="ftk-wrap"><div class="ftk">{_tk_html}{_tk_html}</div></div>
    """, unsafe_allow_html=True)
    st.markdown("<br>",unsafe_allow_html=True)

    # ── THE EDGE — what the product actually is (new positioning, custom icons) ──
    st.markdown(f"""
    <style>
    .edge-head{{max-width:1180px;margin:6px auto 22px;padding:0 48px;text-align:center;}}
    .edge-eyebrow{{font-size:10px;font-weight:800;color:{BLUE};letter-spacing:2.5px;text-transform:uppercase;margin-bottom:10px;}}
    .edge-h2{{font-size:30px;font-weight:900;color:#f1f5f9;letter-spacing:-1px;line-height:1.12;margin:0 auto;max-width:680px;}}
    .edge-sub{{font-size:14px;color:#5d6b86;line-height:1.6;max-width:600px;margin:12px auto 0;}}
    .edge-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;padding:0 48px;max-width:1180px;margin:0 auto;}}
    @media(max-width:992px){{.edge-grid{{grid-template-columns:1fr;padding:0 16px;}}.edge-head{{padding:0 16px;}}.edge-h2{{font-size:24px;}}}}
    .edge-card{{background:linear-gradient(160deg,#0e1530,#0a0e1c);border:1px solid #1c2440;border-radius:14px;padding:22px;transition:border-color .15s ease,transform .15s ease;}}
    .edge-card:hover{{border-color:rgba(99,102,241,0.5);transform:translateY(-2px);}}
    .edge-ic{{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.28);color:#a5b4fc;margin-bottom:14px;}}
    .edge-t{{font-size:16px;font-weight:800;color:#f4f7ff;margin-bottom:7px;letter-spacing:-.2px;}}
    .edge-d{{font-size:13px;color:#5d6b86;line-height:1.65;}}
    </style>
    <div class="edge-head reveal">
        <div class="edge-eyebrow">Why Scanviction</div>
        <div class="edge-h2">An edge built from data most screeners don't touch.</div>
        <div class="edge-sub">Not another red-and-green screener — a whole-market engine that fuses price action with the filings, short interest and money flow that actually move stocks.</div>
    </div>
    <div class="edge-grid">
      <div class="edge-card reveal"><div class="edge-ic">{cat_icon('🌊 Momentum Leaders',24)}</div>
        <div class="edge-t">20 distinct signals</div>
        <div class="edge-d">Momentum, breakouts, squeezes, reversals, insider clusters, volume dry-ups and more — each a unique, non-overlapping setup, so a flagged stock means one clear thing.</div></div>
      <div class="edge-card reveal"><div class="edge-ic">{_svg('<path d="M3 17a9 9 0 0 1 18 0"/><path d="M12 17l5-5"/><circle cx="12" cy="17" r="1.6"/>',24)}</div>
        <div class="edge-t">One Conviction Score</div>
        <div class="edge-d">Six independent edges — technical, money flow, relative strength, trend, squeeze fuel and valuation — blended into a single, comparable 0–100 number.</div></div>
      <div class="edge-card reveal"><div class="edge-ic">{_svg('<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',24)}</div>
        <div class="edge-t">The whole market, every session</div>
        <div class="edge-d">~2,500 of the most liquid U.S. stocks scored continuously on live Polygon market data — not a hand-picked watchlist of the usual names.</div></div>
      <div class="edge-card reveal"><div class="edge-ic">{cat_icon('🏛️ Insider Cluster',24)}</div>
        <div class="edge-t">Insider &amp; catalyst intelligence</div>
        <div class="edge-d">Open-market insider cluster buys and fresh SEC 8-K filings, pulled straight from EDGAR — the conviction smart money acts on, surfaced automatically.</div></div>
      <div class="edge-card reveal"><div class="edge-ic">{cat_icon('🔥 Short Squeeze',24)}</div>
        <div class="edge-t">Real short-squeeze fuel</div>
        <div class="edge-d">Actual FINRA days-to-cover and short-volume — not a technical guess — so squeezes are flagged on the data that genuinely drives them.</div></div>
      <div class="edge-card reveal"><div class="edge-ic">{_svg('<circle cx="12" cy="13" r="8"/><path d="M12 13l4-4"/><circle cx="12" cy="13" r="1.5"/><path d="M12 5V3"/>',24)}</div>
        <div class="edge-t">Read in market context</div>
        <div class="edge-d">A live risk-on / risk-off regime from VIX and credit spreads tells you which setups tend to work right now — so a signal is never read in a vacuum.</div></div>
    </div>
    """, unsafe_allow_html=True)

    # Go Premium CTA
    st.markdown("""
    <style>
    button[aria-label="👑 Unlock Premium Access — Start Today"] {
        background: linear-gradient(135deg,#92400e,#d97706,#f59e0b,#fcd34d) !important;
        border: 1px solid #f59e0b !important; color: #1a0800 !important;
        font-weight: 800 !important; font-size: 16px !important;
        min-height: 56px !important;border-radius: 12px !important;
        box-shadow: 0 8px 32px rgba(245,158,11,0.45) !important;
        letter-spacing: 0.3px !important;
    }
    button[aria-label="👑 Unlock Premium Access — Start Today"]:hover {
        box-shadow: 0 12px 48px rgba(245,158,11,0.65) !important;
        transform: translateY(-2px) !important;
    }
    </style>
    <div style="padding:32px 48px 8px;text-align:center;">
        <div style="font-size:13px;color:#5d6b86;margin-bottom:16px;">Start free and explore every free signal — upgrade only when you want the premium categories, screener and analytics · Cancel anytime · No credit card</div>
    </div>
    """, unsafe_allow_html=True)
    _,cta,_=st.columns([1,4,1])
    with cta:
        if st.button("👑 Unlock Premium Access — Start Today",key="land_prem",type="primary",use_container_width=True): nav("pricing")

    st.markdown("<br>",unsafe_allow_html=True)

    # ── Signal categories showcase — the current 20, with the app's custom icons ──
    cat_cards_html = ""
    for cat,(desc,tier) in COMPOSITE_CATS.items():
        badge = '<span class="lc-badge lc-pro">PRO</span>' if tier=="premium" else '<span class="lc-badge lc-free">FREE</span>'
        cat_cards_html += (
            f'<div class="lc-cat reveal"><div class="lc-cat-h"><span class="lc-cat-ic">{cat_icon(cat,18)}</span>'
            f'<span class="lc-cat-n">{_clean_name(cat)}</span>{badge}</div>'
            f'<div class="lc-cat-d">{cat_def(cat)}</div></div>')

    st.markdown(f"""
    <style>
    .lc-cat-hdr{{max-width:1120px;margin:0 auto 16px;padding:0 48px;text-align:center;}}
    .lc-cat-grid{{max-width:1120px;margin:0 auto;width:100%;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 12px;padding:0 48px;}}
    @media(max-width:900px){{.lc-cat-grid{{grid-template-columns:repeat(2,minmax(0,1fr));padding:0 16px;}}.lc-cat-hdr{{padding:0 16px;}}}}
    @media(max-width:560px){{.lc-cat-grid{{grid-template-columns:1fr;}}}}
    .lc-cat{{background:#0c1322;border:1px solid #1a2740;border-radius:11px;padding:13px 15px;min-height:80px;box-sizing:border-box;transition:border-color .15s ease;}}
    .lc-cat:hover{{border-color:rgba(99,102,241,0.45);}}
    .lc-cat-h{{display:flex;align-items:center;gap:8px;margin-bottom:6px;}}
    .lc-cat-ic{{display:inline-flex;color:#818cf8;flex-shrink:0;}}
    .lc-cat-n{{font-size:13px;font-weight:800;color:#ffffff;letter-spacing:.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .lc-badge{{margin-left:auto;font-size:9px;font-weight:800;padding:2px 7px;border-radius:5px;white-space:nowrap;flex-shrink:0;}}
    .lc-pro{{background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.3);}}
    .lc-free{{background:rgba(34,197,94,.1);color:#4ade80;border:1px solid rgba(34,197,94,.3);}}
    .lc-cat-d{{font-size:11px;color:#5d6b86;line-height:1.5;}}
    </style>
    <div class="lc-cat-hdr reveal">
        <div style="font-size:26px;font-weight:900;color:#f1f5f9;letter-spacing:-.7px;margin-bottom:8px;">23 signals. Zero overlap.</div>
        <div style="font-size:13px;color:#5d6b86;line-height:1.6;max-width:640px;margin:0 auto;">Each category is a distinct, multi-factor setup — built from price action, money flow, short interest, insider filings and valuation. Every stock lands in only its single best-fit category, so the list is signal, not noise.</div>
    </div>
    <div class="lc-cat-grid">{cat_cards_html}</div>
    """, unsafe_allow_html=True)

    _,pc,_=st.columns([2,1,2])
    with pc:
        if st.button("Explore all signals →",key="land_cats",type="primary",use_container_width=True): nav("signup")

    st.markdown("<br>",unsafe_allow_html=True)

    # -- How it works -- 3 steps (honest, sells the engine) --
    st.markdown(f"""
    <style>
    .hiw-head{{text-align:center;max-width:1180px;margin:8px auto 22px;padding:0 48px;}}
    .hiw-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:1100px;margin:0 auto;padding:0 48px;}}
    @media(max-width:992px){{.hiw-grid{{grid-template-columns:1fr;padding:0 16px;}}.hiw-head{{padding:0 16px;}}}}
    .hiw-card{{background:linear-gradient(160deg,#0e1530,#0a0e1c);border:1px solid #1c2440;border-radius:14px;padding:24px 22px;}}
    .hiw-step{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:800;color:#818cf8;background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.28);width:32px;height:32px;border-radius:9px;display:flex;align-items:center;justify-content:center;margin-bottom:14px;}}
    .hiw-t{{font-size:17px;font-weight:800;color:#f4f7ff;margin-bottom:7px;letter-spacing:-.2px;}}
    .hiw-d{{font-size:13px;color:#5d6b86;line-height:1.65;}}
    </style>
    <div class="hiw-head reveal">
        <div class="edge-eyebrow">How it works</div>
        <div class="edge-h2">From the whole market to your best setups, in one pass.</div>
    </div>
    <div class="hiw-grid">
      <div class="hiw-card reveal"><div class="hiw-step">1</div>
        <div class="hiw-t">We scan the market</div>
        <div class="hiw-d">Every session we pull ~2,500 of the most liquid U.S. stocks together with their SEC filings, FINRA short interest and money-flow data.</div></div>
      <div class="hiw-card reveal"><div class="hiw-step">2</div>
        <div class="hiw-t">We score every setup</div>
        <div class="hiw-d">Each stock is matched to its single best-fit signal and given a 0-100 Conviction Score - so strength is instantly comparable across the entire market.</div></div>
      <div class="hiw-card reveal"><div class="hiw-step">3</div>
        <div class="hiw-t">You see the best, first</div>
        <div class="hiw-d">Top Signals surfaces the highest-conviction setups across every category - tap any card for the full, plain-English breakdown behind the score.</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<br>",unsafe_allow_html=True)

    # ── FAQ ──
    st.markdown('<div style="padding:0 48px;"><div class="sec-hd">FAQ</div>',unsafe_allow_html=True)
    for q,a in [
        ("Is this financial advice?","No. Scanviction is an educational analysis tool that produces algorithmic signals. Nothing here is financial, investment, legal or tax advice. Always do your own research and consult a licensed professional before making investment decisions."),
        ("What is the Conviction Score?","A single 0-100 number that blends six independent edges - technical strength, money flow, relative strength versus the market, trend strength, short-squeeze fuel and valuation (plus insider buying when it's present). Higher means a stronger, more confident setup, and it makes very different stocks directly comparable."),
        ("What are the signal categories?","20 distinct, non-overlapping setups - momentum, breakouts, volatility squeezes, reversals, capitulation bottoms, short squeezes, insider clusters, quiet accumulation and more. Each stock is assigned to only its single best-fit category, so a flagged name means one clear thing."),
        ("What data and markets do you cover?","The most liquid U.S. stocks (NASDAQ, NYSE, S&P 500, Russell and high-volume small caps), scored on live Polygon market data plus SEC EDGAR filings (insider Form 4 buys and 8-K catalysts), FINRA short interest and short volume, and a FRED-based market-regime read."),
        ("What is the difference between Free and Premium?","Free gives you the Top Signals feed, the free signal categories, the live market regime and your watchlist. Premium unlocks every category (including Insider Cluster and Short Squeeze), the advanced screener, full BI analytics, and unlimited watchlists and alerts."),
        ("Can I cancel anytime?","Yes. Premium is month-to-month - cancel anytime and keep access through the end of your billing period."),
    ]:
        with st.expander(q):
            st.markdown(f'<div style="font-size:13px;color:#374f6e;line-height:1.75;">{a}</div>',unsafe_allow_html=True)
    st.markdown('</div>',unsafe_allow_html=True)

    # ── Final sign-up CTA band ──
    st.markdown("""
    <div class="reveal" style="max-width:920px;margin:36px auto 10px;padding:0 24px;">
        <div style="background:linear-gradient(135deg,#141a3a,#0c1024);border:1px solid rgba(99,102,241,0.32);
            border-radius:18px;padding:34px 28px;text-align:center;box-shadow:0 22px 60px rgba(99,102,241,0.16);">
            <div style="font-size:28px;font-weight:900;color:#f4f7ff;letter-spacing:-.8px;margin-bottom:9px;">See the whole market, scored.</div>
            <div style="font-size:14px;color:#8da3c4;line-height:1.6;max-width:480px;margin:0 auto;">Free forever, no credit card — your first Top Signals are about sixty seconds away.</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    _, _fcta, _ = st.columns([1, 2, 1])
    with _fcta:
        if st.button("Start free →", key="land_final_cta", type="primary", use_container_width=True):
            nav("signup")
    st.markdown("<br>", unsafe_allow_html=True)

    render_footer()

# ─────────────────────────────────────────────────────────────
# PAGE: FEATURES
# ─────────────────────────────────────────────────────────────
def page_features():
    render_topbar()
    back_button("ft_back")
    st.markdown('<div class="page-wrap">' ,unsafe_allow_html=True)
    # CTA strip at top
    if not is_premium() and is_authed():
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#1a0d00,#0d1525);border:1px solid rgba(245,158,11,0.25);
                    border-radius:12px;padding:14px 20px;margin-bottom:20px;
                    display:flex;align-items:center;justify-content:space-between;">
            <div>
                <div style="font-size:14px;font-weight:700;color:{GOLD};">👑 Upgrade to unlock all premium features</div>
                <div style="font-size:12px;color:#374f6e;margin-top:2px;">All 23 categories · Squeeze scanner · Advanced Brief filters · Unlimited alerts</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if gold_btn("Unlock Premium — $19/mo", "feat_top_prem"): nav("pricing")
        st.markdown("<br>", unsafe_allow_html=True)
    elif not is_authed():
        st.markdown(f"""
        <div style="background:#0d1525;border:1px solid rgba(99,102,241,0.25);border-radius:12px;
                    padding:14px 20px;margin-bottom:20px;">
            <div style="font-size:14px;font-weight:700;color:#e2e8f0;">🚀 Start free — no credit card required</div>
            <div style="font-size:12px;color:#374f6e;margin-top:2px;">Create an account in 60 seconds and start discovering trading opportunities immediately.</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown(f"""
    <div style="text-align:center;padding:40px 0 32px;">
        <div style="font-size:11px;font-weight:700;color:{BLUE};letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;">Full Platform Overview</div>
        <div style="font-size:38px;font-weight:900;color:#f1f5f9;letter-spacing:-1.5px;margin-bottom:10px;">Everything in Scanviction</div>
        <div style="font-size:15px;color:#374f6e;max-width:560px;margin:0 auto;">Built for traders who want data-driven clarity — not noise. Every feature is designed to answer one question: <em>should I pay attention to this stock right now?</em></div>
    </div>
    """, unsafe_allow_html=True)

    features_data = [
        ("🎯","Proprietary Composite Scoring","17 unique composite categories combining RSI, MACD, volume, short interest, social sentiment, and Bollinger Bands into single actionable signals. Categories like '🔥💥 Squeeze + Buzz', '🌪️ Volatility Squeeze', and '🎯📊 Triple Lock' are only available on Scanviction. Each category has a specific multi-factor entry criterion that filters our full universe in real time.","All plans"),
        ("🟢","BUY / WATCH / AVOID Signals","Every stock gets a clear recommendation based on our scoring engine — STRONG BUY, BUY, SQUEEZE BUY, WATCH, HOLD/WAIT, or AVOID — with plain-English reasoning explaining exactly why the signal was triggered. No jargon. No unexplained scores.","All plans"),
        ("💡","Plain-English Technical Analysis","RSI, MACD, moving average crossovers, Bollinger Bands, and volume spikes all translated into conversational sentences. We explain what a Golden Cross means in terms a beginner understands while still giving experts the data they need.","All plans"),
        ("📡","Live Social Sentiment","Real-time StockTwits data showing bullish/bearish % for any stock, watchlist counts, and trending detection. Our composite categories use this data to find early momentum before price moves.","All plans"),
        ("📊","Market Overview Dashboard","Live index data (NASDAQ, S&P 500, DOW, VIX, Russell), sector performance heatmap, market pulse indicator, and top trending tickers in a single clean view.","All plans"),
        ("⭐","Smart Watchlist","Track your stocks with automatic daily scoring. Premium users get watchlist analytics showing average score, % in the green, risk distribution, and sentiment breakdown across holdings.","All plans (Premium: analytics)"),
        ("🔔","Price Alerts","Set price-above or price-below alerts for any ticker. Alerts are managed from your account settings and displayed in your dashboard.","All plans"),
        ("🔍","Advanced Brief filters","Narrow your Daily Brief to exactly the signals you care about: by category, by score, by direction (long/short) and by each category's tracked win rate, so you only see setups from the signal families that have actually been working.","Premium"),
        ("📐","Signal-on-the-Chart","Each stock's detail page draws the detected setup right on the candles — the breakout level, bull-flag pole + box, squeeze bands, breakdown, or reversal — alongside a conviction-score breakdown, recent alerts, and full analysis.","Premium"),
        ("💥","Short Squeeze Scanner","Dedicated scanner identifying stocks with high short float (>10%), high days-to-cover, and rising momentum. Filters by social trending and volume to find squeeze setups before they run.","Premium"),
        ("📉→📈","Deep Stock Reports","Full stock detail pages with 60-day price chart + MA20/MA50 overlaid, volume bar chart vs average, complete plain-English analysis, social sentiment bar, score breakdown, why-flagged section, and related stocks.","Premium (charts)"),
        ("🎪","Email Digest (Coming Q3 2026)","Daily or weekly digest of your top-scored watchlist stocks, new BUY signals, and trending composite category alerts delivered to your inbox. Configurable from account settings.","Premium"),
        ("🛠️","Admin Panel","Full user management (promote/demote roles, delete accounts), API configuration with Twelve Data integration, site analytics with simulated growth charts, data source health monitoring, and security checklist with Streamlit Secrets setup guide.","Admin/Owner"),
        ("🔑","Ranking Controls","Sort and filter any category by Scanviction Score, % change today, volume ratio, short float, or social sentiment. Drag-and-drop ranking priority controls for power users.","Premium"),
        ("🔐","Secure Authentication","Passwords stored as SHA-256 hashes. Credentials loaded exclusively from Streamlit Cloud Secrets — never hardcoded. Supports both flat secrets and [accounts] section format.","All plans"),
    ]

    for i,(icon,title,desc,tier) in enumerate(features_data):
        tc_="card-gold" if tier=="Premium" else "card-blue" if tier=="Admin/Owner" else "card"
        tier_c=GOLD if tier=="Premium" else "#818cf8" if tier=="Admin/Owner" else "#4ade80"
        tier_bg=f"rgba(245,158,11,.12)" if tier=="Premium" else "rgba(129,140,248,.12)" if tier=="Admin/Owner" else "rgba(74,222,128,.1)"
        st.markdown(f"""<div class="{tc_}" style="display:flex;gap:16px;align-items:flex-start;margin-bottom:8px;">
            <div style="font-size:24px;flex-shrink:0;padding-top:2px;">{icon}</div>
            <div style="flex:1;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
                    <div style="font-size:14px;font-weight:700;color:#e2e8f0;">{title}</div>
                    <span style="background:{tier_bg};color:{tier_c};font-size:9px;font-weight:700;padding:2px 8px;border-radius:20px;border:1px solid {tier_c}33;">{tier}</span>
                </div>
                <div style="font-size:13px;color:#374f6e;line-height:1.7;">{desc}</div>
            </div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>",unsafe_allow_html=True)
    _,cta,_=st.columns([1,2,1])
    with cta:
        if st.button("Start Free →",key="feat_su",type="primary",use_container_width=True): nav("signup")
        st.markdown("<br>",unsafe_allow_html=True)
        if gold_btn("Go Premium","feat_prem"): nav("pricing")
    st.markdown('</div>',unsafe_allow_html=True)
    render_footer()

# ─────────────────────────────────────────────────────────────
# AUTH PAGES
# ─────────────────────────────────────────────────────────────
def page_login():
    render_topbar()
    st.markdown('<div class="page-wrap">',unsafe_allow_html=True)
    _,cc,_=st.columns([1,2,1])
    with cc:
        # Suppress Streamlit's transient "Missing Submit Button" warning that flashes red while the
        # form's submit button streams in AFTER its inputs on a nav rerun (a cosmetic frontend
        # artifact — every form here DOES have a submit button). Scoped to THIS form via the keyed
        # container, and real errors are rendered OUTSIDE the form (below) so they still show.
        st.markdown("<style>.st-key-loginbox [data-testid='stForm'] [data-testid='stAlert']{display:none!important;}</style>", unsafe_allow_html=True)
        st.markdown(f'<div style="text-align:center;padding:36px 0 24px;"><div style="font-size:26px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Welcome Back 👋</div><div style="font-size:13px;color:#374f6e;">Sign in to your Scanviction account</div></div>',unsafe_allow_html=True)
        with st.container(key="loginbox"):
            with st.form("lf",clear_on_submit=False):
                email=st.text_input("Email address",label_visibility="visible")
                pw=st.text_input("Password",type="password",label_visibility="visible")
                _submit=st.form_submit_button("Sign In →",type="primary",use_container_width=True)
            if _submit:
                _ck=_client_key(); _lock=secguard.login_locked(email)
                if not email or not pw:
                    st.error("Please enter your email and password.")
                elif _lock:
                    st.error(f"Too many failed attempts. Try again in ~{max(1,_lock//60)} min.")
                elif not secguard.rate_ok("login_client",_ck,30,300):
                    st.error("Too many attempts from here — please wait a moment.")
                elif login(email,pw):
                    secguard.clear_login_fails(email)
                    st.session_state["_login_welcome"] = st.session_state.user.get("name","")
                    track_event("login_success", {"email": email})
                    intended = st.session_state.pop("_intended_page", None)   # honor intended destination
                    nav(intended if intended else "dashboard")
                else:
                    _l=secguard.note_login_fail(email)
                    st.error("Invalid email or password." + (f" Account temporarily locked for ~{max(1,_l//60)} min after repeated failures." if _l else ""))

        # Show hints based on whether secrets are configured
        has_secrets=False
        try: has_secrets=bool(st.secrets.get("owner_email","") or st.secrets.get("owner_pw_hash",""))
        except: pass

        # Demo-account hint ONLY on a non-production instance (no owner secrets). In production
        # (owner secrets set) show nothing — real users sign in with their own account.
        if not has_secrets:
            st.markdown(f'<div style="background:#080b14;border:1px solid {BORDER};border-radius:8px;padding:12px 14px;margin-top:12px;font-size:12px;color:#374f6e;"><span style="color:#a5b4fc;font-weight:600;">Demo accounts:</span><br><span style="font-family:\'JetBrains Mono\',monospace;">demo@scanviction.com</span> / <span style="font-family:\'JetBrains Mono\',monospace;">demo123</span><br><span style="font-family:\'JetBrains Mono\',monospace;">premium@scanviction.com</span> / <span style="font-family:\'JetBrains Mono\',monospace;">premium1</span></div>',unsafe_allow_html=True)

        st.markdown("<br>",unsafe_allow_html=True)
        bc1,bc2=st.columns(2,gap="small")
        with bc1:
            if st.button("Create Free Account →",key="l2s",use_container_width=True,type="primary"): nav("signup")
        with bc2:
            if st.button("Forgot password?",key="l2f",use_container_width=True): nav("forgot_pw")
        if st.button("← Back to Home",key="l2h",use_container_width=True): nav("landing")
    st.markdown('</div>',unsafe_allow_html=True)  # close page-wrap

def page_signup():
    render_topbar()
    st.markdown('<div class="page-wrap">',unsafe_allow_html=True)
    _,cc,_=st.columns([1,2,1])
    with cc:
        st.markdown('<div style="text-align:center;padding:36px 0 24px;"><div style="font-size:26px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Create Your Account 🚀</div><div style="font-size:13px;color:#374f6e;">Free forever. No credit card. No API keys.</div></div>',unsafe_allow_html=True)
        # Anti-bot layer: a honeypot field hidden from humans (bots that fill every input trip it)
        # + a min-fill-time gate (bots submit instantly) stamped when the form first renders. Rate
        # limits (per-client + a global circuit breaker) are applied in the submit handler below.
        st.markdown("<style>.st-key-su_hpbox{display:none!important;height:0;overflow:hidden;}</style>", unsafe_allow_html=True)
        st.session_state.setdefault("_su_render_ts", time.time())
        with st.form("sf"):
            name_col1, name_col2 = st.columns(2, gap="small")
            with name_col1:
                first_name = st.text_input("First name", placeholder="Jane", key="su_first")
            with name_col2:
                last_name  = st.text_input("Last name", placeholder="Doe", key="su_last")
            email=st.text_input("Email",placeholder="you@example.com")
            pw=st.text_input("Password",type="password",placeholder="Min 6 characters")
            pw2=st.text_input("Confirm password",type="password")
            agree=st.checkbox("I agree to the Terms of Service. I understand Scanviction is for educational purposes only and is not financial advice.")
            with st.container(key="su_hpbox"):   # honeypot — invisible to humans
                _hp_val=st.text_input("Company (leave this blank)",key="su_company",label_visibility="collapsed")
            _submit=st.form_submit_button("Create Free Account →",type="primary",use_container_width=True)
        if _submit:
            _too_fast=(time.time()-st.session_state.get("_su_render_ts",0))<secguard.SIGNUP_MIN_FILL_S
            _ok_rate,_rate_msg=secguard.signup_allowed(_client_key())
            if (not secguard.honeypot_ok(_hp_val)) or _too_fast:
                st.error("Something went wrong — please try again.")   # generic: don't tip off bots
            elif not _ok_rate:
                st.error(_rate_msg)
            elif not all([first_name, last_name, email, pw, pw2]): st.error("Please fill in all fields.")
            elif len(first_name)>60 or len(last_name)>60: st.error("Please shorten your name.")
            elif any(c in (first_name+last_name) for c in "<>{}"): st.error("Your name contains invalid characters.")
            elif not secguard.valid_email(email): st.error("Please enter a valid email address.")
            elif pw!=pw2: st.error("Passwords don't match.")
            elif len(pw)<6: st.error("Password must be 6+ characters.")
            elif not agree: st.error("Please agree to the Terms of Service.")
            else:
                ok,msg=signup(email, pw, first_name, last_name)
                if ok:
                    track_event("signup_started", {"email": email})
                    full_name = f"{first_name} {last_name}".strip()
                    code=str(random.randint(100000,999999))
                    st.session_state["_verify_code"]=code
                    st.session_state["_verify_email"]=email
                    st.session_state["_verify_user"]={"name":full_name}
                    st.session_state.pop("user",None); st.session_state.pop("role",None)  # require verify first
                    ok2,info=_send_verification_email_bg(email,code)  # fired in the BG so signup returns instantly
                    if not ok2 and info and info.startswith("DEMO_CODE:"):
                        st.session_state["_demo_code"]=info.split(":",1)[1]
                    nav("verify_email")
                else: st.error(msg)
        if st.button("Already have an account? Sign In",key="s2l",use_container_width=True): nav("login")
    st.markdown('</div>', unsafe_allow_html=True)

def _send_telegram(chat_id, message):
    """Send a Telegram message via the Scanviction bot. Returns (True, None) or (False, error)."""
    try:
        bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token or not chat_id:
            return False, "TELEGRAM_NOT_CONFIGURED"
        import requests as _r
        resp = _r.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.status_code == 200:
            return True, None
        return False, f"Telegram error {resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, str(e)

def _send_push_notification(player_ids, title, message, url=None):
    """Send a OneSignal web/mobile push notification. player_ids: list of OneSignal subscription IDs.
    Returns (True, None) or (False, error)."""
    try:
        app_id  = st.secrets.get("ONESIGNAL_APP_ID", "")
        api_key = st.secrets.get("ONESIGNAL_REST_API_KEY", "")
        if not app_id or not api_key:
            return False, "ONESIGNAL_NOT_CONFIGURED"
        if not player_ids:
            return False, "NO_RECIPIENTS"
        import requests as _r
        payload = {
            "app_id": app_id,
            "include_player_ids": player_ids if isinstance(player_ids, list) else [player_ids],
            "headings": {"en": title},
            "contents": {"en": message},
        }
        if url:
            payload["url"] = url
        resp = _r.post(
            "https://onesignal.com/api/v1/notifications",
            headers={"Authorization": f"Basic {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        if resp.status_code in (200, 201):
            return True, None
        return False, f"OneSignal error {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)

def _email_shell(inner):
    """Wrap email body content in the branded Scanviction dark shell (logo + footer)."""
    return (
        '<div style="font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'background:#07090f;margin:0;padding:0;">'
        '<div style="max-width:520px;margin:0 auto;padding:40px 30px;">'
        '<div style="font-size:22px;font-weight:900;letter-spacing:-0.5px;margin-bottom:22px;">'
        '<span style="color:#e2e8f0;">Scan</span><span style="color:#f59e0b;">viction</span>'
        '<span style="color:#e2e8f0;">Pro</span></div>'
        + inner +
        '<div style="font-size:11px;color:#2a3a52;text-align:center;margin-top:22px;line-height:1.6;">'
        'Educational signals only — not financial advice. Past performance ≠ future results.<br>'
        '&copy; Scanviction</div></div></div>')

def _verify_email_html(code):
    """Welcoming, on-brand verification email that sells the product + carries the code."""
    inner = (
        '<div style="background:linear-gradient(160deg,#0e1530,#0a0e1c);border:1px solid #1c2440;'
        'border-radius:16px;padding:32px 28px;">'
        '<div style="font-size:11px;font-weight:800;letter-spacing:2px;text-transform:uppercase;'
        'color:#818cf8;margin-bottom:10px;">Welcome aboard</div>'
        '<div style="font-size:25px;font-weight:800;color:#f1f5f9;line-height:1.22;margin-bottom:12px;">'
        'The whole market, ranked by conviction \U0001F680</div>'
        '<div style="font-size:14px;color:#94a3b8;line-height:1.7;margin-bottom:24px;">'
        "You're in. Scanviction scores ~2,500 of the most liquid U.S. stocks across 20+ signal "
        'categories every session — momentum leaders, breakouts, volatility squeezes, insider '
        'clusters and more — so the strongest setups surface first, in plain English. Confirm your '
        'email and your Top Signals feed goes live.</div>'
        '<div style="font-size:12px;color:#6b7fa0;margin-bottom:8px;letter-spacing:0.5px;">'
        'YOUR VERIFICATION CODE</div>'
        f'<div style="font-size:38px;font-weight:900;letter-spacing:10px;color:#a5b4fc;padding:18px;'
        f'background:#080b14;border:1px solid #6366f1;border-radius:12px;text-align:center;">{code}</div>'
        '<div style="font-size:12px;color:#4a5e7a;margin-top:16px;line-height:1.6;">Enter it in the app '
        "to unlock your account. The code expires in 10 minutes. Didn't sign up? You can safely ignore "
        'this email.</div></div>')
    return _email_shell(inner)

def _reset_email_html(email, reset_url):
    """Password-reset email that names the account and gives a clear reset link."""
    inner = (
        '<div style="background:linear-gradient(160deg,#0e1530,#0a0e1c);border:1px solid #1c2440;'
        'border-radius:16px;padding:32px 28px;">'
        '<div style="font-size:11px;font-weight:800;letter-spacing:2px;text-transform:uppercase;'
        'color:#818cf8;margin-bottom:10px;">Password reset</div>'
        '<div style="font-size:24px;font-weight:800;color:#f1f5f9;line-height:1.25;margin-bottom:14px;">'
        'Reset your password \U0001F511</div>'
        '<div style="font-size:14px;color:#94a3b8;line-height:1.7;margin-bottom:10px;">We received a '
        'request to reset the password for your Scanviction account:</div>'
        f'<div style="font-size:14px;font-weight:700;color:#a5b4fc;background:#080b14;border:1px solid '
        f'#1c2440;border-radius:8px;padding:11px 14px;margin-bottom:22px;word-break:break-all;">{_esc(email)}</div>'
        f'<a href="{reset_url}" style="display:block;text-align:center;padding:13px 24px;background:#6366f1;'
        'color:#ffffff;text-decoration:none;border-radius:9px;font-weight:700;font-size:14px;">'
        'Reset your password &rarr;</a>'
        '<div style="font-size:12px;color:#4a5e7a;margin-top:16px;line-height:1.6;">This link expires in '
        "1 hour. If you didn't request this, you can safely ignore this email — your password won't "
        'change.</div>'
        f'<div style="font-size:11px;color:#2a3a52;margin-top:14px;word-break:break-all;">Or paste this '
        f'link into your browser:<br>{reset_url}</div></div>')
    return _email_shell(inner)

def _send_password_reset(email, reset_token):
    """Send password reset email. Returns (True,None) or (False, info)."""
    try:
        resend_key = st.secrets.get("RESEND_API_KEY","")
        app_url    = _get_app_url()
        reset_url  = f"{app_url}/?reset_token={reset_token}&email={email}"
        if resend_key:
            import requests as _r
            html = _reset_email_html(email, reset_url)
            resp = _r.post("https://api.resend.com/emails",
                headers={"Authorization":f"Bearer {resend_key}","Content-Type":"application/json"},
                json={"from":st.secrets.get("EMAIL_FROM","Scanviction <support@scanviction.com>"),"to":[email],
                      "subject":"Reset your Scanviction password","html":html},
                timeout=10)
            if resp.status_code in (200,201): return True, None
    except Exception: pass
    return False, f"DEMO_RESET:{reset_token}"

def _send_verification_email(email, code):
    """Send 6-digit email verification code. Falls back to demo mode."""
    try:
        resend_key = st.secrets.get("RESEND_API_KEY","")
        if resend_key:
            import requests as _r
            resp = _r.post("https://api.resend.com/emails",
                headers={"Authorization":f"Bearer {resend_key}","Content-Type":"application/json"},
                json={"from":st.secrets.get("EMAIL_FROM","Scanviction <support@scanviction.com>"),"to":[email],
                      "subject":"Your Scanviction verification code",
                      "html":f"""<div style="font-family:Inter,sans-serif;background:#07090f;padding:40px;color:#e2e8f0;">
                        <h2>Scan<span style="color:#f59e0b;">viction</span></h2>
                        <h3>Verify your email</h3>
                        <div style="font-size:42px;font-weight:900;letter-spacing:8px;color:#6366f1;padding:20px;background:#0d1525;border-radius:12px;text-align:center;">{code}</div>
                        <p style="color:#6b7fa0;margin-top:20px;">Expires in 10 minutes.</p>
                      </div>"""},
                timeout=10)
            if resp.status_code in (200,201): return True,None
            return False, f"Email error: {resp.text}"
    except Exception: pass
    return False, f"DEMO_CODE:{code}"

def _send_verification_email_bg(email, code):
    """Non-blocking verification email. Reads the key in the CALLING (main) thread, then
    fires the actual Resend POST in a daemon thread so SIGNUP RETURNS INSTANTLY instead of
    blocking ~1-2s on the network. Returns (queued, info): info='DEMO_CODE:xxxxxx' only
    when no key is configured (so the verify page can still show the code)."""
    try: resend_key = st.secrets.get("RESEND_API_KEY", "")
    except Exception: resend_key = ""
    try: email_from = st.secrets.get("EMAIL_FROM", "Scanviction <support@scanviction.com>")
    except Exception: email_from = "Scanviction <support@scanviction.com>"
    if not resend_key:
        return False, f"DEMO_CODE:{code}"
    html = _verify_email_html(code)
    def _bg():
        try:
            import requests as _r
            _r.post("https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={"from": email_from, "to": [email],
                      "subject": "Welcome to Scanviction — confirm your email", "html": html},
                timeout=15)
        except Exception:
            pass
    import threading as _t
    _t.Thread(target=_bg, name="msp-verify-email", daemon=True).start()
    return True, None

SUPPORT_EMAIL = "support@scanviction.com"

def _send_support_email(name, from_email, subject, message):
    """Deliver a contact-form message to SUPPORT_EMAIL via Resend (reply-to = the
    sender, so we can reply straight from the inbox). Returns (True, None) when sent.
    With no RESEND_API_KEY it logs the message locally so nothing is lost and returns
    (False, 'DEMO'). This is the in-app webform — it never opens the user's mail app."""
    def esc(s): return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try: resend_key = st.secrets.get("RESEND_API_KEY", "")
    except Exception: resend_key = ""
    if resend_key:
        try:
            import requests as _r
            html = (f'<div style="font-family:Inter,sans-serif;padding:22px;">'
                    f'<h3 style="color:#6366f1;margin:0 0 12px;">New support message</h3>'
                    f'<p style="margin:2px 0;"><b>From:</b> {esc(name)} &lt;{esc(from_email)}&gt;</p>'
                    f'<p style="margin:2px 0;"><b>Subject:</b> {esc(subject) or "(none)"}</p>'
                    f'<hr style="border:none;border-top:1px solid #ddd;margin:12px 0;">'
                    f'<div style="white-space:pre-wrap;color:#222;line-height:1.6;">{esc(message)}</div></div>')
            resp = _r.post("https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={"from": st.secrets.get("EMAIL_FROM", "Scanviction <support@scanviction.com>"),
                      "to": [SUPPORT_EMAIL], "reply_to": (from_email or SUPPORT_EMAIL),
                      "subject": f"[Support] {subject or 'New message'}", "html": html},
                timeout=10)
            if resp.status_code in (200, 201): return True, None
            return False, f"Email provider error {resp.status_code}"
        except Exception as e:
            return False, str(e)
    # Fallback: persist locally so the message isn't lost before email is configured.
    try:
        import json as _j
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support_messages.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_j.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "name": name,
                              "email": from_email, "subject": subject, "message": message}) + "\n")
    except Exception:
        pass
    return False, "DEMO"

def page_forgot():
    render_topbar()
    st.markdown('<div class="page-wrap">',unsafe_allow_html=True)
    _,cc,_=st.columns([1,2,1])
    with cc:
        st.markdown('<div style="text-align:center;padding:28px 0 16px;"><div style="font-size:24px;font-weight:800;color:#e2e8f0;">🔑 Reset Your Password</div><div style="font-size:13px;color:#374f6e;margin-top:6px;">Enter your email and we&#39;ll send a secure reset link.</div></div>',unsafe_allow_html=True)
        # Check if user came via reset link in URL
        try:
            params = st.query_params
            reset_token = params.get("reset_token","")
            reset_email = params.get("email","")
        except Exception:
            reset_token = ""; reset_email = ""

        if reset_token and reset_email:
            db = st.session_state.users_db
            stored = db.get(reset_email,{}).get("reset_token","")
            expiry  = db.get(reset_email,{}).get("reset_token_expiry",0)
            if stored == reset_token and time.time() < expiry:
                st.markdown('<div style="background:#0d1525;border:1px solid rgba(34,197,94,0.3);border-radius:10px;padding:14px;margin-bottom:12px;font-size:13px;font-weight:700;color:#4ade80;">✅ Reset link verified — set your new password</div>',unsafe_allow_html=True)
                with st.form("rpf"):
                    np_=st.text_input("New Password",type="password",placeholder="Min 8 characters")
                    np2=st.text_input("Confirm New Password",type="password")
                    if st.form_submit_button("🔐 Reset Password",type="primary",use_container_width=True):
                        if not np_ or not np2: st.error("Fill in both fields.")
                        elif np_!=np2: st.error("Passwords don't match.")
                        elif len(np_)<8: st.error("Minimum 8 characters.")
                        else:
                            db[reset_email]["pw"]=hp(np_)
                            db[reset_email].pop("reset_token",None)
                            db[reset_email].pop("reset_token_expiry",None)
                            _save_global_db(db); save_user_to_file(reset_email,db[reset_email])
                            try: st.query_params.clear()
                            except: pass
                            st.success("✅ Password reset! Redirecting to login…")
                            time.sleep(1.5); nav("login")
                return
            else:
                st.error("⚠️ Reset link is invalid or expired. Request a new one.")

        with st.form("fpf"):
            email=st.text_input("Email Address",placeholder="you@example.com")
            if st.form_submit_button("📧 Send Reset Link →",type="primary",use_container_width=True):
                _rok,_rmsg=secguard.reset_allowed(email,_client_key())
                if not secguard.valid_email(email):
                    st.error("Enter a valid email address.")
                elif not _rok:
                    st.error(_rmsg)   # anti email-bombing: per-address + per-client throttle
                else:
                    # Send ONLY if the email is registered, but ALWAYS show the SAME response so an
                    # attacker can't ENUMERATE which addresses have accounts from the reply.
                    _demo_url=""
                    if email in st.session_state.users_db:
                        token=_secrets.token_urlsafe(24)   # CSPRNG reset token
                        st.session_state.users_db[email]["reset_token"]=token
                        st.session_state.users_db[email]["reset_token_expiry"]=time.time()+3600
                        _save_global_db(st.session_state.users_db)
                        save_user_to_file(email,st.session_state.users_db[email])
                        ok,info=_send_password_reset(email,token)
                        if (not ok) and info and info.startswith("DEMO_RESET:"):
                            _demo_url=f"{_get_app_url()}/?reset_token={info.split(':',1)[1]}&email={email}"
                    st.markdown(f'<div style="background:#04200d;border:1px solid rgba(34,197,94,0.3);border-radius:10px;padding:18px;text-align:center;margin-top:10px;"><div style="font-size:24px;margin-bottom:8px;">📧</div><div style="font-size:14px;font-weight:700;color:#4ade80;margin-bottom:4px;">Check Your Email</div><div style="font-size:12px;color:#374f6e;">If <strong>{_esc(email)}</strong> is registered, a reset link was sent. Check your inbox (and spam).</div></div>',unsafe_allow_html=True)
                    if _demo_url:   # dev only (no RESEND key) — surfaces the link for local testing
                        st.markdown(f'<div style="background:#0d1525;border:1px solid rgba(245,158,11,0.3);border-radius:10px;padding:16px;margin-top:10px;"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:8px;">📧 Demo Mode — no email provider configured</div><div style="font-size:11px;color:#374f6e;margin-bottom:8px;">Add RESEND_API_KEY to Secrets for production email.</div><div style="font-size:11px;color:#4ade80;word-break:break-all;">{_esc(_demo_url)}</div></div>',unsafe_allow_html=True)
        if st.button("← Back to Login",key="f2l",use_container_width=True): nav("login")
    st.markdown('</div>',unsafe_allow_html=True)  # close page-wrap

# ─────────────────────────────────────────────────────────────
# PAGE: DASHBOARD
# ─────────────────────────────────────────────────────────────
def _render_onboarding():
    """First-run activation nudge: a 3-step checklist that walks the value flow — explore a signal in
    Discover (conviction breakdown + % since signal) → add it to the watchlist → set a price alert.
    Activation is the metric that most drives retention, so we make the path obvious. Auto-hides when
    complete (and fires an 'activated' funnel event) or when dismissed for the session."""
    if not is_authed() or st.session_state.get("_onboard_dismissed"):
        return
    s1 = bool(st.session_state.get("watchlist"))
    s2 = bool(st.session_state.get("_seen_detail"))
    s3 = bool(st.session_state.get("alerts"))
    if s1 and s2 and s3:
        track_once("_activated_tracked", "activated")   # fully activated → funnel event, then hide
        return
    done = sum([s1, s2, s3])
    def _row(ok, label):
        ic = "✅" if ok else "⬜"; col = "#5d6b86" if ok else "#e2e8f0"; deco = "line-through" if ok else "none"
        # Icon + a SINGLE label span. Putting the label's inline <b> tags directly in a flex
        # container turns each into its own flex item, which wraps into a scrambled grid on
        # narrow (mobile) widths — wrapping it in one span makes it flow as normal text.
        return (f'<div style="display:flex;align-items:flex-start;gap:9px;font-size:13px;color:{col};margin:5px 0;">'
                f'<span style="flex:0 0 auto;line-height:1.5;">{ic}</span>'
                f'<span style="flex:1 1 auto;min-width:0;line-height:1.5;text-decoration:{deco};">{label}</span></div>')
    st.markdown(f"""<div class="card" style="border-left:3px solid {GOLD};margin-bottom:16px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <div style="font-size:14px;font-weight:800;color:#e2e8f0;">🚀 Get started ({done}/3)</div>
          <div style="font-size:11px;color:#5d6b86;">~1 minute — the quickest tour of how Scanviction works</div>
        </div>
        <div style="font-size:12px;color:#6b7fa0;margin-bottom:9px;">We score ~2,500 US stocks every day and rank them by conviction. Here's how to put that to work:</div>
        {_row(s2, "Head to <b>Discover</b>, open a stock, and check its <b>conviction breakdown</b> + <b>% since we flagged it</b>")}
        {_row(s1, "Add that stock to your <b>watchlist</b> to keep tracking it")}
        {_row(s3, "Set a <b>price alert</b> so we ping you the moment it moves")}
    </div>""", unsafe_allow_html=True)
    b1, b2, _sp = st.columns([1, 1, 2])
    with b1:
        if (not s1 or not s2):
            if st.button("Browse signals →", key="ob_go", use_container_width=True): nav("discover")
        elif not s3:
            if st.button("Set an alert →", key="ob_go", use_container_width=True): nav("watchlist")
    with b2:
        if st.button("Dismiss", key="ob_dismiss", use_container_width=True):
            st.session_state["_onboard_dismissed"] = True; st.rerun()


def page_dashboard():
    render_topbar("dashboard")
    st.markdown('<div class="page-wrap pw-narrow">' ,unsafe_allow_html=True)

    # ── Welcome strip ──
    user_name = st.session_state.user.get("name","Trader") if is_authed() else "Trader"
    role_lbl = {"owner":"👑 Owner","admin":"🛡️ Admin","premium":"⭐ Premium","free":"👤 Free"}.get(st.session_state.get("role","free"),"👤 Free")
    role_color = GOLD if is_premium() else "#6b7fa0"

    now_hour = datetime.now().hour
    greeting = "Good morning" if now_hour<12 else ("Good afternoon" if now_hour<18 else "Good evening")

    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:18px;flex-wrap:wrap;gap:8px;">
        <div style="min-width:0;flex:1 1 auto;">
            <div style="font-size:10px;font-weight:800;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px;">Market Overview</div>
            <div style="font-size:14px;color:#6b7fa0;margin-bottom:2px;">{greeting},</div>
            <div style="font-size:26px;font-weight:800;color:#e2e8f0;letter-spacing:-0.5px;overflow-wrap:anywhere;">{_esc(user_name)}</div>
        </div>
        <div style="text-align:right;flex:0 0 auto;">
            <div style="font-size:11px;color:#374f6e;margin-bottom:2px;">Account Status</div>
            <div style="font-size:14px;font-weight:700;color:{role_color};">{role_lbl}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── First-run activation checklist (auto-hides when done/dismissed) ──
    _render_onboarding()

    # ── Live market countdown timer ──
    render_market_timer()

    # ── Market regime backdrop (free, keyless FRED) — the same context shown on Discover ──
    try:
        _render_regime_banner(_safe_regime())
    except Exception:
        pass

    # ── Market Overview ONLY ──
    # NOTE: This page is intentionally about the OVERALL MARKET — indexes, breadth,
    # sectors, movers, buzz. Everything about the MODEL (Top Signals teaser, the
    # track-record proof band, category win rates) lives on the Results page;
    # signal browsing lives in Discover. No model content belongs here.
    st.markdown(f'<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">📊 MARKET OVERVIEW</div>',unsafe_allow_html=True)

    with st.spinner("Loading market data…"):
        idx=get_indexes(); secs=get_sectors(); movers=get_bi_movers()

    idx_cols=st.columns(len(idx)) if idx else []
    for col,(name,d) in zip(idx_cols,idx.items()):
        c=GREEN if d["pct"]>=0 else RED; ar="▲" if d["pct"]>=0 else "▼"
        hist=d.get("hist",[])
        bars=""
        if hist:
            mn,mx=min(hist),max(hist); rng=mx-mn if mx!=mn else 1
            bars=''.join([f'<div style="height:{int(14*(v-mn)/rng+3)}px;width:4px;background:{GREEN if d["pct"]>=0 else RED};border-radius:2px;display:inline-block;margin-right:1px;vertical-align:bottom;opacity:0.55;"></div>' for v in hist])
        col.markdown(f"""<div class="idx-w" style="padding:14px;">
            <div class="idx-name">{name}</div>
            <div class="idx-price" style="font-size:18px;">{d['price']:,.2f}</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:{c};">{ar}{abs(d['pct']):.2f}%</div>
            <div style="margin-top:10px;height:18px;display:flex;align-items:flex-end;">{bars}</div>
        </div>""",unsafe_allow_html=True)

    # ── Quick stats row ──
    st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
    avg_pct = sum(m["pct"] for m in movers)/len(movers) if movers else 0
    bull_count = sum(1 for m in movers if m["pct"]>0)
    bear_count = sum(1 for m in movers if m["pct"]<0)
    breadth_label = "Bullish" if avg_pct>0.3 else "Bearish" if avg_pct<-0.3 else "Mixed"
    breadth_color = GREEN if avg_pct>0.3 else RED if avg_pct<-0.3 else "#94a3b8"
    top_sec = max(secs,key=secs.get) if secs else "—"
    top_sec_chg = secs.get(top_sec,0) if secs else 0

    qcols = st.columns(4)
    qcols[0].markdown(f"""<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:12px 14px;border-radius:10px;">
        <div style="font-size:10px;color:#374f6e;letter-spacing:1.5px;font-weight:700;">MARKET BREADTH</div>
        <div style="font-size:18px;font-weight:800;color:{breadth_color};margin-top:4px;">{breadth_label}</div>
        <div style="font-size:10px;color:#4a5e7a;margin-top:2px;">{bull_count} ↑ · {bear_count} ↓</div>
    </div>""", unsafe_allow_html=True)
    qcols[1].markdown(f"""<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:12px 14px;border-radius:10px;">
        <div style="font-size:10px;color:#374f6e;letter-spacing:1.5px;font-weight:700;">STRONGEST SECTOR</div>
        <div style="font-size:14px;font-weight:800;color:{GREEN};margin-top:4px;">{top_sec}</div>
        <div style="font-size:10px;color:#4a5e7a;margin-top:2px;">+{top_sec_chg:.2f}% today</div>
    </div>""", unsafe_allow_html=True)
    qcols[2].markdown(f"""<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:12px 14px;border-radius:10px;">
        <div style="font-size:10px;color:#374f6e;letter-spacing:1.5px;font-weight:700;">YOUR WATCHLIST</div>
        <div style="font-size:18px;font-weight:800;color:#e2e8f0;margin-top:4px;">{len(st.session_state.get("watchlist",[]))} stocks</div>
        <div style="font-size:10px;color:#4a5e7a;margin-top:2px;">tracked</div>
    </div>""", unsafe_allow_html=True)
    qcols[3].markdown(f"""<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:12px 14px;border-radius:10px;">
        <div style="font-size:10px;color:#374f6e;letter-spacing:1.5px;font-weight:700;">ACTIVE ALERTS</div>
        <div style="font-size:18px;font-weight:800;color:#e2e8f0;margin-top:4px;">{len(st.session_state.get("alerts",[]))} active</div>
        <div style="font-size:10px;color:#4a5e7a;margin-top:2px;">monitoring</div>
    </div>""", unsafe_allow_html=True)

    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)

    # ── Today's Market Brief (auto-generated insight) ──
    try:
        # Compute regime
        if HAS_SIGNAL_ENGINE:
            squeeze_count = sum(1 for m in movers if m.get("pct",0) > 5)
            regime_info = detect_market_regime(secs, avg_pct, squeeze_count)
            regime_label = regime_info.get("label","⚖️ Mixed")
            regime_desc = regime_info.get("description","Mixed market conditions.")
            best_strategies = regime_info.get("best_strategies",[])
        else:
            regime_label = "⚖️ Mixed"
            regime_desc = "Standard market conditions."
            best_strategies = []

        # Find strongest and weakest sector
        sec_sorted_brief = sorted(secs.items(), key=lambda x: x[1], reverse=True)
        strong_sec = sec_sorted_brief[0] if sec_sorted_brief else ("—", 0)
        weak_sec = sec_sorted_brief[-1] if sec_sorted_brief else ("—", 0)

        # Top mover
        top_gain = max(movers, key=lambda x: x["pct"]) if movers else {}
        top_loss = min(movers, key=lambda x: x["pct"]) if movers else {}

        strategies_str = " · ".join(best_strategies[:3]) if best_strategies else "Diversified composite signals"

        brief_html = f"""
        <div style="background:linear-gradient(135deg,#0a1228 0%,#0d1525 100%);
                    border:1px solid rgba(99,102,241,0.25);border-radius:14px;
                    padding:18px 22px;margin-bottom:18px;">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
                <span style="font-size:11px;font-weight:800;color:#a5b4fc;letter-spacing:2px;">💡 TODAY'S MARKET BRIEF</span>
                <span style="background:rgba(99,102,241,0.15);color:#a5b4fc;font-size:10px;font-weight:700;padding:3px 10px;border-radius:12px;border:1px solid rgba(99,102,241,0.3);">{regime_label}</span>
            </div>
            <div style="font-size:13px;color:#e2e8f0;line-height:1.7;">
                {regime_desc} <strong style="color:#4ade80;">{strong_sec[0]}</strong> leads sectors at <strong style="color:#4ade80;">+{strong_sec[1]:.2f}%</strong>,
                while <strong style="color:#f87171;">{weak_sec[0]}</strong> lags at <strong style="color:#f87171;">{weak_sec[1]:+.2f}%</strong>.
                Top mover: <strong style="color:#818cf8;">{top_gain.get('t','—')}</strong> ({'+' if top_gain.get('pct',0)>=0 else ''}{top_gain.get('pct',0):.2f}%).
            </div>
            <div style="font-size:11px;color:#6b7fa0;margin-top:8px;line-height:1.6;">
                <strong style="color:#94a3b8;">Best strategies in this regime:</strong> {strategies_str}
            </div>
        </div>
        """
        st.markdown(brief_html, unsafe_allow_html=True)
    except Exception:
        pass

    # ── Top Movers + Hot Stocks (2 columns) ──
    left,right=st.columns(2,gap="small")

    with left:
        st.markdown(f'<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">📈 TOP MOVERS TODAY</div>',unsafe_allow_html=True)
        gainers = sorted(movers, key=lambda x:x["pct"], reverse=True)[:5]
        losers = sorted(movers, key=lambda x:x["pct"])[:5]

        st.markdown('<div style="font-size:11px;color:#4ade80;font-weight:700;margin-bottom:6px;">🟢 GAINERS</div>',unsafe_allow_html=True)
        for m in gainers:
            st.markdown(f"""<div class="sr" style="padding:8px 12px;margin-bottom:3px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="sr-tick" style="font-size:13px;">{m['t']}</span>
                    <div style="display:flex;gap:14px;align-items:center;">
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#e2e8f0;">${m['price']:,.2f}</span>
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:{GREEN};">▲{m['pct']:.2f}%</span>
                    </div>
                </div>
            </div>""",unsafe_allow_html=True)

        st.markdown('<div style="font-size:11px;color:#f87171;font-weight:700;margin:10px 0 6px;">🔴 LOSERS</div>',unsafe_allow_html=True)
        for m in losers:
            st.markdown(f"""<div class="sr" style="padding:8px 12px;margin-bottom:3px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="sr-tick" style="font-size:13px;">{m['t']}</span>
                    <div style="display:flex;gap:14px;align-items:center;">
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#e2e8f0;">${m['price']:,.2f}</span>
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:{RED};">▼{abs(m['pct']):.2f}%</span>
                    </div>
                </div>
            </div>""",unsafe_allow_html=True)

    with right:
        st.markdown(f'<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">🔥 SOCIAL BUZZ</div>',unsafe_allow_html=True)
        # Serve entirely from the WARM cache (quote + sentiment are already on the
        # scanned rows), exactly like Gainers/Losers — the old per-ticker
        # get_quote()+st_sent() network loop made this table trickle in row by row
        # while the neighbouring tables appeared instantly.
        hot=st_hot()
        _buzz_rows=[]
        for t in hot[:8]:
            wr = _warm_row_for(t)
            q = (wr or {}).get("q") or {}
            s = (wr or {}).get("sent") or {}
            if not q:                      # rare: hot name outside the scanned universe
                try: q = get_quote(t) or {}
                except Exception: q = {}
                if q:
                    try: s = st_sent(t) or {}
                    except Exception: s = {}
            if not q.get("price"):
                continue
            _pct = q.get("pct", 0) or 0
            _bull = s.get("bull", 50) if isinstance(s, dict) else 50
            cc_=GREEN if _pct>=0 else RED; ar="▲" if _pct>=0 else "▼"
            bull_color = GREEN if _bull>=60 else RED if _bull<40 else "#94a3b8"
            _buzz_rows.append(f"""<div class="sr" style="padding:8px 12px;margin-bottom:3px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div>
                        <span class="sr-tick" style="font-size:13px;">{t}</span>
                        <span style="font-size:10px;color:{bull_color};margin-left:8px;font-weight:700;">{_bull}% bullish</span>
                    </div>
                    <div style="display:flex;gap:12px;align-items:center;">
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#e2e8f0;">${q.get('price',0):,.2f}</span>
                        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:{cc_};">{ar}{abs(_pct):.2f}%</span>
                    </div>
                </div>
            </div>""")
        # One markdown call → the whole table paints at once (no row-by-row pop-in).
        if _buzz_rows:
            st.markdown("".join(_buzz_rows), unsafe_allow_html=True)
        else:
            st.markdown('<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px;text-align:center;font-size:12px;color:#374f6e;">Buzz data is warming — back in a moment.</div>', unsafe_allow_html=True)

    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)

    # ── Sector Heatmap ──
    st.markdown(f'<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">🗺️ SECTOR HEATMAP</div>',unsafe_allow_html=True)
    sec_sorted=sorted(secs.items(),key=lambda x:x[1],reverse=True)
    if not sec_sorted:
        st.markdown('<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:16px;text-align:center;font-size:12px;color:#374f6e;">Sector data is loading or temporarily unavailable.</div>', unsafe_allow_html=True)
        sec_sorted = []
    sc_cols=st.columns(len(sec_sorted)) if sec_sorted else []
    for i,(sec,chg) in enumerate(sec_sorted):
        with sc_cols[i]:
            intensity = min(abs(chg)/2.5, 1.0)
            if chg > 0:
                bg = f"rgba(34,197,94,{0.15+intensity*0.5})"
                tc = "#4ade80"
            elif chg < 0:
                bg = f"rgba(239,68,68,{0.15+intensity*0.5})"
                tc = "#f87171"
            else:
                bg = "rgba(255,255,255,0.04)"; tc = "#94a3b8"
            arrow = "▲" if chg>=0 else "▼"
            st.markdown(f"""<div style="background:{bg};border-radius:8px;padding:14px 6px;text-align:center;border:1px solid rgba(255,255,255,0.05);">
                <div style="font-size:11px;color:#e2e8f0;font-weight:600;margin-bottom:4px;">{sec}</div>
                <div style="font-size:14px;font-weight:800;color:{tc};font-family:'JetBrains Mono',monospace;">{arrow}{abs(chg):.2f}%</div>
            </div>""",unsafe_allow_html=True)

    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)

    # ── Recently Viewed Stocks ──
    rv = st.session_state.get("recently_viewed", [])
    if is_authed():
        # Load from user DB if session is empty
        if not rv:
            uemail = st.session_state.user.get("email","")
            db_user = st.session_state.users_db.get(uemail, {})
            rv = db_user.get("recently_viewed", [])
            st.session_state.recently_viewed = rv
    if rv:
        st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">🕒 RECENTLY VIEWED</div>', unsafe_allow_html=True)
        rv_cols = st.columns(min(len(rv), 6))
        for i, t in enumerate(rv[:6]):
            with rv_cols[i]:
                q_rv = get_quote(t)
                if q_rv:
                    cc_rv = GREEN if q_rv["pct"]>=0 else RED
                    ar_rv = "▲" if q_rv["pct"]>=0 else "▼"
                    st.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:12px 10px;text-align:center;">
                        <div style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:#818cf8;">{t}</div>
                        <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:#e2e8f0;margin:2px 0;">${q_rv['price']:,.2f}</div>
                        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:{cc_rv};">{ar_rv}{abs(q_rv['pct']):.2f}%</div>
                    </div>""", unsafe_allow_html=True)

    # ── Premium teaser if free user ──
    if not is_premium():
        st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#1a0d00,#0d1525);border:1px solid rgba(245,158,11,0.3);
                    border-radius:14px;padding:24px 28px;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;">
                <div>
                    <div style="font-size:11px;font-weight:700;color:{GOLD};letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;">👑 UPGRADE TO PREMIUM</div>
                    <div style="font-size:18px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Unlock 15 Premium Composite Categories + Real-Time Telegram Alerts</div>
                    <div style="font-size:13px;color:#6b7fa0;">Insider Cluster · Short Squeeze · Relative Strength · Breakdown (short) · Brief filters · Signal Charts</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if gold_btn("Start Premium — $19/month →", "dash_upgrade"): nav("pricing")

    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: RESULTS  (route key stays "performance" — renaming it would break bookmarks)
# ─────────────────────────────────────────────────────────────
def page_performance():
    """RESULTS — how the engine's calls actually worked out. The counterpart to Discover:
    Discover is what Scanviction is flagging right now, this is how earlier flags did.
    Headline win rate, best performers since their signal, win rate by category and the
    latest activity. The deeper per-horizon audit (1/3/5/10/30-day vs the S&P) lives on
    the separate Full Track Record page. Market Overview stays purely about the market."""
    render_topbar("performance")
    st.markdown('<div class="page-wrap pw-narrow">', unsafe_allow_html=True)

    # Page-scoped animation kit. Entrance animations are SHORT one-shots (this page
    # has no auto-refresh fragment, so they replay only on a real visit — the rerun-
    # replay problem that got the old page-enter fade removed doesn't apply here).
    st.markdown(f"""<style>
    .pf-hd{{text-align:center;margin:6px 0 2px;}}
    .pf-title{{font-size:26px;font-weight:900;color:#f1f5f9;letter-spacing:-0.6px;}}
    .pf-sub{{font-size:12.5px;color:#374f6e;max-width:600px;margin:6px auto 0;line-height:1.55;}}
    .pf-live{{display:inline-flex;align-items:center;gap:7px;font-size:10px;font-weight:800;color:#34d399;
        letter-spacing:1.6px;text-transform:uppercase;background:rgba(52,211,153,0.08);
        border:1px solid rgba(52,211,153,0.25);border-radius:999px;padding:4px 12px;margin-bottom:10px;}}
    .pf-dot{{width:7px;height:7px;border-radius:50%;background:#34d399;animation:pfPulse 2s infinite;}}
    @keyframes pfPulse{{0%,100%{{box-shadow:0 0 0 0 rgba(52,211,153,.5);}}50%{{box-shadow:0 0 0 6px rgba(52,211,153,0);}}}}
    .pf-lbl{{font-size:11px;font-weight:800;color:#4a5e7a;letter-spacing:2.5px;text-transform:uppercase;
        margin:26px 0 6px;text-align:center;}}
    .pf-note{{font-size:12px;color:#374f6e;text-align:center;max-width:560px;margin:0 auto 14px;line-height:1.5;}}
    /* Category performance bars — grow in from the left, staggered */
    .pf-cat{{display:flex;align-items:center;gap:12px;background:#0d1525;border:1px solid #1c2942;
        border-radius:11px;padding:10px 14px;margin-bottom:7px;
        animation:pfUp .5s cubic-bezier(.22,1,.36,1) both;transition:border-color .15s ease,transform .15s ease;}}
    .pf-cat:hover{{border-color:rgba(99,102,241,.45);transform:translateX(3px);}}
    .pf-cat-ic{{display:inline-flex;flex-shrink:0;}}
    .pf-cat-n{{font-size:12.5px;font-weight:700;color:#dbe4f1;min-width:0;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap;flex:0 1 190px;}}
    .pf-cat-bar{{flex:1;height:8px;background:rgba(255,255,255,0.05);border-radius:4px;overflow:hidden;}}
    .pf-cat-fill{{height:8px;border-radius:4px;transform-origin:left;animation:pfGrow .9s cubic-bezier(.22,1,.36,1) both;}}
    @keyframes pfGrow{{from{{transform:scaleX(0);}}to{{transform:scaleX(1);}}}}
    @keyframes pfUp{{from{{opacity:0;transform:translateY(8px);}}to{{opacity:1;transform:translateY(0);}}}}
    .pf-cat-wr{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:800;min-width:52px;text-align:right;}}
    .pf-cat-meta{{font-size:10.5px;color:#4a5e7a;min-width:110px;text-align:right;white-space:nowrap;}}
    /* Live activity rows — slide in, staggered */
    .pf-evt{{display:flex;align-items:center;gap:10px;background:#0b1220;
        border:1px solid #1c2942;border-radius:10px;padding:9px 13px;margin-bottom:6px;
        animation:pfUp .45s cubic-bezier(.22,1,.36,1) both;}}
    .pf-evt-t{{font-family:'JetBrains Mono',monospace;font-size:13.5px;font-weight:800;color:#f1f5f9;min-width:52px;}}
    .pf-evt-cat{{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:700;min-width:0;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .pf-evt-sc{{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:800;border-radius:6px;padding:1px 7px;flex-shrink:0;}}
    .pf-evt-age{{font-size:10px;color:#4a5e7a;margin-left:auto;flex-shrink:0;}}
    .pf-evt-new{{font-size:8.5px;font-weight:800;letter-spacing:.5px;color:#34d399;background:rgba(52,211,153,.1);
        border:1px solid rgba(52,211,153,.3);border-radius:5px;padding:1px 6px;flex-shrink:0;}}
    </style>""", unsafe_allow_html=True)

    st.markdown('<div class="pf-hd">'
                '<div class="pf-live"><span class="pf-dot"></span>Live · updates with every scan</div>'
                '<div class="pf-title">Results</div>'
                '<div class="pf-sub">How the engine\'s calls actually worked out. Every signal is logged with a locked '
                'entry price and timestamp the moment it fires, then measured in its called direction — so these are '
                'outcomes, not estimates. For what\'s being flagged <b style="color:#a5b4fc;">right now</b>, see '
                '<b style="color:#a5b4fc;">Discover</b>. Educational only — not financial advice.</div></div>',
                unsafe_allow_html=True)

    # ── 1) Live track record (the proof band that used to sit on Market Overview) ──
    # This page LEADS with the band, so it can't silently render nothing: the band hides
    # until at least 3 signals have resolved outcomes, which is exactly the cold-start a
    # visitor needs explained. Every other section here has an empty state; so does this.
    if not render_signal_proof("performance"):
        st.markdown(
            '<div style="background:linear-gradient(135deg,rgba(245,158,11,0.07),transparent);'
            'border:1px solid rgba(245,158,11,0.22);border-radius:14px;padding:22px 20px;margin:8px 0 6px;">'
            '<div style="font-size:10px;font-weight:800;color:#f59e0b;letter-spacing:2px;'
            'text-transform:uppercase;margin-bottom:10px;">📈 Track Record · building</div>'
            '<div style="font-size:13px;color:#6b7fa0;line-height:1.6;">The engine is logging signals with '
            'locked entry prices right now, but none have completed a measurement horizon yet — outcomes '
            'resolve over 1–20 trading days, and we publish the aggregate only once at least three signals '
            'have actually been measured.</div>'
            '<div style="font-size:11.5px;color:#374f6e;margin-top:9px;">No estimates, no backfilled history: '
            'this band fills in on its own as the first signals mature.</div></div>',
            unsafe_allow_html=True)

    # ── 2) Top performers SINCE SIGNAL — ranked by the accumulated move, not conviction ──
    # This list is the whole point of the page, so it is ordered by the measured move since
    # each signal fired and picks with no locked snapshot are left out entirely. Ordering by
    # conviction and backfilling "anything not underwater" is what let a signal from two
    # minutes ago — which by definition has accumulated nothing — head the list.
    st.markdown('<div class="pf-lbl">🏆 Top Performers · since signal</div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-note">The best picks by how far they\'ve moved <b style="color:#a5b4fc;">since '
                'the signal fired</b> — days or weeks ago, not just today. Longs measured up, shorts labeled SHORT and '
                'measured as shorts (a decline = a win). For what\'s being flagged in today\'s session, see '
                '<b style="color:#a5b4fc;">Today\'s Signals</b> on Discover. Tap any card for the full breakdown.</div>',
                unsafe_allow_html=True)
    try:
        _pgrouped = _discover_grouped()
    except Exception:
        _pgrouped = {}
    if _pgrouped:
        _ppool = _signal_candidates(_pgrouped)
        _psnaps = _discover_lock_and_load_snaps(_ppool)
        _ptop = _top_since_signal(_ppool, _psnaps, 6, per_cat=2)
        if _ptop:
            _render_conviction_grid(_ptop, "perf_ts", _psnaps)
        else:
            st.markdown('<div class="pf-note">No pick has a measured gain since its signal yet — entry prices lock the '
                        'moment a signal fires, so this fills in as the current batch matures.</div>',
                        unsafe_allow_html=True)
    else:
        st.markdown('<div class="pf-note">The scan is warming — top performers appear in a moment.</div>',
                    unsafe_allow_html=True)

    # ── 3) Performance by category — animated win-rate bars from REAL outcomes ──
    st.markdown('<div class="pf-lbl">📊 Win Rate by Category</div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-note">Win rate per signal category across all matured, tracked signals '
                '(demo data excluded). Bars fill as real outcomes accumulate at the 1–20 day horizons.</div>',
                unsafe_allow_html=True)
    _stats = {}
    if HAS_SIGNAL_ENGINE:
        try: _stats = get_category_performance_stats() or {}
        except Exception: _stats = {}
    _rows = sorted(((c, s) for c, s in _stats.items() if (s.get("count") or 0) > 0),
                   key=lambda x: (x[1].get("win_rate") or 0, x[1].get("count") or 0), reverse=True)
    if _rows:
        _bars = []
        for _bi, (_cat, _s) in enumerate(_rows[:14]):
            _wr = float(_s.get("win_rate") or 0); _n = int(_s.get("count") or 0)
            _avg = _s.get("avg_5d")
            _ac = cat_accent(_cat)
            _wc = GREEN if _wr >= 55 else GOLD if _wr >= 45 else RED
            _avg_s = (f'avg 5d {"+" if _avg >= 0 else ""}{_avg:.1f}%' if _avg is not None else "maturing")
            _bars.append(
                f'<div class="pf-cat" style="animation-delay:{_bi*0.06:.2f}s;">'
                f'<span class="pf-cat-ic" style="color:{_ac};">{cat_icon(_cat, 17)}</span>'
                f'<span class="pf-cat-n">{_clean_name(_cat)}</span>'
                f'<div class="pf-cat-bar"><div class="pf-cat-fill" style="width:{max(4, min(100, _wr)):.0f}%;'
                f'background:linear-gradient(90deg,{_ac}66,{_ac});animation-delay:{0.15 + _bi*0.06:.2f}s;"></div></div>'
                f'<span class="pf-cat-wr" style="color:{_wc};">{_wr:.0f}%</span>'
                f'<span class="pf-cat-meta">{_n} signal{"s" if _n != 1 else ""} · {_avg_s}</span>'
                f'</div>')
        st.markdown("".join(_bars), unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);border-radius:12px;'
                    'padding:26px;text-align:center;font-size:12.5px;color:#374f6e;">Category stats appear as tracked '
                    'signals mature (outcomes are measured over 1–20 trading days). The engine is logging signals now.</div>',
                    unsafe_allow_html=True)

    # ── 4) Latest model activity — newest signals & updates streaming in ──
    st.markdown('<div class="pf-lbl">⚡ Latest Model Activity</div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-note">The newest stocks to enter a signal category — your Daily Brief pings '
                'these; this is the model working in real time.</div>', unsafe_allow_html=True)
    _evts = []
    if HAS_SIGNAL_ENGINE:
        try: _evts = get_recent_signal_events(limit=10) or []
        except Exception: _evts = []
    if _evts:
        _erows = []
        for _ei, _e in enumerate(_evts):
            _cat = _e.get("category", "") or ""
            _t = _e.get("ticker", "?")
            _scv = int(_e.get("score_at_trigger", 0) or 0)
            try: _ets = datetime.fromisoformat(_e.get("triggered_at", "")).timestamp()
            except Exception: _ets = 0
            _agee = _humanize_age(_ets) if _ets else ""
            _isnew = bool(_ets and (time.time() - _ets) < 6 * 3600)
            _ac = cat_accent(_cat)
            _scc = GREEN if _scv >= 65 else GOLD if _scv >= 40 else "#94a3b8"
            _locked = _signal_is_locked(_cat)
            _tick_html = (f'<span class="pf-evt-t" style="filter:blur(5px);user-select:none;">{_t}</span>'
                          if _locked else f'<span class="pf-evt-t">{_t}</span>')
            _erows.append(
                f'<div class="pf-evt" style="animation-delay:{_ei*0.05:.2f}s;">{_tick_html}'
                f'<span class="pf-evt-cat" style="color:{_ac};">{cat_icon(_cat, 13)}<span>{_clean_name(_cat)}</span></span>'
                + (f'<span class="pf-evt-sc" style="color:{_scc};background:{_scc}1f;">{_scv}</span>' if _scv else "")
                + ('<span class="pf-evt-new">NEW</span>' if _isnew else "")
                + f'<span class="pf-evt-age">{_agee}</span></div>')
        st.markdown("".join(_erows), unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);border-radius:12px;'
                    'padding:22px;text-align:center;font-size:12.5px;color:#374f6e;">No signal events logged yet — '
                    'new category entries appear here as the market moves.</div>', unsafe_allow_html=True)

    _pc1, _pc2 = st.columns(2, gap="small")
    with _pc1:
        if st.button("🔍 Browse all signals in Discover →", key="perf_to_disc", use_container_width=True, type="primary"):
            st.session_state.discover_cat = DISCOVER_HOME
            nav("discover")
    with _pc2:
        if st.button("🔔 Open your Daily Brief →", key="perf_to_brief", use_container_width=True):
            nav("signals")

    st.markdown('<div style="font-size:10px;color:#2a3a52;text-align:center;margin-top:16px;">'
                'Every figure on this page is measured from locked entry snapshots — nothing is backfilled or curated. '
                'Past performance does not guarantee future results.</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: DISCOVER
# ─────────────────────────────────────────────────────────────
# The 20 composite categories collapse into 6 scannable THEMES so Discover leads
# with answers (a cross-category Top Signals feed) instead of a 30-button wall.
# Every composite key appears exactly once across these lists.
DISCOVER_THEMES = [
    ("🔥 Momentum & Trend",        ["🌊 Momentum Leaders", "⚡ Momentum Surge", "🏆 Relative Strength", "🏅 Quality Momentum", "🎯 Pullback Buy"]),
    ("📈 Breakouts & Volatility",  ["🚀 Breakout Watch", "🌪️ Volatility Squeeze", "💥 Volatility Expansion", "🍃 VCP Volume Dry-Up"]),
    ("📉 Reversals & Bottoms",     ["📉→📈 Oversold Reversal", "🪂 Fallen Angels", "🩸 Capitulation Bottom"]),
    ("⚡ Squeeze & Short Interest", ["🔥 Short Squeeze", "⚡🧲 Smart-Money Squeeze"]),
    ("🏛️ Smart Money & Catalysts", ["🏛️ Insider Cluster", "🎪 Catalyst / Gap", "🦈 Quiet Accumulation"]),
    ("🎭 Social, Value & Hidden",  ["🎭 Social Catalyst", "💎 Value Momentum", "💡 Hidden Movers"]),
    ("🐻 Bearish & Short",         ["📉 Breakdown", "🐻 Distribution", "🔻 Overbought Fade"]),
]

# ── Per-THEME accent hues ─────────────────────────────────────────────────────
# Every category used to render in the same indigo, so the Discover grid read as
# one undifferentiated wall ("they all blend together"). Each theme family now
# carries its own accent — used on tile borders, icons, count badges and card
# category labels — while the base surfaces stay on the brand palette, so the
# page stays on-theme but a Squeeze tile is instantly distinguishable from a
# Reversal tile.
THEME_ACCENT = {
    "🔥 Momentum & Trend":        "#818cf8",   # indigo — the brand core
    "📈 Breakouts & Volatility":  "#22d3ee",   # cyan
    "📉 Reversals & Bottoms":     "#34d399",   # emerald
    "⚡ Squeeze & Short Interest": "#f59e0b",   # amber
    "🏛️ Smart Money & Catalysts": "#a78bfa",   # violet
    "🎭 Social, Value & Hidden":  "#f472b6",   # pink
    "🐻 Bearish & Short":         "#fb7185",   # rose
}
_CAT_THEME = {c: th for th, cats in DISCOVER_THEMES for c in cats}

def cat_accent(cat):
    """The accent hue for a category (via its theme), or brand indigo."""
    return THEME_ACCENT.get(_CAT_THEME.get(cat, cat), "#818cf8")

# Regime → themes to highlight + a one-line plain-English guide. GUIDANCE ONLY:
# this never changes any score or hides any category — it just orients the user.
REGIME_THEMES = {
    "risk_on":  ["🔥 Momentum & Trend", "⚡ Squeeze & Short Interest", "📈 Breakouts & Volatility"],
    "neutral":  ["🔥 Momentum & Trend", "🏛️ Smart Money & Catalysts"],
    "risk_off": ["🐻 Bearish & Short", "📉 Reversals & Bottoms", "🎭 Social, Value & Hidden"],
}
REGIME_GUIDANCE = {
    "risk_on":  "Risk appetite is healthy — momentum, breakouts and squeeze setups tend to lead in a calm, tight-credit tape.",
    "neutral":  "A balanced tape — lean on high-conviction momentum and smart-money / catalyst setups.",
    "risk_off": "Defensive tape — reversals, quality and value tend to hold up better when volatility and credit stress are rising.",
}
DISCOVER_HOME = "__home__"   # sentinel: show the Top Signals home instead of one category


def _safe_regime():
    try:
        rg = _market_regime()
        return rg if (rg and rg.get("regime") not in (None, "unknown")) else None
    except Exception:
        return None


def _disc_key(cat):
    """Stable, collision-free widget-key suffix for a category name."""
    return "".join(ch if ch.isalnum() else "_" for ch in cat)[:40] or "x"


def _cat_is_locked(cat):
    meta = COMPOSITE_CATS.get(cat)
    return bool(meta) and meta[1] == "premium" and not is_premium()


def _signal_is_locked(cat):
    """Tier-gate a feed / alert signal the SAME way Discover gates categories, so the
    Signals feed and Discover are consistent for a given account. Premium = the premium
    composite categories AND the filing/data event alerts (insider buys, fresh 8-K, short
    interest) — those are the 'Insider Cluster & Short Squeeze' / advanced-data Premium
    features. Free composite categories stay open."""
    if is_premium():
        return False
    if cat in EVENT_ALERT_TYPES:
        return True
    meta = COMPOSITE_CATS.get(cat)
    return bool(meta) and meta[1] == "premium"


def _discover_grouped():
    """{primary_cat: [rows sorted by fit desc]} over the warm universe. CACHED per-warm
    in session_state (keyed by the universe build time) so a Discover body rerun — e.g.
    expanding a theme — doesn't re-bucket ~2,500 rows every time (that re-bucketing was a
    big chunk of the dropdown's perceived lag). Empty dict while still warming."""
    with _UNIVERSE_LOCK:
        bid = _UNIVERSE_CACHE.get("built_at", 0)
    if bid and st.session_state.get("_disc_grp_at") == bid and "_disc_grp" in st.session_state:
        return st.session_state["_disc_grp"]
    grouped = {}
    for r in build_scored_universe():
        pc = r.get("primary_cat")
        if pc:
            grouped.setdefault(pc, []).append(r)
    for pc in grouped:
        grouped[pc].sort(key=lambda x: (x.get("comp", 0) or 0), reverse=True)
    if bid:
        st.session_state["_disc_grp"] = grouped
        st.session_state["_disc_grp_at"] = bid
    return grouped


def _top_signals(grouped, n=6):
    """Highest-conviction picks ACROSS every category (the cross-category feed)."""
    rows = [r for rs in grouped.values() for r in rs]
    # Rank by conviction WEIGHTED by the category's backtested edge, so the categories that have
    # historically led the market (Relative Strength / Momentum) surface first and the coin-flip /
    # regime-fragile ones sink — without changing any score the user sees.
    rows.sort(key=lambda r: (r.get("conviction") or r.get("sc") or 0) * category_edge(r.get("primary_cat", "")), reverse=True)
    # one card per ticker (rows are already unique tickers, but be defensive)
    seen, out = set(), []
    for r in rows:
        t = r.get("t")
        if t and t not in seen:
            seen.add(t); out.append(r)
        if len(out) >= n:
            break
    return out


# ── The two performance concepts ─────────────────────────────────────────────────────
# These are DIFFERENT questions and every board that shows one must not silently show the
# other:
#   • SINCE SIGNAL — the measured move from the locked entry snapshot, in the called
#     direction. Cumulative and open-ended: a pick that fired two weeks ago and kept
#     working is exactly what belongs at the top. This is the model's track record.
#   • TODAY        — the intraday move (q.pct), also in the called direction. Resets every
#     session. This is "what is running right now", independent of when the signal fired.
# Ranking by conviction and labelling the result either way is what produced a "Today's
# Board" full of week-old picks and a "since signal" list led by a two-minute-old signal.

def _cat_diverse(rows, n, per_cat=1, seen_tickers=None):
    """First `n` rows, at most `per_cat` per primary category, order preserved.
    `seen_tickers` (a mutable set) additionally de-dupes ACROSS sections of one board."""
    out, cat_seen = [], {}
    for r in rows:
        t = r.get("t")
        if seen_tickers is not None and t in seen_tickers:
            continue
        c = r.get("primary_cat", "")
        if cat_seen.get(c, 0) >= per_cat:
            continue
        cat_seen[c] = cat_seen.get(c, 0) + 1
        out.append(r)
        if seen_tickers is not None:
            seen_tickers.add(t)
        if len(out) >= n:
            break
    return out


def _signal_candidates(grouped, per_cat=4, cap=160):
    """A ranked candidate pool that samples EVERY category before ranking.

    _top_signals ranks by conviction × the category's backtested edge, and the highest-edge
    category (Relative Strength) has enough high scorers to fill the entire head of that
    list on its own. That is why the board rendered six near-identical Relative Strength
    cards, and why a downstream one-per-category filter could only ever find a single pick.
    Taking the best `per_cat` from each category FIRST guarantees the pool spans the board.
    """
    pool = []
    for _cat, rows in (grouped or {}).items():
        pool.extend(sorted(rows, key=lambda r: (r.get("conviction") or r.get("sc") or 0),
                           reverse=True)[:per_cat])
    pool.sort(key=lambda r: (r.get("conviction") or r.get("sc") or 0)
              * category_edge(r.get("primary_cat", "")), reverse=True)
    seen, out = set(), []
    for r in pool:
        t = r.get("t")
        if t and t not in seen:
            seen.add(t); out.append(r)
        if len(out) >= cap:
            break
    return out


def _today_pct(r):
    """Today's move in the signal's OWN direction (a bear pick counts a decline as a gain)."""
    try:
        pct = float((r.get("q") or {}).get("pct", 0) or 0)
    except Exception:
        return 0.0
    return -pct if category_dir(r.get("primary_cat", "") or "") == "bear" else pct


def _top_since_signal(rows, snaps, n, per_cat=1, min_pct=0.0, seen_tickers=None):
    """Best MEASURED move since the signal fired, in the called direction.

    Rows with no locked snapshot are EXCLUDED rather than backfilled: there is nothing to
    measure yet, and letting them through is what put a two-minute-old signal at the head
    of a list whose whole premise is accumulated performance."""
    scored = []
    for r in rows:
        snap = snaps.get(r.get("t"))
        if not snap:
            continue
        d, _ = _since_signal_pct(r.get("primary_cat", ""), snap.get("entry_price", 0),
                                 (r.get("q") or {}).get("price", 0))
        if d is None or d < min_pct:
            continue
        scored.append((d, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return _cat_diverse([r for _, r in scored], n, per_cat, seen_tickers)


def _top_movers_today(rows, n, per_cat=1, min_pct=0.0, seen_tickers=None):
    """Biggest move TODAY in the called direction — regardless of when the signal fired."""
    scored = [(_today_pct(r), r) for r in rows]
    scored = [(p, r) for p, r in scored if p > min_pct]
    scored.sort(key=lambda x: x[0], reverse=True)
    return _cat_diverse([r for _, r in scored], n, per_cat, seen_tickers)


def _lock_svg(size=13):
    return _svg('<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>', size)


# REMOVED: _winning_top_signals(). It ordered by CONVICTION, kept anything "not underwater",
# and backfilled the rest with snapshot-less signals — so under a "since signal" heading it
# surfaced picks with nothing accumulated yet (a two-minute-old signal led the list), and
# under a "today" heading it surfaced picks that had fired a fortnight ago. Use
# _top_since_signal() or _top_movers_today(); each ranks by the number it actually claims.


def _signal_stamp(ts):
    """Absolute ET timestamp of a signal from an epoch float: 'Jul 19, 9:45am ET'.
    The cards used to show only a relative age ('1d ago') — this is the actual
    when-it-was-recommended timestamp. Blank on any failure."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York"))
        hr = dt.hour % 12 or 12
        return f"{dt.strftime('%b')} {dt.day}, {hr}:{dt.strftime('%M')}{dt.strftime('%p').lower()} ET"
    except Exception:
        return ""


def _since_signal_pct(cat, entry, price):
    """Signed %-since-signal in the SIGNAL'S OWN direction: for a bear/short category a
    price DECLINE is a positive return (that's the trade the signal called). Returns
    (display_pct, is_short) or (None, False) when it can't be computed."""
    try:
        entry = float(entry or 0); price = float(price or 0)
        if entry <= 0 or price <= 0:
            return None, False
        pct = (price - entry) / entry * 100.0
        if category_dir(cat or "") == "bear":
            return -pct, True
        return pct, False
    except Exception:
        return None, False


def _perf_since_html(r, snap):
    """Percent-since-signal + the actual signal timestamp. The entry price + timestamp
    lock the first time a signal surfaces (see record_recommendations_bulk), so the %
    keeps accumulating as the live price moves. For short signals the % is measured as
    the SHORT's return (a decline shows green/positive, labeled 'short'). Empty until
    a snapshot exists."""
    if not snap:
        return ""
    try:
        price = (r.get("q") or {}).get("price", 0) or 0
        disp, is_short = _since_signal_pct(r.get("primary_cat", ""), snap.get("entry_price", 0), price)
        if disp is None:
            return ""
        ts = snap.get("triggered_at", 0)
        stamp = _signal_stamp(ts)
        age = _humanize_age(ts)
        when = f"{stamp} · {age}" if stamp else f"Signaled {age}"
        pcol = GREEN if disp >= 0 else RED
        sign = "+" if disp >= 0 else ""
        lbl = "% since (short)" if is_short else "% since"
        return (f'<div class="cv-perf"><span class="cv-perf-age" title="When this signal was flagged">{when}</span>'
                f'<span style="color:{pcol};font-weight:700;font-family:JetBrains Mono,monospace;">{sign}{disp:.2f}{lbl}</span></div>')
    except Exception:
        return ""


# Shared conviction-card styles. Previously these classes were only defined inside
# page_dashboard / page_discover's inline <style> blocks, so the SAME cards rendered
# as an unstyled "wall of text" wherever the grid was reused (and anywhere else the
# grid is reused). The grid renderer now injects this once per script run.
_CV_CARD_CSS = f"""<style>
.cv-card{{background:linear-gradient(135deg,#0d1525,#0a0f1a);border:1px solid #1c2942;
    border-radius:12px;padding:12px 14px;min-height:120px;display:flex;flex-direction:column;
    gap:7px;margin-bottom:6px;transition:border-color .15s ease;}}
.cv-card:hover{{border-color:rgba(99,102,241,0.5);}}
.cv-top{{display:flex;justify-content:space-between;align-items:baseline;gap:6px;}}
.cv-tick{{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;color:#f1f5f9;}}
.cv-px{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;white-space:nowrap;}}
.cv-conv{{display:flex;align-items:center;gap:8px;}}
.cv-bar{{flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;}}
.cv-fill{{height:6px;border-radius:3px;}}
.cv-num{{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:800;min-width:24px;text-align:right;}}
.cv-tag{{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:#a5b4fc;min-width:0;}}
.cv-tag>span:first-of-type{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.cv-tag svg{{color:currentColor;flex-shrink:0;}}  /* inherits the per-theme accent */
.cv-prem{{display:inline-flex;align-items:center;gap:3px;margin-left:auto;color:{GOLD};font-size:10px;flex-shrink:0;}}
.cv-short{{font-size:8.5px;font-weight:800;letter-spacing:.6px;color:#fb7185;background:rgba(251,113,133,.12);
    border:1px solid rgba(251,113,133,.35);border-radius:5px;padding:1px 5px;flex-shrink:0;}}
.cv-prem svg{{color:{GOLD};}}
.cv-why{{font-size:11px;color:#6b7fa0;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;
    -webkit-box-orient:vertical;overflow:hidden;flex:1;}}
.cv-perf{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;
    padding-top:7px;border-top:1px solid rgba(255,255,255,0.06);font-size:10.5px;}}
.cv-perf-age{{color:#4a5e7a;}}
</style>"""
_CV_CSS_STATE = {"done": False}   # fresh namespace per script run → one injection per run


def _conviction_card_html(r, locked=False, snap=None):
    """Compact, conviction-led card atom (Top Signals). Custom category icon + clean
    name, conviction bar, one-line why, and percent-since-signal. Minimal by design —
    the full scorecard lives on the detail page."""
    t = r.get("t", ""); q = r.get("q") or {}
    price = q.get("price", 0) or 0; pct = q.get("pct", 0) or 0
    cat = r.get("primary_cat", "") or ""
    conv = int(r.get("conviction") or r.get("sc") or 0)
    why = (r.get("why") or "").strip()
    is_bear = category_dir(cat) == "bear"
    cc = GREEN if pct > 0 else RED if pct < 0 else "#6b7fa0"
    ar = "▲" if pct >= 0 else "▼"
    # Short setups score with bear_conviction (higher = stronger short), so colour them
    # on a bearish rose/amber ramp rather than the bullish green ramp.
    if is_bear:
        convc = "#fb7185" if conv >= 70 else "#fb923c" if conv >= 45 else "#94a3b8"
    else:
        convc = GREEN if conv >= 70 else GOLD if conv >= 45 else RED
    short_b = '<span class="cv-short" style="margin-left:6px;">SHORT</span>' if is_bear else ""
    # Per-theme accent on the category label + icon, so cards from different
    # signal families are visually distinct at a glance.
    ac = cat_accent(cat)
    if locked:
        head = ('<span class="cv-tick" style="filter:blur(5px);user-select:none;">NVDA</span>'
                '<span class="cv-px" style="filter:blur(4px);user-select:none;">$000 ▲0%</span>')
        tag = (f'<div class="cv-tag" style="color:{ac};">{cat_icon(cat, 14)}<span>{_clean_name(cat)}</span>{short_b}'
               f'<span class="cv-prem">{_lock_svg(11)} Premium</span></div>')
        body = f'<div class="cv-why" style="color:{GOLD};">Unlock to reveal this pick</div>'
        perf = ""
    else:
        head = (f'<span class="cv-tick">{t}</span>'
                f'<span class="cv-px" style="color:{cc};">${price:,.2f} {ar}{abs(pct):.1f}%</span>')
        tag = f'<div class="cv-tag" style="color:{ac};">{cat_icon(cat, 14)}<span>{_clean_name(cat)}</span>{short_b}</div>'
        body = f'<div class="cv-why">{why[:78]}{"…" if len(why) > 78 else ""}</div>'
        perf = _perf_since_html(r, snap)
    return (f'<div class="cv-card"><div class="cv-top">{head}</div>'
            f'<div class="cv-conv"><div class="cv-bar"><div class="cv-fill" style="width:{max(4, min(100, conv))}%;background:{convc};"></div></div>'
            f'<span class="cv-num" style="color:{convc};">{conv}</span></div>{tag}{body}{perf}</div>')


def _render_conviction_grid(rows, key_prefix, snaps=None):
    """3-up grid of FULLY-CLICKABLE conviction tiles — the whole card opens the detail
    (or the upgrade page for a locked premium pick). No separate button."""
    # Ensure the card styles exist on EVERY page that renders this grid (the scanner
    # previously showed raw unstyled text because only Discover injected them).
    if not _CV_CSS_STATE["done"]:
        _CV_CSS_STATE["done"] = True
        st.markdown(_CV_CARD_CSS, unsafe_allow_html=True)
    snaps = snaps or {}
    for rs in range(0, len(rows), 3):
        cols = st.columns(3, gap="small")
        chunk = rows[rs:rs + 3]
        for ci in range(3):
            with cols[ci]:
                if ci >= len(chunk):
                    continue
                r = chunk[ci]; t = r.get("t", "")
                locked = _cat_is_locked(r.get("primary_cat", ""))
                if clickable_tile(_conviction_card_html(r, locked, snaps.get(t)),
                                  key=f"{key_prefix}_{_disc_key(t)}"):
                    if locked:
                        nav("pricing")
                    else:
                        st.session_state.detail_ticker = t
                        st.session_state.detail_data = dict(r)
                        nav("stock_detail")


def _render_preparing_screen():
    """A SINGLE, calm, professional 'preparing' element (one st.markdown) shown while
    the universe warms. Rendered alone — no timer / banner / multi-card grid — so the
    bounded auto-refresh reruns REUSE this identical element instead of re-creating a
    grid every 2s (which, combined with the data-stale frame-hide, is what flashed).
    All motion is CSS; it tells the user exactly what's happening."""
    sources = ["live Polygon market data"]
    if HAS_EDGAR and EDGAR_ENABLED:
        sources.append("SEC insider &amp; 8-K filings")
    sources.append("FINRA short interest")
    src = ", ".join(sources[:-1]) + (" and " + sources[-1] if len(sources) > 1 else "")
    st.markdown(f"""
    <div class="prep-wrap"><div class="prep-card">
      <div class="prep-orbit"><span class="prep-dot"></span><span class="prep-dot"></span><span class="prep-dot"></span></div>
      <div class="prep-title">Preparing your market signals</div>
      <div class="prep-sub">Scoring the most liquid U.S. stocks across 23 signal categories using {src}.
        This runs once and usually takes under a minute — it'll appear automatically when ready.</div>
      <div class="prep-bar"><div class="prep-bar-fill"></div></div>
      <div class="prep-steps">
        <span class="prep-step">Loading market data</span>
        <span class="prep-step">Computing factors</span>
        <span class="prep-step">Ranking conviction</span>
      </div>
    </div></div>""", unsafe_allow_html=True)


def _render_home_failed():
    """Terminal 'data unavailable' state for the home feed (not polled, so no flash)."""
    st.markdown(f'<div class="prep-wrap"><div class="prep-card">'
                f'<div style="font-size:30px;margin-bottom:8px;">📡</div>'
                f'<div class="prep-title">Market data is temporarily unavailable</div>'
                f'<div class="prep-sub">The data provider returned nothing on the last attempt — usually a brief '
                f'rate-limit or maintenance window, not a problem on your end. It refreshes on its own.</div></div></div>',
                unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        if st.button("🔄 Retry now", key="disc_home_retry", use_container_width=True):
            try: _kick_background_warm(force=True)
            except Exception: pass
            st.rerun(scope="fragment")


def _maybe_warm_poll():
    """Bounded auto-advance so the home view flips from skeleton to live data on
    its own when the background warm finishes (fragment-scoped — no page blank)."""
    att = st.session_state.get("_warm_attempts", 0)
    if att < 100:  # ~100 × 2.2s ≈ 3.5 min — covers a full cold market-wide warm
        st.session_state["_warm_attempts"] = att + 1
        time.sleep(2.2)
        try:
            st.rerun(scope="fragment")
        except Exception:
            try:
                st.rerun()
            except Exception:
                pass


def _render_regime_banner(rg):
    if not rg or not rg.get("note"):
        return
    rc = {"risk_on": GREEN, "neutral": GOLD, "risk_off": RED}.get(rg["regime"], GOLD)
    st.markdown(
        f"<div style='display:flex;justify-content:center;margin:-2px 0 12px;'>"
        f"<div style='display:inline-flex;align-items:center;gap:9px;background:#0d1525;"
        f"border:1px solid {rc}44;border-radius:999px;padding:6px 16px;font-size:11.5px;color:#94a3b8;'>"
        f"<span style='width:8px;height:8px;border-radius:50%;background:{rc};display:inline-block;box-shadow:0 0 7px {rc};'></span>"
        f"<span style='font-weight:700;color:{rc};'>Market regime: {rg.get('label','')}</span>"
        f"<span style='color:#374f6e;'>{rg.get('note','')}</span></div></div>",
        unsafe_allow_html=True)


def _render_regime_guidance(rg):
    if not rg:
        return
    g = REGIME_GUIDANCE.get(rg["regime"], "")
    if not g:
        return
    chips = "".join(f'<span class="rg-chip">{cat_icon(th, 13)}<span>{_clean_name(th)}</span></span>'
                    for th in REGIME_THEMES.get(rg["regime"], []))
    st.markdown(f'<div class="rg-guide">{g}<div class="rg-chips">{chips}</div></div>', unsafe_allow_html=True)


def _theme_is_open(i, default):
    k = f"_theme_open_{i}"
    if k not in st.session_state:
        st.session_state[k] = default
    return bool(st.session_state[k])


def _render_theme_browser(rg, grouped):
    """Themed groups as CUSTOM collapsibles rendered inline in the Discover body fragment.
    Toggling open/closed (and drilling into a category) does a fragment-scoped rerun of the
    WHOLE body — fast and in-place (no full-page reload, so scroll is preserved and the
    drill-in doesn't jump to the bottom). The dropdown flash is handled by the stale-frame
    fade delay (tuned so an in-place fragment rerun never reaches it). Regime-relevant
    themes (and the first) open by default."""
    highlight = set(REGIME_THEMES.get(rg["regime"], [])) if rg else set()
    for ti, (theme, cats) in enumerate(DISCOVER_THEMES):
        active = [c for c in cats if grouped.get(c)]
        focus = theme in highlight
        opened = _theme_is_open(ti, focus or ti == 0)
        chev = "▾" if opened else "▸"
        foc = '<span class="th-focus">in focus</span>' if focus else ""
        _tac = cat_accent(theme)   # per-theme accent → header icon + live count
        hdr = (f'<div class="th-head {"th-head-on" if opened else ""}" style="border-left:3px solid {_tac};">'
               f'<span class="th-head-l"><span class="th-ic" style="color:{_tac};">{cat_icon(theme, 18)}</span>'
               f'<span class="th-head-n">{_clean_name(theme)}</span>{foc}</span>'
               f'<span class="th-head-r">{len(active)}/{len(cats)} live <span class="th-chev">{chev}</span></span></div>')
        with st.columns(1)[0]:
            if clickable_tile(hdr, key=f"th_head_{ti}"):
                st.session_state[f"_theme_open_{ti}"] = not opened
                st.rerun(scope="fragment")
        if opened:
            _render_category_grid(cats, grouped, key_prefix=f"ct{ti}")


def _category_tile_html(cat, rows, locked, standard=False):
    """A clickable category tile: custom icon + clean name + plain-English definition
    + a teaser. Picks show the ticker (emphasized) + its Conviction Score in a colored
    chip — higher score = stronger match, so the chip is greener the higher it goes."""
    n = len(rows)
    is_bear = category_dir(cat) == "bear"
    if locked:
        teaser = f'<span class="ct-prem">{_lock_svg(11)} Premium — unlock to view</span>'
    elif standard:
        teaser = f"{n} stocks ready" if n else "Tap to browse"
    elif rows:
        picks = []
        for r in rows[:3]:
            s = int(r.get("conviction") or r.get("sc") or 0)
            if is_bear:
                sc_col = "#fb7185" if s >= 70 else "#fb923c" if s >= 45 else "#7e8aa3"
            else:
                sc_col = "#34d399" if s >= 70 else "#a5b4fc" if s >= 50 else "#7e8aa3"
            picks.append(f'<span class="ct-pick"><b>{r["t"]}</b>'
                         f'<span class="ct-sc" style="color:{sc_col};background:{sc_col}1f;">{s}</span></span>')
        teaser = "".join(picks)
    else:
        teaser = '<span style="color:#3a4a63;">No matches right now</span>'
    # Theme accent: left edge + icon + count badge, so each signal family is
    # instantly distinguishable at a glance instead of a wall of identical indigo.
    ac = cat_accent(cat)
    cnt = f'<span class="ct-count" style="color:{ac};background:{ac}22;">{n}</span>' if (n and not locked) else ""
    short_b = '<span class="cv-short" style="margin-left:6px;">SHORT</span>' if is_bear else ""
    return (f'<div class="cat-tile{" cat-tile-lock" if locked else ""}" style="border-left:3px solid {ac};">'
            f'<div class="ct-h"><span class="ct-ic" style="color:{ac};">{cat_icon(cat, 19)}</span>'
            f'<span class="ct-n">{_clean_name(cat)}</span>{short_b}{cnt}</div>'
            f'<div class="ct-d">{cat_def(cat)}</div>'
            f'<div class="ct-f">{teaser}</div></div>')


def _render_category_grid(cats, grouped, key_prefix, standard=False):
    """2-up grid of fully-clickable category tiles → drill into that category."""
    for rs in range(0, len(cats), 2):
        cols = st.columns(2, gap="small")
        chunk = cats[rs:rs + 2]
        for ci in range(2):
            with cols[ci]:
                if ci >= len(chunk):
                    continue
                cat = chunk[ci]
                rows = grouped.get(cat, [])
                locked = _cat_is_locked(cat)
                if clickable_tile(_category_tile_html(cat, rows, locked, standard),
                                  key=f"{key_prefix}_{_disc_key(cat)}"):
                    if locked:
                        nav("pricing")
                    else:
                        # Drill-in switches the Discover view (home → category) via an
                        # in-place fragment rerun of the body — fast, no full-page reload.
                        # Request a one-shot scroll-to-top so we land on the category header.
                        st.session_state.discover_cat = cat
                        st.session_state["_disc_scroll_top"] = True
                        st.rerun(scope="fragment")


def _scroll_to_top():
    """Scroll the Streamlit main view back to the top. Used as a one-shot on category
    drill-in (the drill-in is an in-place fragment rerun, so the browser otherwise keeps
    the old scroll offset and leaves you mid-page).

    Rendered via components.html (a same-origin iframe) because scripts there are
    GUARANTEED to execute — st.html-injected <script> tags are not reliably run by the
    browser (innerHTML-inserted scripts don't execute), which is why drill-ins kept
    landing mid-page. The retry tail runs out to ~2.6s so the reset still wins after
    the category's content (quote refresh + cards) finishes streaming in.

    KEPT on components.html (deprecated) deliberately: reaching window.parent requires
    the srcdoc iframe's same-origin; st.iframe (data: URL) is opaque-origin and can't.
    See the pinned streamlit ceiling in requirements.txt."""
    components.html("""
    <script>
    const go = () => { try {
        const d = window.parent.document;
        const sel = 'section.main,[data-testid="stMain"],[data-testid="stAppViewContainer"],[data-testid="stMainBlockContainer"]';
        d.querySelectorAll(sel).forEach(e => { try { e.scrollTo(0, 0); } catch(_) { try { e.scrollTop = 0; } catch(__){} } });
        try { window.parent.scrollTo(0, 0); } catch(_) {}
        try { const se = d.scrollingElement; if (se) se.scrollTop = 0; } catch(_) {}
    } catch(_) {} };
    go(); [60, 200, 450, 900, 1600, 2600].forEach(t => setTimeout(go, t));
    </script>
    """, height=0)


def _render_discover_category(sel):
    """Drill-in view for one selected category: a back link, the category header (with
    our custom icon + definition), then the full results grid (reuses render_cat)."""
    # Scroll-to-top so a drill-in lands on the category header/description rather
    # than wherever the tile was (mid-page). Fires on the tile-click flag AND on any
    # category change (covers entry paths that don't set the flag — dashboard links,
    # deep links — which previously left the view stranded mid-page).
    if st.session_state.pop("_disc_scroll_top", False) or st.session_state.get("_disc_last_cat") != sel:
        _scroll_to_top()
    st.session_state["_disc_last_cat"] = sel
    if st.button("← All signals", key="disc_back_home"):
        st.session_state.discover_cat = DISCOVER_HOME
        st.rerun(scope="fragment")
    is_comp = sel in COMPOSITE_CATS
    tier_str = ""
    if is_comp:
        _, tier = COMPOSITE_CATS[sel]
        tcol = GOLD if tier == "premium" else GREEN
        tlbl = "PREMIUM" if tier == "premium" else "FREE"
        tbg = "rgba(245,158,11,.1)" if tier == "premium" else "rgba(34,197,94,.1)"
        tbd = "rgba(245,158,11,.25)" if tier == "premium" else "rgba(34,197,94,.25)"
        tier_str = f'<span class="disc-meta-pill" style="background:{tbg};color:{tcol};border-color:{tbd};">{tlbl}</span>'
    defn = cat_def(sel) or (COMPOSITE_CATS[sel][0] if is_comp else f"Browse all {_clean_name(sel)} stocks")
    _dh = (f"<div class='disc-cat-header'>"
           f"<div class='disc-cat-title dch-title'><span class='dch-ic'>{cat_icon(sel, 30)}</span>"
           f"<span>{_clean_name(sel)}</span></div>"
           f"<div class='disc-cat-desc'>{defn}</div><div class='disc-cat-meta'>{tier_str}"
           f"<span class='disc-meta-pill'>Polygon live data</span>")
    if HAS_EDGAR and EDGAR_ENABLED:
        _dh += "<span class='disc-meta-pill'>SEC insider &amp; 8-K</span>"
    _dh += "<span class='disc-meta-pill'>Background refresh</span></div></div>"
    st.markdown(_dh, unsafe_allow_html=True)
    if is_comp and COMPOSITE_CATS.get(sel, ("", None))[1] == "premium" and not is_premium():
        render_lock(sel)
    else:
        render_cat(sel, show_why=is_comp)


def _discover_lock_and_load_snaps(top):
    """Lock (record once) + load the percent-since-signal snapshots for the home Top
    Signals, keyed by ticker. Recording is idempotent — the entry price + timestamp
    lock on a signal's FIRST appearance, so the % keeps accumulating without resetting
    when the live price refreshes. CACHED per (warm, top-tickers) in session_state so a
    body rerun (theme toggle) skips the record + _load_recs I/O — that I/O was a chunk of
    the dropdown lag."""
    with _UNIVERSE_LOCK:
        _bid = _UNIVERSE_CACHE.get("built_at", 0)
    _ck = (_bid, tuple(r.get("t") for r in top))
    if _bid and st.session_state.get("_disc_snap_key") == _ck and "_disc_snap" in st.session_state:
        return st.session_state["_disc_snap"]
    snaps = {}
    try:
        by_cat = {}
        for r in top:
            c = r.get("primary_cat")
            if not c:
                continue
            q = r.get("q") or {}
            by_cat.setdefault(c, []).append((r.get("t", ""), q.get("price", 0),
                                             r.get("sc"), r.get("op"), r.get("why")))
        for c, items in by_cat.items():
            try:
                record_recommendations_bulk(c, items)
            except Exception:
                pass
        alls = _load_recs()
        for r in top:
            snaps[r.get("t")] = alls.get(_rec_key(r.get("primary_cat"), r.get("t")))
    except Exception:
        pass
    if _bid:
        st.session_state["_disc_snap"] = snaps
        st.session_state["_disc_snap_key"] = _ck
    return snaps


@st.fragment(run_every=120)
def _discover_body():
    # Auto-refreshing fragment (FIXED 120s cadence): only this body re-renders, in
    # place — the page chrome (topbar/CSS, outside the fragment) is untouched, which
    # is what killed the full-page flash the old streamlit_autorefresh rerun caused.
    # STATIC decoration is deliberate: wrapping dynamically at call time
    # (st.fragment(run_every=x)(fn)()) mints a NEW fragment identity every script
    # run, and the previous identity's scheduled auto-rerun could still fire and
    # APPEND a second copy of the board — the "duplicate Top Signals" bug. A fixed
    # cadence is the price of a stable identity; off-hours reruns are cheap (they
    # re-read the warm cache).
    st.markdown('<div class="page-wrap pw-narrow">',unsafe_allow_html=True)

    sel = st.session_state.get("discover_cat", DISCOVER_HOME)
    # Unknown/stale selections fall back to the home feed instead of erroring.
    is_home = (sel == DISCOVER_HOME) or (sel not in COMPOSITE_CATS and sel not in CATEGORIES)
    grouped = _discover_grouped()

    # ── HOME + no data yet → ONE calm, professional screen, nothing else ──
    # Rendering the preparing screen ALONE (no timer/banner/grid) is what keeps the
    # bounded auto-refresh from flashing: every rerun reuses this single identical
    # element instead of tearing down and rebuilding a multi-card grid.
    if is_home and not grouped:
        _in_prog = False
        try: _in_prog = _WARM_IN_PROGRESS
        except Exception: pass
        if universe_is_warming() or _in_prog:
            _render_preparing_screen()
            st.markdown('</div>', unsafe_allow_html=True)
            _maybe_warm_poll()   # gentle bounded poll; flips to live data when ready
            return
        # Attempt finished but produced nothing → terminal failed state (not polled).
        _render_home_failed()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # ── We have data (or we're in a category view): render the full chrome ──
    st.session_state["_warm_attempts"] = 0
    render_market_timer()
    rg = _safe_regime()
    _render_regime_banner(rg)

    # Drill-in view for a specific category (reached via "See all", the sidebar,
    # or a deep-link). Everything else is the guided Top-Signals home.
    if not is_home:
        _render_discover_category(sel)
        _discover_upsell_footer()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # ── HOME with data: guidance + cross-category Top Signals + themed browser ──
    _render_regime_guidance(rg)

    # TODAY's board means TODAY. This used to rank by conviction and backfill with anything
    # "not underwater", so it filled with picks that fired one and two weeks ago — correct
    # for a track record, wrong under a heading that says today. Cumulative since-signal
    # performance now lives on the Results page; this board is strictly what's moving
    # in the current session, measured in each signal's own direction.
    st.markdown('<div class="disc-section-label">Today\'s Signals</div>', unsafe_allow_html=True)
    st.markdown('<div class="disc-sub">What the engine is flagging <b style="color:#a5b4fc;">right now</b>, ranked by '
                'today\'s move and measured in each signal\'s own direction — a short counts a decline as a gain. For how '
                'earlier calls actually worked out, see <b style="color:#a5b4fc;">Results</b>. '
                'Tap any card for the full breakdown.</div>', unsafe_allow_html=True)
    # Sample every category before ranking, so one high-edge category can't fill the board.
    pool = _signal_candidates(grouped)
    snaps = _discover_lock_and_load_snaps(pool)
    top = _top_movers_today(pool, 6, per_cat=2)
    if not top:                      # flat/closed tape — nothing is up in its own direction
        top = _top_signals(grouped, 6)
    _render_conviction_grid(top, "ts", snaps)

    # ── Browse by signal (6 themed groups) ──
    st.markdown('<div class="disc-section-label" style="margin-top:26px;">Browse by Signal</div>', unsafe_allow_html=True)
    st.markdown('<div class="disc-sub">Each pick shows its 0–100 <b style="color:#a5b4fc;">Conviction Score</b> — the higher the number, the stronger and more confident the signal\'s match for that category. We surface the top 3 per category; the count badge is how many stocks are in it right now.</div>', unsafe_allow_html=True)
    _render_theme_browser(rg, grouped)

    # ── Free screens (curated sector / theme lists) ──
    if CATEGORIES:
        st.markdown('<div class="disc-section-label" style="margin-top:22px;">Free Screens</div>', unsafe_allow_html=True)
        std_grouped = {c: list(CATEGORIES.get(c, [])) for c in CATEGORIES}
        _render_category_grid(list(CATEGORIES.keys()), std_grouped, key_prefix="fs", standard=True)

    _discover_upsell_footer()
    st.markdown('</div>', unsafe_allow_html=True)  # close page-wrap


def _discover_upsell_footer():
    """Bottom upgrade nudge for free users (shared by both Discover views)."""
    if is_premium():
        return
    st.markdown('<div style="height:24px;"></div>', unsafe_allow_html=True)
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#1a0d00,#0d1525);border:1px solid rgba(245,158,11,0.25);
                border-radius:12px;padding:18px 24px;text-align:center;">
        <div style="font-size:14px;font-weight:700;color:{GOLD};margin-bottom:4px;">👑 Unlock All Premium Signals</div>
        <div style="font-size:12px;color:#374f6e;">Insider Cluster, Short Squeeze, Relative Strength, VCP, Quiet Accumulation & more — plus every locked Top Signal.</div>
    </div>
    """, unsafe_allow_html=True)
    if gold_btn("Upgrade to Premium →", "disc_upgrade_bottom"): nav("pricing")


def page_discover():
    render_topbar("discover")
    try:
        ensure_universe_worker()
        if universe_is_warming():
            _kick_background_warm()
    except Exception:
        pass
    # ── Automatic price refresh (no toggle — a live product should just be live) ──
    # FRAGMENT-scoped (see _discover_body's decorator): stats/prices update in place
    # every 2 minutes with no full-page rerun, so there's no screen flash.
    st.markdown(f"""<style>
    .disc-section-label{{font-size:11px;font-weight:800;color:#4a5e7a;letter-spacing:2.5px;
        text-transform:uppercase;margin:22px 0 6px;text-align:center;}}
    .disc-sub{{font-size:12px;color:#374f6e;text-align:center;max-width:560px;margin:0 auto 14px;line-height:1.45;}}
    /* ── Conviction card atom (Top Signals + teasers) ── */
    .cv-card{{background:linear-gradient(135deg,#0d1525,#0a0f1a);border:1px solid #1c2942;
        border-radius:12px;padding:12px 14px;min-height:120px;display:flex;flex-direction:column;
        gap:7px;margin-bottom:6px;transition:border-color .15s ease;}}
    .cv-card:hover{{border-color:rgba(99,102,241,0.5);}}
    .cv-top{{display:flex;justify-content:space-between;align-items:baseline;gap:6px;}}
    .cv-tick{{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;color:#f1f5f9;}}
    .cv-px{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;white-space:nowrap;}}
    .cv-conv{{display:flex;align-items:center;gap:8px;}}
    .cv-bar{{flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;}}
    .cv-fill{{height:6px;border-radius:3px;}}
    .cv-num{{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:800;min-width:24px;text-align:right;}}
    .cv-tag{{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:#a5b4fc;min-width:0;}}
    .cv-tag>span:first-of-type{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .cv-tag svg{{color:currentColor;flex-shrink:0;}}  /* inherits the per-theme accent */
    .cv-prem{{display:inline-flex;align-items:center;gap:3px;margin-left:auto;color:{GOLD};font-size:10px;flex-shrink:0;}}
    .cv-short{{font-size:8.5px;font-weight:800;letter-spacing:.6px;color:#fb7185;background:rgba(251,113,133,.12);
        border:1px solid rgba(251,113,133,.35);border-radius:5px;padding:1px 5px;flex-shrink:0;}}
    .cv-prem svg{{color:{GOLD};}}
    .cv-why{{font-size:11px;color:#6b7fa0;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;
        -webkit-box-orient:vertical;overflow:hidden;flex:1;}}
    .cv-perf{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;
        padding-top:7px;border-top:1px solid rgba(255,255,255,0.06);font-size:10.5px;}}
    .cv-perf-age{{color:#4a5e7a;}}
    /* ── 'Preparing your signals' warm screen (single, CSS-animated element) ── */
    .prep-wrap{{display:flex;justify-content:center;align-items:flex-start;padding:30px 0 60px;min-height:56vh;}}
    .prep-card{{background:linear-gradient(135deg,#0d1525,#080b14);border:1px solid #1c2942;border-radius:18px;
        padding:40px 44px;max-width:520px;text-align:center;box-shadow:0 10px 44px rgba(0,0,0,0.38);}}
    .prep-orbit{{display:flex;justify-content:center;align-items:flex-end;gap:7px;height:26px;margin-bottom:6px;}}
    .prep-dot{{width:9px;height:9px;border-radius:50%;background:#6366f1;display:inline-block;animation:prepbounce 1.2s ease-in-out infinite;}}
    .prep-dot:nth-child(2){{background:#6366f1;animation-delay:.15s;}}
    .prep-dot:nth-child(3){{background:#818cf8;animation-delay:.3s;}}
    @keyframes prepbounce{{0%,100%{{transform:translateY(0);opacity:.35;}}50%{{transform:translateY(-9px);opacity:1;}}}}
    .prep-title{{font-size:20px;font-weight:800;color:#f1f5f9;margin:16px 0 8px;letter-spacing:-0.3px;}}
    .prep-sub{{font-size:13px;color:#6b7fa0;line-height:1.6;max-width:430px;margin:0 auto 22px;}}
    .prep-bar{{height:5px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;margin:0 auto 20px;max-width:330px;}}
    .prep-bar-fill{{height:5px;width:38%;border-radius:3px;
        background:linear-gradient(90deg,transparent,#6366f1,#818cf8,#6366f1,transparent);animation:prepslide 1.5s ease-in-out infinite;}}
    @keyframes prepslide{{0%{{transform:translateX(-130%);}}100%{{transform:translateX(360%);}}}}
    .prep-steps{{display:flex;gap:9px;justify-content:center;flex-wrap:wrap;}}
    .prep-step{{font-size:11px;font-weight:700;color:#374f6e;background:rgba(99,102,241,0.06);
        border:1px solid rgba(99,102,241,0.15);border-radius:20px;padding:5px 13px;animation:prepglow 3.6s ease-in-out infinite;}}
    .prep-step:nth-child(2){{animation-delay:1.2s;}} .prep-step:nth-child(3){{animation-delay:2.4s;}}
    @keyframes prepglow{{0%,66%,100%{{color:#374f6e;border-color:rgba(99,102,241,0.15);background:rgba(99,102,241,0.06);}}
        12%,42%{{color:#a5b4fc;border-color:rgba(99,102,241,0.5);background:rgba(99,102,241,0.16);}}}}
    /* ── Regime guidance + theme rows ── */
    .rg-guide{{text-align:center;font-size:12.5px;color:#8da3c4;max-width:640px;margin:0 auto 16px;line-height:1.5;}}
    .rg-chips{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:9px;}}
    .rg-chip{{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:#cbd5e1;
        background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.3);border-radius:20px;padding:4px 12px;}}
    .rg-chip svg{{color:#a5b4fc;}}
    /* ── Themed collapsible headers (custom + clickable) ── */
    .th-head{{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#0d1525;
        border:1px solid {BORDER};border-radius:11px;padding:11px 15px;margin-bottom:6px;transition:all .15s ease;}}
    .th-head:hover{{border-color:rgba(99,102,241,0.45);background:#0f1830;}}
    .th-head-on{{border-color:rgba(99,102,241,0.4);background:linear-gradient(135deg,#0f1830,#0b1322);}}
    .th-head-l{{display:flex;align-items:center;gap:9px;min-width:0;}}
    /* icon color comes from the per-theme accent wrapper (currentColor) */
    .th-head-l svg{{flex-shrink:0;}}
    .th-ic{{display:inline-flex;flex-shrink:0;color:#a5b4fc;}}
    .th-head-n{{font-size:14px;font-weight:800;color:#ffffff;letter-spacing:.2px;}}
    .th-focus{{font-size:9px;font-weight:700;color:{GOLD};background:rgba(245,158,11,0.12);
        border:1px solid rgba(245,158,11,0.3);border-radius:10px;padding:2px 8px;text-transform:uppercase;letter-spacing:.5px;}}
    .th-head-r{{font-size:11px;color:#4a5e7a;font-weight:600;white-space:nowrap;display:flex;align-items:center;gap:6px;}}
    .th-chev{{color:#6b7fa0;font-size:12px;}}
    /* ── Clickable category tile — smooth lift on hover, accent left edge ── */
    .cat-tile{{background:linear-gradient(135deg,#0c1322,#0a0f1a);border:1px solid #1a2740;border-radius:11px;
        padding:12px 14px;min-height:98px;display:flex;flex-direction:column;gap:5px;margin-bottom:6px;
        transition:border-color .18s ease, transform .18s cubic-bezier(.22,1,.36,1), box-shadow .18s ease;}}
    .cat-tile:hover{{border-color:rgba(99,102,241,0.5);transform:translateY(-2px);
        box-shadow:0 8px 22px rgba(0,0,0,0.35);}}
    .cat-tile-lock{{opacity:.92;}}
    .ct-h{{display:flex;align-items:center;gap:9px;}}
    .ct-ic{{display:inline-flex;color:#818cf8;flex-shrink:0;}}
    .ct-n{{font-size:14px;font-weight:800;color:#ffffff;letter-spacing:.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .ct-count{{margin-left:auto;font-size:10px;font-weight:800;color:#a5b4fc;background:rgba(99,102,241,0.14);border-radius:9px;padding:1px 8px;flex-shrink:0;}}
    .ct-d{{font-size:11px;color:#6b7fa0;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
    .ct-f{{display:flex;gap:9px;flex-wrap:wrap;align-items:center;font-size:11px;margin-top:auto;padding-top:6px;}}
    .ct-pick{{display:inline-flex;align-items:center;gap:5px;}}
    .ct-pick b{{color:#f4f7ff;font-weight:800;font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.4px;}}
    .ct-sc{{font-size:10px;font-weight:800;font-family:'JetBrains Mono',monospace;border-radius:6px;padding:1px 6px;}}
    .ct-prem{{display:inline-flex;align-items:center;gap:4px;color:{GOLD};font-family:'Inter',sans-serif;}}
    .ct-prem svg{{color:{GOLD};}}
    .disc-cat-header{{background:linear-gradient(135deg,#0d1525 0%,#080b14 100%);
        border:1px solid {BORDER};border-radius:16px;padding:26px 30px;margin-bottom:16px;
        box-shadow:0 2px 16px rgba(0,0,0,0.25);text-align:center;}}
    .disc-cat-title{{font-size:30px;font-weight:900;color:#f1f5f9;letter-spacing:-0.8px;margin-bottom:8px;}}
    .dch-title{{display:inline-flex;align-items:center;gap:12px;justify-content:center;}}
    .dch-ic{{display:inline-flex;color:#a5b4fc;}}
    .dch-ic svg{{width:30px;height:30px;}}
    .disc-cat-desc{{font-size:14px;color:#6b7fa0;line-height:1.55;max-width:640px;margin:0 auto;}}
    .disc-cat-meta{{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;justify-content:center;}}
    .disc-meta-pill{{font-size:11px;font-weight:600;padding:5px 13px;border-radius:20px;
        background:rgba(99,102,241,0.08);color:#a5b4fc;border:1px solid rgba(99,102,241,0.2);}}
    /* Category buttons inherit the global .stButton style (40px, consistent
       across the whole app). We only add nowrap so long category names don't
       wrap — NOT a different height, which previously made card buttons on
       this page inconsistent with the rest of the site. */
    [data-testid="stMainBlockContainer"] .stButton>button{{
        white-space:nowrap !important;overflow:hidden !important;text-overflow:ellipsis !important;}}
    </style>""", unsafe_allow_html=True)
    _discover_body()

# ─────────────────────────────────────────────────────────────
# PAGE: STOCK DETAIL
# ─────────────────────────────────────────────────────────────
@st.fragment(run_every=120)
def _signal_perf_box(ticker, ticker_signals, price_hint):
    """The '{Long|Short} since signal' card. An auto-refreshing FRAGMENT (fixed 120s;
    statically decorated — a dynamic st.fragment(...)(fn) wrap mints a new fragment
    identity every run, whose orphaned timers duplicated content elsewhere).
    On each tick it re-reads the ticker's warm-row quote —
    which the scan worker refreshes in place every few minutes — so 'Current' and the
    return keep tracking the live scan while you sit on the page, with no full-page
    rerun (no flash).

    Direction is DERIVED from the signal itself — a bearish/short category
    (Breakdown, Distribution, Overbought Fade, …) measures a short's return;
    everything else measures long. No user choice: the signal defines the trade.

    Entry anchor: the most recent signal-engine event for this ticker; when none
    exists yet (e.g. the worker was down when the pick first surfaced) it falls back
    to the LOCKED Discover snapshot (recs store), so this card shows the same
    entry/timestamp as the '% since' on the cards instead of claiming 'no prior
    signal' while the card right above said 'Signaled 1d ago'."""
    # Freshest price: warm row first (live-refreshed), click-time hint as fallback.
    price = price_hint or 0
    _wr = _warm_row_for(ticker)
    if _wr and ((_wr.get("q") or {}).get("price") or 0) > 0:
        price = _wr["q"]["price"]

    _dir_cat = (ticker_signals[0].get("category", "") or "") if ticker_signals else ""
    if not _dir_cat and _wr:
        _dir_cat = _wr.get("primary_cat") or ""
    try:
        # category_dir returns "bear"/"bull" — the old comparison against "short"
        # could never be true, so every signal displayed as LONG. Fixed.
        dir_key = "short" if category_dir(_dir_cat) == "bear" else "long"
    except Exception:
        dir_key = "long"
    direction = "Short (Short-Sell)" if dir_key == "short" else "Long (Buy)"
    st.markdown(f'<div style="display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;'
                f'color:{"#fb7185" if dir_key=="short" else "#4ade80"};background:{"rgba(251,113,133,.1)" if dir_key=="short" else "rgba(34,197,94,.1)"};'
                f'border:1px solid {"rgba(251,113,133,.3)" if dir_key=="short" else "rgba(34,197,94,.3)"};border-radius:20px;'
                f'padding:4px 12px;margin-bottom:10px;">{"📉 SHORT position" if dir_key=="short" else "📈 LONG position"}'
                f'<span style="color:#4a5e7a;font-weight:600;">· set by the signal type</span></div>',
                unsafe_allow_html=True)

    # ── Resolve the entry anchor: signal event → locked Discover snapshot → today ──
    entry_price = None; sig_ts = None; tracked = None
    if ticker_signals:
        most_recent = ticker_signals[0]
        entry_price = most_recent.get("trigger_price", price) or price
        tracked = (most_recent.get("outcomes") or {}).get("current_pct")
        try: sig_ts = datetime.fromisoformat(most_recent.get("triggered_at", "")).timestamp()
        except Exception: sig_ts = None
    else:
        snap = None
        try:
            if _dir_cat:
                snap = get_recommendation_snapshot(_dir_cat, ticker)
            if not snap:
                snap = get_recommendation_snapshot("__universe__", ticker)
        except Exception:
            snap = None
        if snap and (snap.get("entry_price") or 0) > 0:
            entry_price = snap["entry_price"]
            sig_ts = snap.get("triggered_at")
    if entry_price:
        _stamp = _signal_stamp(sig_ts) if sig_ts else ""
        _when = _stamp or "an earlier scan"
        st.caption(f"Measured from the Scanviction signal on {_when} · Entry ${entry_price:.2f}")
    else:
        entry_price = price
        st.caption(f"Measured from today's price (signal just flagged — tracking starts now)")
    # Held: exact age from the signal timestamp — hours/minutes on day one instead
    # of a flat '0d'.
    if sig_ts:
        _secs = max(0, time.time() - float(sig_ts))
        held_str = (f"{int(_secs//86400)}d" if _secs >= 86400
                    else f"{int(_secs//3600)}h" if _secs >= 3600 else f"{max(1, int(_secs//60))}m")
    else:
        held_str = "0d"

    # LONG return since entry. Normally from the live price — but if a TRACKED snapshot
    # (outcomes.current_pct) exists and the live quote disagrees with it by a lot, the
    # live quote is unreliable (stale / bad data / demo-seed entry) so we fall back to
    # the tracked return. This keeps this card CONSISTENT with the Track Record above
    # instead of showing a nonsensical figure (e.g. -61% on a freshly-flagged signal).
    live_ret = ((price - entry_price) / entry_price * 100) if entry_price else 0.0
    if tracked is not None and abs(live_ret - float(tracked)) > 12:
        long_ret = float(tracked)
    else:
        long_ret = live_ret
    cur_price = price
    ret_pct = long_ret if dir_key == "long" else -long_ret
    pos = ret_pct >= 0
    pcol = "#4ade80" if pos else "#f87171"
    pbg = "rgba(34,197,94,0.08)" if pos else "rgba(239,68,68,0.08)"
    st.markdown(f"""
    <div style="background:{pbg};border:1px solid {pcol}44;border-radius:12px;padding:18px 20px;margin-bottom:12px;">
        <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:12px;">{direction} since signal</div>
        <div style="display:flex;gap:28px;flex-wrap:wrap;align-items:center;">
            <div><div style="font-size:11px;color:#374f6e;margin-bottom:2px;">Return since signal</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:900;color:{pcol};">{'+' if pos else ''}{ret_pct:.2f}%</div></div>
            <div><div style="font-size:11px;color:#374f6e;margin-bottom:2px;">Entry</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#818cf8;">${entry_price:.2f}</div></div>
            <div><div style="font-size:11px;color:#374f6e;margin-bottom:2px;">Current</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#e2e8f0;">${cur_price:.2f}</div></div>
            <div><div style="font-size:11px;color:#374f6e;margin-bottom:2px;">Held</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#e2e8f0;">{held_str}</div></div>
        </div>
        <div style="font-size:10px;color:#374f6e;margin-top:10px;">Price auto-updates with the scan — every few minutes during market hours.</div>
    </div>
    """, unsafe_allow_html=True)


def _warm_index():
    """{ticker: row} over the warm universe, CACHED per-warm in session_state (keyed by
    the universe build time). Turns repeated per-ticker lookups (every detail open / demo
    price / stale-quote patch) from an O(2,500) scan into an O(1) dict hit. The values are
    the live row objects, so in-place quote patches stay reflected."""
    with _UNIVERSE_LOCK:
        bid = _UNIVERSE_CACHE.get("built_at", 0)
    if bid and st.session_state.get("_warm_idx_at") == bid and "_warm_idx" in st.session_state:
        return st.session_state["_warm_idx"]
    idx = {}
    try:
        for r in build_scored_universe():
            t = r.get("t")
            if t and t not in idx:
                idx[t] = r
    except Exception:
        pass
    if bid:
        st.session_state["_warm_idx"] = idx
        st.session_state["_warm_idx_at"] = bid
    return idx

def _warm_row_for(ticker):
    """The warm-scan row for a ticker (its correct, consistent Polygon quote + daily
    bars + info), or None if it isn't in today's scan. Lets the detail page use scan
    data on ANY navigation path (e.g. the Signals feed's quote-less 'View') instead of
    re-fetching a per-ticker live quote that can be unreliable for off-scan names."""
    if not ticker:
        return None
    return _warm_index().get(ticker)


def _demo_seed_enabled():
    """Auto-seeding of DEMO signal history is OPT-IN (SEED_DEMO_SIGNALS=1) and OFF by
    default. The old always-on seeding polluted production: the Signal Engine panel
    showed one fake 1W/0L '100% win rate' row per category (seeded winners) instead of
    the real tracked record. Real signals accrue from the scan worker; use the admin
    'Purge Demo Data' button to clear any previously seeded rows."""
    return _os.environ.get("SEED_DEMO_SIGNALS", "0").lower() in ("1", "true", "yes")


def _demo_price(t):
    """Live price for a ticker used to anchor demo signal entries — warm scan first
    (consistent with the rest of the app), then a per-ticker quote. 0 if unavailable."""
    try:
        wr = _warm_row_for(t)
        if wr:
            p = (wr.get("q") or {}).get("price")
            if p:
                return float(p)
        q = get_quote(t)
        return float((q or {}).get("price") or 0)
    except Exception:
        return 0


def _signal_pattern_family(cat):
    """Map a composite signal category to a chart-pattern family so we can draw the
    right geometry (a Breakout gets a resistance line + breakout arrow, a Squeeze gets
    compressed Bollinger bands, a Distribution gets a lower-highs trendline, etc.)."""
    c = cat or ""
    if "Breakdown" in c: return "breakdown"
    if "Distribution" in c or "Overbought Fade" in c: return "lower_highs"
    if "Volatility Squeeze" in c or "VCP" in c: return "squeeze"
    if "Pullback" in c or "Quiet Accumulation" in c: return "pullback"
    if "Oversold Reversal" in c or "Fallen Angels" in c or "Capitulation" in c: return "reversal"
    if any(k in c for k in ("Breakout", "Relative Strength", "Momentum", "Expansion",
                            "Catalyst", "Short Squeeze", "Smart-Money")):
        return "breakout"
    return "levels"

_PATTERN_LABEL = {"breakout": "Breakout", "breakdown": "Breakdown", "squeeze": "Volatility Squeeze",
                  "pullback": "Pullback to Support", "reversal": "Reversal Off Lows",
                  "lower_highs": "Distribution", "levels": "Key Levels"}

def _detect_flag(c):
    """Heuristic bull-flag detector: a sharp POLE (>=8% over <=8 bars) followed by a
    TIGHTER consolidation (the flag). Returns (pole_start, pole_end, flag_end, gain) or
    None. `c` is a list of closes. Pure geometry — no external libs."""
    n = len(c); best = None
    for ps in range(max(0, n - 26), n - 6):
        base = c[ps]
        if not base: continue
        for pe in range(ps + 3, min(ps + 9, n - 3)):
            gain = (c[pe] - base) / base
            if gain >= 0.08:
                flag = c[pe:]
                if len(flag) >= 3:
                    fmax, fmin = max(flag), min(flag)
                    rng = (fmax - fmin) / fmax if fmax else 1.0
                    if rng <= 0.10 and rng <= gain * 0.7 and (best is None or gain > best[3]):
                        best = (ps, pe, n - 1, gain)
    return best

def render_signal_chart(df, cat, factors, q, ticker):
    """Draw the OHLC candles with the DETECTED signal geometry drawn ON the chart —
    resistance/support lines, breakout/breakdown arrows, a regression channel, a bull-
    flag pole+box, compressed squeeze bands, or a reversal marker, chosen by the stock's
    primary signal category. Returns True if it rendered. Fully guarded so a charting
    hiccup never breaks the detail page."""
    if not HAS_PLOTLY or df is None or len(df) < 20:
        return False
    try:
        import numpy as np
        factors = factors or {}
        d = df.tail(70).reset_index(drop=True).copy()
        n = len(d)
        has_dt = "datetime" in d.columns
        x = d["datetime"] if has_dt else pd.Series(range(n))
        close = d["close"].astype(float); high = d["high"].astype(float); low = d["low"].astype(float)
        fam = _signal_pattern_family(cat)
        vr = factors.get("vol_ratio")

        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=x, open=d["open"], high=high, low=low, close=close,
            increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
            increasing_fillcolor="rgba(34,197,94,0.45)", decreasing_fillcolor="rgba(239,68,68,0.45)",
            name="", showlegend=False))
        ma20 = close.rolling(20).mean(); ma50 = close.rolling(min(50, n)).mean()
        fig.add_trace(go.Scatter(x=x, y=ma20, line=dict(color=GOLD, width=1, dash="dot"), name="MA20"))
        fig.add_trace(go.Scatter(x=x, y=ma50, line=dict(color="#64748b", width=1, dash="dot"), name="MA50"))

        def hline(y, color, text, pos):
            fig.add_hline(y=y, line=dict(color=color, width=1.2, dash="dash"),
                          annotation_text=text, annotation_position=pos,
                          annotation_font=dict(color=color, size=11))

        def arrow(i, kind, label, color):
            yv = float(low.iloc[i]) * 0.985 if kind == "up" else float(high.iloc[i]) * 1.015
            sym = "triangle-up" if kind == "up" else "triangle-down"
            pos = "bottom center" if kind == "up" else "top center"
            fig.add_trace(go.Scatter(x=[x.iloc[i]], y=[yv], mode="markers+text",
                marker=dict(symbol=sym, size=15, color=color, line=dict(color="#fff", width=1)),
                text=[label], textposition=pos, textfont=dict(color=color, size=11), showlegend=False))

        def add_channel(col):
            idx = np.arange(n); m, b = np.polyfit(idx, close.values, 1)
            fit = m * idx + b; resid = close.values - fit
            fig.add_trace(go.Scatter(x=x, y=fit + resid.max(), line=dict(color=col, width=1), name="", showlegend=False))
            fig.add_trace(go.Scatter(x=x, y=fit + resid.min(), line=dict(color=col, width=1),
                fill="tonexty", fillcolor="rgba(99,102,241,0.06)", name="", showlegend=False))

        flag = _detect_flag(close.values.tolist()) if fam in ("breakout", "pullback") else None
        label = _PATTERN_LABEL.get(fam, "Signal"); caption = ""

        if fam == "breakout":
            res = float(high.iloc[:-3].tail(25).max())
            hline(res, "rgba(248,113,113,0.7)", f"Breakout ${res:,.2f}", "top left")
            add_channel("rgba(99,102,241,0.45)")
            bo = next((i for i in range(max(0, n - 8), n) if close.iloc[i] > res), None)
            if bo is not None: arrow(bo, "up", "Breakout", "#22c55e")
            caption = f"Pressing above ${res:,.2f} resistance" + (f" on {vr:.1f}× volume." if vr else ".")
        elif fam == "breakdown":
            sup = float(low.iloc[:-3].tail(25).min())
            hline(sup, "rgba(248,113,113,0.7)", f"Support ${sup:,.2f}", "bottom left")
            add_channel("rgba(239,68,68,0.35)")
            bd = next((i for i in range(max(0, n - 8), n) if close.iloc[i] < sup), None)
            if bd is not None: arrow(bd, "down", "Breakdown", "#ef4444")
            caption = f"Broke below ${sup:,.2f} support" + (f" on {vr:.1f}× volume." if vr else ".")
        elif fam == "squeeze":
            mid = close.rolling(20).mean(); sd = close.rolling(20).std()
            fig.add_trace(go.Scatter(x=x, y=mid + 2 * sd, line=dict(color="rgba(129,140,248,0.5)", width=1), name="", showlegend=False))
            fig.add_trace(go.Scatter(x=x, y=mid - 2 * sd, line=dict(color="rgba(129,140,248,0.5)", width=1),
                fill="tonexty", fillcolor="rgba(129,140,248,0.08)", name="", showlegend=False))
            caption = "Bollinger bands compressed to a tight range — coiled for an expansion."
        elif fam == "reversal":
            lo_i = int(low.iloc[-20:].idxmin())
            arrow(lo_i, "up", "Reversal", "#22c55e")
            fig.add_shape(type="line", x0=x.iloc[lo_i], y0=float(low.iloc[lo_i]), x1=x.iloc[n - 1], y1=float(close.iloc[n - 1]),
                line=dict(color="rgba(34,197,94,0.7)", width=1.5, dash="dot"))
            caption = f"Bounced off ${float(low.iloc[lo_i]):,.2f} as momentum turned back up."
        elif fam == "lower_highs":
            half = max(2, n // 2)
            h1 = int(high.iloc[:half].idxmax()); h2 = int(high.iloc[half:].idxmax())
            if h2 > h1:
                slope = (float(high.iloc[h2]) - float(high.iloc[h1])) / max(1, (h2 - h1))
                y_end = float(high.iloc[h1]) + slope * ((n - 1) - h1)
                fig.add_shape(type="line", x0=x.iloc[h1], y0=float(high.iloc[h1]), x1=x.iloc[n - 1], y1=y_end,
                    line=dict(color="rgba(248,113,113,0.7)", width=1.5, dash="dash"))
            caption = "Lower highs as money flows out — a distribution / fade setup."
        elif fam == "pullback":
            touch = next((i for i in range(n - 1, max(0, n - 8), -1)
                          if not pd.isna(ma20.iloc[i]) and low.iloc[i] <= ma20.iloc[i] * 1.01), None)
            if touch is not None: arrow(touch, "up", "Pullback", "#818cf8")
            caption = "Pulled back to the rising 20-day MA — a buy-the-dip zone."
        else:  # levels
            res = float(high.iloc[:-1].tail(20).max()); sup = float(low.iloc[:-1].tail(20).min())
            hline(res, "rgba(248,113,113,0.6)", f"Resistance ${res:,.2f}", "top left")
            hline(sup, "rgba(52,211,153,0.6)", f"Support ${sup:,.2f}", "bottom left")
            caption = f"Trading between ${sup:,.2f} support and ${res:,.2f} resistance."

        if flag is not None:
            ps, pe, fe, gain = flag
            fig.add_trace(go.Scatter(x=[x.iloc[ps], x.iloc[pe]], y=[float(close.iloc[ps]), float(close.iloc[pe])],
                mode="lines", line=dict(color="rgba(34,197,94,0.9)", width=3), name="", showlegend=False))
            fbox = close.iloc[pe:]
            fig.add_shape(type="rect", x0=x.iloc[pe], x1=x.iloc[n - 1], y0=float(fbox.min()), y1=float(fbox.max()),
                line=dict(color="rgba(129,140,248,0.7)", width=1), fillcolor="rgba(129,140,248,0.07)")
            label = "Bull Flag"
            caption = f"Bull flag — a {gain * 100:.0f}% pole into a tight consolidation, primed to continue. " + caption

        xaxis = dict(showgrid=False, color="#4a5e7a", tickfont=dict(size=10))
        if has_dt:
            xaxis["rangebreaks"] = [dict(bounds=["sat", "mon"])]
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=8, b=0), height=330, xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, bgcolor="rgba(0,0,0,0)", font=dict(color="#6b7fa0", size=11)),
            xaxis=xaxis,
            yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.04)", color="#4a5e7a", tickfont=dict(size=10)))

        st.markdown(f'<div class="sec-hd">📐 Signal on the Chart — <span style="color:#a5b4fc;">{label}</span></div>', unsafe_allow_html=True)
        st.plotly_chart(fig, use_container_width=True, key=f"sigchart_{ticker}")
        if caption:
            st.markdown(f'<div style="font-size:12px;color:#6b7fa0;margin:-6px 0 6px;">{caption}</div>', unsafe_allow_html=True)
        return True
    except Exception:
        return False


def page_detail():
    render_topbar()
    st.session_state["_seen_detail"] = True   # onboarding: opening a signal detail = activation step 2
    st.markdown('<div class="page-wrap">',unsafe_allow_html=True)
    ticker=st.session_state.get("detail_ticker")
    data=st.session_state.get("detail_data",{})

    # ── Track in Recently Viewed (per-user) ──
    if ticker and is_authed():
        try:
            rv = st.session_state.get("recently_viewed", [])
            # Remove if already in list, then prepend
            rv = [t for t in rv if t != ticker]
            rv.insert(0, ticker)
            rv = rv[:10]  # Keep last 10
            st.session_state.recently_viewed = rv
            # Persist to user DB
            uemail = st.session_state.user["email"]
            if uemail in st.session_state.users_db:
                st.session_state.users_db[uemail]["recently_viewed"] = rv
        except Exception:
            pass

    # ── Back button ──
    back_button("back_det")
    _bcc, bc2, _ = st.columns([1,1,5])
    with bc2:
        ticker_for_wl = st.session_state.get("detail_ticker","")
        wl = st.session_state.get("watchlist",[])
        in_wl = ticker_for_wl in wl
        if st.button("✅ Watching" if in_wl else "➕ Watchlist", key="top_wl_det", use_container_width=True):
            if in_wl: wl.remove(ticker_for_wl)
            else: wl.append(ticker_for_wl)
            st.session_state.watchlist = wl
            if is_authed():
                db = st.session_state.users_db.get(st.session_state.user["email"],{})
                db["watchlist"] = wl
                save_user_to_file(st.session_state.user["email"], db)
            st.rerun()
    if not ticker: st.warning("No stock selected."); return

    # Resolve the ticker's data. When opened WITHOUT a payload (e.g. the Signals feed
    # "View" sets detail_data={}), pull from the warm scan first — that's the correct,
    # consistent Polygon price + daily bars for any of the ~2,500 scanned names. Only
    # fall back to a per-ticker live fetch (less reliable for off-scan tickers) when the
    # name genuinely isn't in the scan.
    q=data.get("q"); df=data.get("df"); info=data.get("info"); sent=data.get("sent")
    # ALWAYS look up the warm row (not only when the payload is missing pieces): the
    # click-time payload's quote is a snapshot frozen at the moment the card was
    # clicked, while the warm row's quote keeps being refreshed in place by the scan
    # worker every few minutes. Preferring the warm quote is what makes the price on
    # this page (and the Signal Performance "Current") track the 15-minute scan
    # instead of staying stuck at the click-time number.
    _wr=_warm_row_for(ticker)
    if _wr:
        if _wr.get("q"): q = _wr["q"]
        if df is None: df=_wr.get("df")
        info = info or _wr.get("info")
        sent = sent or _wr.get("sent")
    # Anything still missing needs a slow per-ticker network fetch (cold OHLCV / .info /
    # sentiment). Show a spinner so a cold open feels responsive instead of a frozen stale
    # page. A WARM open fills everything above, so this block is a no-op and the spinner never
    # visibly renders (the `with` exits in ~0ms).
    with st.spinner(f"Loading {ticker}…"):
        q=q or get_quote(ticker)
        if df is None: df=yf_ohlcv(ticker,90)
        # A NON-warm ticker (no warm row) has no short-interest data, which the squeeze score
        # needs — so it must come from the full yf_fund(.info) here (fast_info lacks it, and
        # dropping it would change the score/recommendation away from the signal that led here).
        # Warm tickers (the real-alert case) skip this — they already carry short interest from
        # the warm row and only top up mktcap/52W from FAST fast_info in the merge below.
        info=info or yf_fund(ticker)
        sent=sent or st_sent(ticker)
    # The warm 'info' is a PARTIAL dict (Polygon price/technicals + EPS-derived P/E) missing
    # market cap + 52-week range. Fill those from yfinance's FAST fast_info endpoint (not the
    # slow full .info scrape, which made opening a stock take 1-3s) so market cap is correct
    # for the risk calc + display without blocking the page. (sector/beta would need the slow
    # .info; they stay absent here — not worth a multi-second wait per click.)
    if isinstance(info, dict) and not (info.get("mktcap") and info.get("hi52")):
        _ff = yf_fast_fund(ticker)
        info = dict(info)
        for _k in ("mktcap", "hi52", "lo52"):
            if not info.get(_k) and _ff.get(_k):
                info[_k] = _ff[_k]
    # Compute the indicator series ONCE and share it (this page used to recompute the full
    # indicator stack 4-5x per open). For a WARM ticker (the real-alert case) reuse the row's
    # ALREADY-computed score + factors and its sentiment — the detail then opens FAST (no slow
    # real-sentiment re-fetch, no re-score) AND matches the feed/Discover score exactly. Only a
    # NON-warm ticker pays the real-sentiment fetch + full compute (it has no warm score to reuse).
    _ind = precompute_indicators(df)
    if _wr and _wr.get("bd") and _wr.get("factors") is not None:
        _factors = _wr["factors"]
        sc, bd, op, risk, conf = _wr["sc"], _wr["bd"], _wr["op"], _wr["risk"], _wr["conf"]
    else:
        if isinstance(sent, dict) and sent.get("src") == "bulk":
            sent = st_sent(ticker)   # non-warm: upgrade bulk -> real sentiment for the score
        _factors = compute_factors(df, ind=_ind)
        sc, bd, op, risk, conf = compute_scores(df, info, sent, ind=_ind)
    ig = get_insights(df, info, ind=_ind)
    rec_lbl,rec_clr,rec_txt=get_recommendation(sc,bd,info)
    hot=ticker in st_hot()
    if not q: st.error(f"Could not load {ticker}."); return

    pct=q.get("pct",0); price=q.get("price",0); prev=q.get("prev",0); chg=q.get("chg",0)
    cc=GREEN if pct>0 else RED if pct<0 else "#6b7fa0"; ar="▲" if pct>0 else "▼" if pct<0 else "•"
    rc=risk_color(risk); sf=(info.get("sf",0) or 0)*100
    mc_v=info.get("mktcap",0)
    mc_s=fmt_mktcap(mc_v)
    # Volatility-based suggested stop (ATR-sized; direction from the warm row's long/short call).
    _stop_dir = "short" if (_wr or {}).get("direction") == "bear" else "long"
    _stop = recommended_stop(price, (_factors or {}).get("atr_pct", 0), _stop_dir)
    _stop_line = (f'<div style="margin-top:4px;font-size:11px;color:#94a3b8;">🛑 Suggested stop '
                  f'<b style="color:#e2e8f0;">${_stop["price"]:,.2f}</b> · {_stop["pct"]:.1f}% '
                  f'{"above" if _stop_dir == "short" else "below"} '
                  f'<span style="color:#2a3a52;">({STOP_ATR_MULT:g}× ATR)</span></div>') if _stop else ""

    st.markdown('<div class="page-wrap">' ,unsafe_allow_html=True)

    # Report header
# Report header
    h1,h2,h3=st.columns([3,2,2],gap="small")
    with h1:
        hot_b='<span class="b b-hot">🔥 HOT</span>' if hot else ""
        _sec = (info.get('sector') or '').strip(); _ind = (info.get('industry') or '').strip()
        _sec_line = (f'<div style="font-size:12px;color:#2a3a52;">{_sec}'
                     f'{" · " + _ind if (_ind and _ind != "N/A") else ""}</div>'
                     if (_sec and _sec != "N/A") else "")   # hide a bare "N/A · N/A"
        st.markdown(f"""<div style="padding:4px 0;">
<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
<span style="font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:800;color:#818cf8;">{ticker}</span>{hot_b}
<span style="display:inline-block;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:800;background:{rec_clr}22;color:{rec_clr};border:1px solid {rec_clr}44;">{rec_lbl}</span>
</div>
<div style="font-size:15px;color:#4a5e7a;margin-bottom:2px;">{q.get('name','')}</div>
{_sec_line}
<div style="margin-top:8px;font-size:13px;color:#374f6e;font-style:italic;">→ {rec_txt}</div>
<div style="margin-top:6px;font-size:11px;color:{rc};">⚡ {risk} Risk · {conf} confidence</div>
{_stop_line}
</div>""", unsafe_allow_html=True)
    with h2:
        st.markdown(f"""<div style="text-align:right;padding:4px 0;">
<div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:800;color:#e2e8f0;letter-spacing:-1px;">${price:,.2f}</div>
<div style="font-size:17px;font-weight:700;color:{cc};">{ar} ${abs(chg):.2f} ({abs(pct):.2f}%)</div>
<div style="font-size:12px;color:#2a3a52;margin-top:4px;">Prev close: ${prev:,.2f}</div>
</div>""", unsafe_allow_html=True)
    with h3:
        sc_c=GREEN if sc>=65 else GOLD if sc>=40 else RED
        sc_bg="#04200d" if sc>=65 else "#1a1000" if sc>=40 else "#200404"
        st.markdown(f"""<div style="background:{sc_bg};border:1px solid {sc_c};border-radius:10px;padding:16px;text-align:center;">
<div style="font-family:'JetBrains Mono',monospace;font-size:42px;font-weight:800;color:{sc_c};">{sc}</div>
<div style="font-size:10px;color:{sc_c};text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Scanviction Score</div>
<div style="font-size:11px;color:#2a3a52;margin-top:4px;">{op}</div>
</div>""", unsafe_allow_html=True)

    st.divider()

    # Session stats. 52-week high/low: prefer the real fundamental; else fall back to the
    # high/low of the daily bars we already have (a recent-window proxy); else HIDE the cell
    # rather than show a misleading $0.00 (e.g. a mega-cap reading "52W High $0.00").
    _hi52 = info.get("hi52") or 0; _lo52 = info.get("lo52") or 0
    if (not _hi52 or not _lo52) and df is not None and len(df):
        try:
            if not _hi52 and "high" in df.columns: _hi52 = float(df["high"].max())
            if not _lo52 and "low"  in df.columns: _lo52 = float(df["low"].min())
        except Exception: pass
    _px = lambda v: (f"${float(v):,.2f}" if v else "—")       # hide a misleading $0.00
    _pe = info.get("pe"); _avgvol = info.get("avgvol") or 0; _vol = q.get("volume", 0) or 0
    s_items=[("Open",_px(q.get('open')),None),("High",_px(q.get('high')),GREEN),
             ("Low",_px(q.get('low')),RED),
             ("Volume",f"{_vol/1e6:.2f}M" if _vol else "—",None),
             ("vs Avg",f"{_vol/_avgvol:.1f}×" if (_vol and _avgvol) else "—",None),   # no /1 ratio
             ("Mkt Cap",mc_s,None)]
    if _hi52: s_items.append(("52W High",f"${_hi52:,.2f}",GREEN))
    if _lo52: s_items.append(("52W Low",f"${_lo52:,.2f}",RED))
    s_items += [("P/E",f"{_pe:.1f}×" if _pe else "N/A",None),     # was raw unrounded float
                ("Short Float",f"{sf:.1f}%",RED if sf>=20 else None)]
    # One flex-wrap grid (NOT st.columns) so the stats keep their READING ORDER when they
    # wrap on mobile — st.columns stacks in column order, which scrambled them to
    # Open, Mkt Cap, High, 52W High, Low, 52W Low, ...
    _stat_cells = "".join(
        f'<div class="stat"><div class="stat-l">{lbl}</div>'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:14px;font-weight:700;color:#e2e8f0;">{val}</div>'
        f'</div>'
        for lbl, val, _vc in s_items)
    st.markdown(f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));'
                f'gap:8px;margin-bottom:8px;">{_stat_cells}</div>', unsafe_allow_html=True)

    st.markdown("<br>",unsafe_allow_html=True)

    # ── Recent alerts for this ticker (immediate "what just fired here") ──
    if HAS_SIGNAL_ENGINE:
        if _demo_seed_enabled():
            try: seed_demo_signal_history(_demo_price)   # idempotent — opt-in via SEED_DEMO_SIGNALS
            except Exception: pass
        try: _alerts = get_ticker_signal_history(ticker, limit=8)
        except Exception: _alerts = []
        if _alerts:
            # Dedup the strip: the signal history can carry the same (ticker, category) more
            # than once, which rendered as identical duplicate chips. _alerts is newest-first,
            # so keep only the most recent event per category for this compact glance.
            # Gate premium signals for free users (same tiering as the feed/Discover) so the
            # strip never surfaces a premium alert a free account couldn't access elsewhere.
            _alerts = [_s for _s in _alerts if not _signal_is_locked(_s.get("category", "Signal"))]
            _seen_cat = set(); _dedup = []
            for _s in _alerts:
                _c = _s.get("category", "Signal")
                if _c not in _seen_cat:
                    _seen_cat.add(_c); _dedup.append(_s)
            _alerts = _dedup
            _chips = ""
            for sig in _alerts[:6]:
                _cat = sig.get("category", "Signal")
                try: _age = _humanize_age(datetime.fromisoformat(sig.get("triggered_at", "")).timestamp())
                except Exception: _age = ""
                _rec = (sig.get("recommendation", "") or "").strip()
                if _cat in EVENT_ALERT_TYPES:
                    _sub = _rec or "Filing / data event"
                else:
                    # score_at_trigger is the composite Scanviction Score (sc) for a category
                    # entry — label it "Score" (matching the header) and contrast it with the LIVE
                    # sc, the SAME metric (NOT the differently-scaled conviction_score). Makes the
                    # "at signal → now" an apples-to-apples decay and the chip read as historical.
                    _sub = f"Score {int(sig.get('score_at_trigger', 0) or 0)} at signal → {int(sc)} now"
                    if _rec:
                        _sub += f" · {_rec}"
                _chips += (f'<div class="ra-chip"><div class="ra-h">{cat_icon(_cat,14)}'
                           f'<span class="ra-cat">{_clean_name(_cat)}</span>'
                           f'<span class="ra-age">{_age}</span></div>'
                           f'<div class="ra-sub">{_sub[:64]}</div></div>')
            st.markdown(f'<div class="ra-lbl">Recent alerts for {ticker}</div>'
                        f'<div class="ra-strip">{_chips}</div>', unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

    # ── Signal on the chart (the detected pattern drawn ON the price action) ──
    _sig_cat = data.get("primary_cat") or ""
    _sig_fac = data.get("factors") or _factors   # reuse the factors computed once above
    if not _sig_cat and _sig_fac:
        try: _sig_cat = category_for_feat(_sig_fac)[0] or ""
        except Exception: _sig_cat = ""
    if _sig_cat:
        if render_signal_chart(df, _sig_cat, _sig_fac, q, ticker):
            st.markdown("<br>", unsafe_allow_html=True)

    # Chart + Insights
    cc_col,ci_col=st.columns([3,2],gap="small")
    with cc_col:
        st.markdown('<div class="sec-hd">📈 Price Chart (90 Days)</div>',unsafe_allow_html=True)
        if df is not None and len(df)>5:
            pdf=df.copy(); pdf["MA20"]=pdf["close"].rolling(20).mean(); pdf["MA50"]=pdf["close"].rolling(min(50,len(pdf))).mean()
            if HAS_PLOTLY:
                fig=go.Figure()
                fig.add_trace(go.Scatter(x=pdf["datetime"],y=pdf["close"],name="Price",line=dict(color=BLUE,width=2),fill="tozeroy",fillcolor="rgba(99,102,241,0.05)"))
                fig.add_trace(go.Scatter(x=pdf["datetime"],y=pdf["MA20"],name="MA20",line=dict(color=GOLD,width=1,dash="dot")))
                fig.add_trace(go.Scatter(x=pdf["datetime"],y=pdf["MA50"],name="MA50",line=dict(color=RED,width=1,dash="dot")))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=0,b=0),height=280,
                    legend=dict(orientation="h",yanchor="bottom",y=1.02,bgcolor="rgba(0,0,0,0)",font=dict(color="#6b7fa0",size=11)),
                    xaxis=dict(showgrid=False,color="#4a5e7a",tickfont=dict(size=10)),
                    yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#4a5e7a",tickfont=dict(size=10)))
                st.plotly_chart(fig,use_container_width=True)
                # Volume
                avg_v=df["volume"].rolling(20).mean()
                fig2=go.Figure()
                colors_v=[GREEN if v>=a else RED for v,a in zip(df["volume"],avg_v)]
                fig2.add_trace(go.Bar(x=df["datetime"],y=df["volume"],marker_color=colors_v))
                fig2.add_trace(go.Scatter(x=df["datetime"],y=avg_v,name="20d Avg",line=dict(color=GOLD,width=1,dash="dash")))
                fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=0,b=0),height=120,showlegend=False,
                    xaxis=dict(showgrid=False,color="#4a5e7a"),yaxis=dict(showgrid=False,color="#4a5e7a"))
                st.plotly_chart(fig2,use_container_width=True)
            else:
                cdf=pdf[["datetime","close","MA20","MA50"]].rename(columns={"datetime":"Date","close":"Price"}).set_index("Date")
                st.line_chart(cdf,color=[BLUE,GOLD,RED])
        else: st.info("Chart data unavailable.")

        if bd:
            st.markdown('<div class="sec-hd" style="margin-top:12px;">Score Breakdown</div>',unsafe_allow_html=True)
            if is_premium():
                max_v={"Momentum":25,"Trend":20,"MACD":15,"Volume":15,"Sentiment":15,"Squeeze":10}
                for comp,pts in bd.items():
                    mx=max_v.get(comp,15); pct_=pts/mx if mx>0 else 0
                    c_=GREEN if pct_>=0.8 else GOLD if pct_>=0.4 else RED
                    st.markdown(f"""<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
                        <div style="width:76px;font-size:11px;color:#374f6e;">{comp}</div>
                        <div style="flex:1;background:rgba(255,255,255,.05);border-radius:3px;height:6px;">
                            <div style="background:{c_};width:{int(pct_*100)}%;height:6px;border-radius:3px;"></div>
                        </div>
                        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:{c_};width:42px;flex:0 0 auto;white-space:nowrap;text-align:right;">{pts}/{mx}</div>
                    </div>""",unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="background:#080b14;border:1px solid rgba(245,158,11,.2);border-radius:7px;padding:10px;font-size:12px;color:{GOLD};">🔒 Score breakdown is Premium only.</div>',unsafe_allow_html=True)

    with ci_col:
        st.markdown('<div class="sec-hd">💡 Plain-English Analysis</div>',unsafe_allow_html=True)
        if ig:
            for lbl,txt,s,conf in ig[:7]:
                cls="ins-bull" if s=="bull" else "ins-bear" if s=="bear" else ""
                bc="b-bull" if s=="bull" else "b-bear" if s=="bear" else "b-neu"
                bl="Bullish" if s=="bull" else "Bearish" if s=="bear" else "Neutral"
                st.markdown(f"""<div class="ins {cls}">
                    <div class="ins-label">{lbl} <span class="b {bc}">{bl}</span>
                        <span style="font-size:10px;color:#2a3a52;margin-left:auto;"> · {conf}</span></div>
                    <div class="ins-text">{txt}</div>
                </div>""",unsafe_allow_html=True)
        else: st.info("No indicators available.")

        st.markdown('<div class="sec-hd" style="margin-top:14px;">📡 Social Sentiment</div>',unsafe_allow_html=True)
        bull=sent.get("bull",50)
        st.markdown(f"""<div class="card">
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
                <span style="font-size:12px;font-weight:700;color:{GREEN};">🟢 Bullish {bull}%</span>
                <span style="font-size:12px;font-weight:700;color:{RED};">🔴 Bearish {100-bull}%</span>
            </div>
            <div style="background:rgba(255,255,255,.05);border-radius:5px;height:8px;overflow:hidden;">
                <div style="background:linear-gradient(90deg,{GREEN},{GREEN}88);width:{bull}%;height:8px;"></div>
            </div>
            <div style="font-size:11px;color:#2a3a52;margin-top:8px;">👥 {sent.get('wl',0):,} watching · {sent.get('msgs',0)} recent posts</div>
        </div>""",unsafe_allow_html=True)

    # ── Multi-factor recommendation scorecard (the rich, click-through detail) ──
    render_scorecard(ticker, df, info, sent, sc, bd, factors=_factors)

    # ── SIGNAL TRACK RECORD for this ticker ──
    st.markdown('<div class="div-line"></div>', unsafe_allow_html=True)

    # Get signal history for this ticker
    if HAS_SIGNAL_ENGINE and _demo_seed_enabled():
        seed_demo_signal_history(_demo_price)  # opt-in demo data (SEED_DEMO_SIGNALS=1)
    ticker_signals = get_ticker_signal_history(ticker, limit=10) if HAS_SIGNAL_ENGINE else []

    st.markdown(f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
        <div style="font-size:16px;font-weight:800;color:#e2e8f0;">📈 Scanviction Signal Track Record</div>
        <span style="background:rgba(168,85,247,0.15);color:#c084fc;border:1px solid rgba(168,85,247,0.35);
              font-size:10px;font-weight:700;padding:4px 12px;border-radius:20px;">Proprietary Data</span>
    </div>
    <div style="font-size:12px;color:#374f6e;margin-bottom:14px;">
        Every time Scanviction flagged <strong style="color:#818cf8;">{ticker}</strong> via a composite signal,
        we tracked what actually happened. Use this to evaluate signal quality.
    </div>
    """, unsafe_allow_html=True)

    if ticker_signals:
        for sig in ticker_signals[:3]:
            outs = sig.get("outcomes", {})
            curr_pct = outs.get("current_pct", 0) or 0
            label = outs.get("label", "pending")
            label_color = "#4ade80" if label == "success" else "#f87171" if label == "failure" else "#fbbf24"
            label_bg = "rgba(34,197,94,0.08)" if label == "success" else "rgba(239,68,68,0.08)" if label == "failure" else "rgba(251,191,36,0.08)"
            label_emoji = "✅" if label == "success" else "❌" if label == "failure" else "⏳"

            trigger_dt = datetime.fromisoformat(sig.get("triggered_at", datetime.now().isoformat()))
            days_ago = (datetime.now() - trigger_dt).days
            trigger_price = sig.get("trigger_price", 0)
            conf = sig.get("confidence", {})
            lifecycle = sig.get("lifecycle_stage", "candidate")
            lifecycle_colors = {"candidate":"#6b7fa0","confirmed":"#fbbf24","active":"#4ade80",
                                 "extended":"#818cf8","completed":"#4ade80","failed":"#f87171"}

            # Build the outcome tiles into ONE joined string. (Rendering each tile on its
            # own line inside the markdown f-string is the blank-line gotcha: a None tile
            # leaves a blank line, after which the deeply-indented HTML that follows is
            # parsed as a markdown code block and dumped as raw text.)
            def _otile(lbl, val, col):
                return (f'<div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:6px;padding:6px 14px;">'
                        f'<div style="font-size:11px;color:#374f6e;">{lbl}</div>'
                        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:13px;font-weight:700;color:{col};">{val}</div></div>')
            _ot = []
            for _lbl, _k in (("+1 Day", "1d_pct"), ("+3 Days", "3d_pct"), ("+5 Days", "5d_pct")):
                _v = outs.get(_k)
                if _v is not None:
                    _ot.append(_otile(_lbl, f"{_v:+.1f}%", "#4ade80" if _v >= 0 else "#f87171"))
            if outs.get("max_upside") is not None:
                _ot.append(_otile("Max ↑", f'+{outs["max_upside"]:.1f}%', "#4ade80"))
            if outs.get("max_drawdown") is not None:
                _ot.append(_otile("Max ↓", f'{outs["max_drawdown"]:.1f}%', "#f87171"))
            _tiles_html = "".join(_ot)

            st.markdown(f"""
            <div style="background:{label_bg};border:1px solid {label_color}33;border-radius:12px;
                        padding:16px 18px;margin-bottom:10px;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;">
                    <div>
                        <div style="font-size:13px;font-weight:800;color:#e2e8f0;margin-bottom:4px;">
                            {sig.get("category","Signal")}
                        </div>
                        <div style="font-size:11px;color:#374f6e;">
                            Flagged {days_ago}d ago · Entry: <span style="font-family:'JetBrains Mono',monospace;color:#818cf8;">${trigger_price:.2f}</span>
                            · {conf.get("confidence","N/A")} confidence · {conf.get("risk","N/A")} risk
                            <span style="background:{lifecycle_colors.get(lifecycle,"#6b7fa0")}22;
                                   color:{lifecycle_colors.get(lifecycle,"#6b7fa0")};
                                   border:1px solid {lifecycle_colors.get(lifecycle,"#6b7fa0")}44;
                                   font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;margin-left:8px;">
                                {lifecycle.upper()}
                            </span>
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:20px;font-weight:900;font-family:'JetBrains Mono',monospace;color:{label_color};">
                            {'+' if curr_pct >= 0 else ''}{curr_pct:.1f}%
                        </div>
                        <div style="font-size:11px;color:#374f6e;">{label_emoji} {label.title()}</div>
                    </div>
                </div>
                <div style="display:flex;gap:16px;margin-top:12px;flex-wrap:wrap;">{_tiles_html}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown(f"""<div style="background:#0d1525;border:1px solid {BORDER};border-radius:10px;
                        padding:16px;text-align:center;font-size:12px;color:#374f6e;">
            No previous signal events recorded for {ticker} yet. Check back after this setup matures.
        </div>""", unsafe_allow_html=True)

    # ── SIGNAL PERFORMANCE (percent-based; no dollar projections) ──
    st.markdown('<div class="div-line"></div>', unsafe_allow_html=True)
    st.markdown(f"""
    <div style="font-size:16px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Signal Performance</div>
    <div style="font-size:12px;color:#374f6e;margin-bottom:14px;">
        How {ticker} has moved since today's Scanviction signal — the percent return, long or short.
        <span style="color:{GOLD};"> Educational only — not financial advice.</span>
    </div>
    """, unsafe_allow_html=True)

    if not is_authed():
        st.markdown(f'<div class="card" style="text-align:center;padding:18px;"><div style="font-size:13px;color:#374f6e;">Sign in to track signal performance.</div></div>', unsafe_allow_html=True)
        if st.button("Sign In", key="det_pnl_login", use_container_width=True, type="primary"):
            nav("login")
    else:
        # Auto-refreshing fragment (statically decorated, fixed 120s): the box
        # re-reads the warm quote in place — Current price + return stay live while
        # the user sits on the page, without a full-page rerun (no flash).
        _signal_perf_box(ticker, ticker_signals, price)

    # Why flagged
    st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
    st.markdown('<div class="sec-hd">🎯 Why This Stock Is On Your Radar</div>',unsafe_allow_html=True)
    reasons=[]
    if sc>=70: reasons.append(("Strong multi-factor signal — momentum, trend, MACD, and volume align","bull"))
    if sent.get("bull",50)>=65: reasons.append((f"{sent['bull']}% of StockTwits traders are currently bullish","bull"))
    if sf>=20: reasons.append((f"{sf:.0f}% of shares are sold short — rising price forces short covering (squeeze)","bull"))
    if hot: reasons.append(("Currently trending on StockTwits Hot list","bull"))
    for lbl,_,sv,_ in ig[:4]: reasons.append((lbl,sv))
    rc2=st.columns(2)
    for i,(r,sv) in enumerate(reasons[:6]):
        em="🟢" if sv=="bull" else "🔴" if sv=="bear" else "⚪"
        with rc2[i%2]:
            st.markdown(f'<div style="background:#080b14;border:1px solid {BORDER};border-radius:7px;padding:9px 13px;margin-bottom:5px;font-size:12px;color:#374f6e;">{em} {r}</div>',unsafe_allow_html=True)

    # Related
    sector=info.get("sector","N/A")
    if sector!="N/A":
        st.markdown(f'<div class="sec-hd" style="margin-top:16px;">🔗 Related — {sector}</div>',unsafe_allow_html=True)
        all_t=list(set([t for tl in CATEGORIES.values() for t in tl]))
        related=[rt for rt in all_t if rt!=ticker and yf_fund(rt).get("sector")==sector][:5]
        if related:
            rcols=st.columns(len(related)) if related else []
            for col,rt in zip(rcols,related):
                rq=get_quote(rt)
                if rq:
                    rc_=GREEN if rq["pct"]>=0 else RED
                    col.markdown(f'<div class="stat" style="cursor:pointer;"><div style="font-family:\'JetBrains Mono\',monospace;font-size:12px;font-weight:700;color:#818cf8;">{rt}</div><div style="font-family:\'JetBrains Mono\',monospace;font-size:13px;font-weight:700;color:#e2e8f0;">${rq["price"]:,.2f}</div><div style="font-size:11px;font-weight:700;color:{rc_};">{"▲" if rq["pct"]>=0 else "▼"}{abs(rq["pct"]):.2f}%</div></div>',unsafe_allow_html=True)
                    if col.button("View",key=f"rel_{rt}",use_container_width=True):
                        st.session_state.detail_ticker=rt; st.session_state.detail_data={}; st.rerun()

    if info.get("desc"):
        with st.expander(f"About {q.get('name',ticker)}"):
            st.markdown(f'<div style="font-size:13px;color:#374f6e;line-height:1.7;">{info["desc"]}</div>',unsafe_allow_html=True)

    # Actions
    st.markdown("<br>",unsafe_allow_html=True)
    st.markdown(f'<div style="font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:8px;">⚡ QUICK ACTIONS</div>', unsafe_allow_html=True)
    wl=st.session_state.get("watchlist",[]); in_wl=ticker in wl
    a1,a2,a3=st.columns(3, gap="small")
    with a1:
        if st.button("✅ On Watchlist" if in_wl else "➕ Add to Watchlist",key="det_wl",type="primary",use_container_width=True):
            if in_wl: wl.remove(ticker)
            else:     wl.append(ticker)
            st.session_state.watchlist = wl
            if is_authed():
                db = st.session_state.users_db.get(st.session_state.user["email"], {})
                db["watchlist"] = wl
                save_user_to_file(st.session_state.user["email"], db)
            st.toast(f"{'Removed from' if in_wl else 'Added to'} watchlist", icon="⭐")
            st.rerun()
    with a2:
        if st.button("🔔 Manage Alerts →",key="det_alert_nav",use_container_width=True):
            nav("settings")
    with a3:
        if st.button("📊 Signal History →",key="det_track",use_container_width=True):
            nav("signal_track")

    # ── Smart Alert Presets ──
    st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
    st.markdown(f'<div style="font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:8px;">🎯 SMART ALERT PRESETS for {ticker}</div>', unsafe_allow_html=True)
    st.markdown(f'<div style="font-size:11px;color:#374f6e;margin-bottom:10px;">One-click alerts based on current price. Notifications via your default channels.</div>', unsafe_allow_html=True)
    p1, p2, p3, p4 = st.columns(4, gap="small")
    presets = [
        (p1, f"📈 +10% from here", "price_above", round(price*1.10, 2), f"Trigger when {ticker} crosses ${price*1.10:.2f}"),
        (p2, f"📉 -10% from here", "price_below", round(price*0.90, 2), f"Trigger when {ticker} falls to ${price*0.90:.2f}"),
        (p3, f"🔊 Volume 2× avg", "volume_spike", 2.0, f"Trigger on volume surge"),
        (p4, f"⚡ Big move ±5%", "pct_change", 5.0, f"Trigger on 5% daily move"),
    ]
    for col, label, ptype, thresh, desc in presets:
        with col:
            if st.button(label, key=f"preset_{ptype}_{ticker}", use_container_width=True, help=desc):
                if not is_authed():
                    st.warning("Sign in to set alerts.")
                else:
                    alerts = st.session_state.get("alerts", [])
                    new_a = {
                        "id": f"{ticker}_{ptype}_{int(time.time())}",
                        "ticker": ticker, "type": ptype, "threshold": thresh,
                        "label": f"{ticker} {label}", "channels": ["email"],
                        "active": True, "created": datetime.now().strftime("%Y-%m-%d %H:%M")
                    }
                    alerts.append(new_a)
                    st.session_state.alerts = alerts
                    save_alerts_to_file(st.session_state.user["email"], alerts)
                    st.toast(f"🔔 {label} alert set!", icon="✅")
                    st.rerun()

    st.markdown('<div class="disc" style="margin-top:14px;">⚠️ For educational purposes only. Not financial advice. Trading involves risk of loss.</div>',unsafe_allow_html=True)
    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: SIGNAL TRACK RECORD
# ─────────────────────────────────────────────────────────────
SIGNAL_BANNER_MIN = int(_os.environ.get("SIGNAL_BANNER_MIN", "75"))   # score for a signal to earn a banner

def _resolve_signals_last_seen():
    """The user's 'seen up to' watermark (epoch secs). Session first, then the PERSISTED user
    record, else a short welcome window so a first/returning user still sees recent activity
    (not a huge backlog, and not nothing). Cached back into the session."""
    last = st.session_state.get("signals_last_seen")
    if last is None:
        email = st.session_state.user.get("email", "") if is_authed() else ""
        rec = st.session_state.get("users_db", {}).get(email, {}) if email else {}
        last = rec.get("signals_last_seen")
        if last is None:
            # First login: clean slate ("you're caught up"). Only signals from now on notify —
            # no overwhelming backlog. Returning users use their persisted (feed-visit) watermark.
            last = time.time()
        st.session_state["signals_last_seen"] = float(last)
    return float(last or 0)

def _unseen_signals():
    """Tier-filtered signal events the user hasn't seen yet (newest first). Drives the bell
    badge, the login/new-arrival toast, and the high-conviction banner. Premium-gated signals
    are excluded for free users (consistent with Discover and the feed)."""
    if not HAS_SIGNAL_ENGINE or not is_authed():
        return []
    try:
        last = _resolve_signals_last_seen()
        out = []
        for e in get_recent_signal_events(limit=100):
            if _signal_is_locked(e.get("category", "")):
                continue
            try: ts = datetime.fromisoformat(e.get("triggered_at", "")).timestamp()
            except Exception: ts = 0
            if ts > last:
                out.append(e)
        return out
    except Exception:
        return []

def _signals_unseen():
    """Unseen count for the topbar 🔔 badge (capped at 99)."""
    return min(len(_unseen_signals()), 99)

def page_signals():
    """Live in-app feed of stocks that just ENTERED a composite category — the
    non-overbearing default 'notification' surface (pull, never interrupts).
    Browser/phone push is opt-in via Settings → Alerts."""
    render_topbar("signals")
    st.markdown('<div class="page-wrap pw-narrow">', unsafe_allow_html=True)
    back_button("signals_back")
    # Visiting the feed marks everything currently logged as "seen" → bell + banner clear.
    st.session_state["signals_last_seen"] = time.time()
    try:  # persist the watermark so the bell stays cleared across sessions, not just this one
        _em = st.session_state.user.get("email", "")
        if _em and _em in st.session_state.get("users_db", {}):
            st.session_state.users_db[_em]["signals_last_seen"] = st.session_state["signals_last_seen"]
            save_user_to_file(_em, st.session_state.users_db[_em])
    except Exception: pass

    st.markdown('<div style="font-size:22px;font-weight:800;color:#e2e8f0;margin-bottom:2px;">Daily Brief</div>'
                '<div style="font-size:13px;color:#374f6e;margin-bottom:16px;">What\'s new since you last looked — stocks newly entering a signal category, open-market insider buys, fresh SEC 8-K catalysts and short-interest surges — newest first, no scanning required. This is the "what changed" companion to <span style="color:#818cf8;font-weight:600;">Discover</span> (the full ranked board). '
                'Filter it by type, category, score, direction and category win rate below. '
                'Want it pushed to your phone, browser or email? Turn it on in <span style="color:#818cf8;font-weight:600;">Settings → Alerts</span>.</div>',
                unsafe_allow_html=True)

    if not HAS_SIGNAL_ENGINE:
        st.info("Signal tracking isn't available in this environment.")
        st.markdown('</div>', unsafe_allow_html=True); return

    events = get_recent_signal_events(limit=80)
    # Demo seeding is OPT-IN (SEED_DEMO_SIGNALS=1) — production shows only real events.
    if _demo_seed_enabled() and (not events or not any(e.get("category") in EVENT_ALERT_TYPES for e in events)):
        try: seed_demo_signal_history(_demo_price); events = get_recent_signal_events(limit=80)
        except Exception: pass

    # ── Filters ──
    # These absorbed the retired Refine page. Filtering belongs with the feed it filters,
    # not on a separate tab you have to remember exists — and Refine's own copy admitted it
    # only ever narrowed the CURRENT signals, which is exactly what this feed holds.
    TYPE_OPTS = ["All", "Signal entries", "Insider buys", "8-K filings", "Short interest"]
    _type = st.radio("Show", TYPE_OPTS, horizontal=True, key="sig_type_filter", label_visibility="collapsed")
    fc1, fc2 = st.columns([3, 1])
    with fc1:
        # Options come from the categories ACTUALLY present in the current feed (minus the event
        # types, which the "Show" filter above handles). Deriving from the data guarantees every
        # option matches real events — the static COMPOSITE_CATS list drifted from the stored
        # category strings, so selecting one filtered to nothing.
        _feed_cats = sorted({e.get("category", "") for e in events
                             if e.get("category") and e.get("category") not in EVENT_ALERT_TYPES})
        cats = st.multiselect("Filter categories", _feed_cats,
                              default=[], placeholder="All categories", key="sig_cat_filter")
    with fc2:
        min_sc = st.slider("Min score", 0, 100, 0, key="sig_min_sc")

    # Direction + historical win rate. Premium — this is the capability Refine used to sell,
    # and it's the pair that turns a chronological feed into a research tool: "only shorts,
    # only from categories that have actually been right more than half the time".
    _win_by_cat = {}
    if HAS_SIGNAL_ENGINE:
        try:
            _win_by_cat = {c: float(s.get("win_rate") or 0)
                           for c, s in (get_category_performance_stats() or {}).items()
                           if (s.get("count") or 0) > 0}
        except Exception:
            _win_by_cat = {}
    _dir, _min_win = "All", 0
    if is_premium():
        ac1, ac2 = st.columns([1.4, 2])
        with ac1:
            _dir = st.radio("Direction", ["All", "Long", "Short"], horizontal=True,
                            key="sig_dir_filter")
        with ac2:
            _min_win = st.slider(
                "Min category win rate %", 0, 100, 0, step=5, key="sig_min_win",
                help="Keep only signals from categories whose tracked outcomes have been "
                     "correct at least this often. Categories with no resolved outcomes yet "
                     "are hidden once this is above 0.")
    elif events:
        # Gate on "there is a feed", not on "_feed_cats is non-empty" — a feed made up
        # entirely of insider/8-K/short-interest rows has no categories but is still
        # perfectly filterable by direction and win rate.
        st.markdown(f'<div style="font-size:11.5px;color:{GOLD};background:rgba(245,158,11,.07);'
                    'border:1px solid rgba(245,158,11,.22);border-radius:9px;padding:8px 12px;'
                    'margin:2px 0 10px;">👑 Premium adds direction (long/short) and a minimum '
                    'category win-rate filter to this feed.</div>', unsafe_allow_html=True)

    def _evt_kind(c):
        return {EVT_INSIDER: "Insider buys", EVT_8K: "8-K filings",
                EVT_SHORT: "Short interest"}.get(c, "Signal entries")
    if _type != "All":
        events = [e for e in events if _evt_kind(e.get("category", "")) == _type]
    if cats:
        events = [e for e in events if e.get("category") in cats]
    events = [e for e in events if (e.get("score_at_trigger", 0) or 0) >= min_sc]
    if _dir != "All":
        _want_bear = (_dir == "Short")
        # Event rows (insider / 8-K / short-interest) aren't directional calls, so a
        # direction filter has nothing to say about them — they drop out rather than
        # being silently counted as longs.
        events = [e for e in events
                  if e.get("category") not in EVENT_ALERT_TYPES
                  and (category_dir(e.get("category", "")) == "bear") == _want_bear]
    if _min_win > 0:
        events = [e for e in events if _win_by_cat.get(e.get("category", ""), -1) >= _min_win]

    if not events:
        st.markdown('<div class="card" style="text-align:center;padding:40px 24px;">'
                    '<div style="font-size:32px;margin-bottom:10px;">🔕</div>'
                    '<div style="font-size:15px;font-weight:700;color:#e2e8f0;">No signals match your filters yet</div>'
                    '<div style="font-size:13px;color:#374f6e;margin-top:6px;">New category entries show up here automatically as the market moves.</div>'
                    '</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True); return

    st.markdown(f'<div style="font-size:12px;color:#374f6e;margin:6px 0 12px;">{len(events)} recent signal{"s" if len(events)!=1 else ""}</div>', unsafe_allow_html=True)

    for e in events[:50]:
        t = e.get("ticker", "?"); cat = e.get("category", "Signal")
        sc = int(e.get("score_at_trigger", 0) or 0)
        price = float(e.get("trigger_price", 0) or 0)
        rec = e.get("recommendation", "") or ""
        sec = (e.get("info_snapshot") or {}).get("sector") or ""
        try: age = _humanize_age(datetime.fromisoformat(e.get("triggered_at", "")).timestamp())
        except Exception: age = ""
        is_evt = cat in EVENT_ALERT_TYPES
        locked = _signal_is_locked(cat)   # gate premium signals for free users (like Discover)
        sc_c = GREEN if sc >= 65 else GOLD if sc >= 40 else RED
        cm, cb = st.columns([6, 1.2], gap="small")
        with cm:
            if locked:
                # Premium signal: reveal the TYPE (a conversion teaser) but blur the stock and
                # lock the details + View — consistent with Discover's "Unlock to reveal" gating.
                st.markdown(f'<div class="sr" style="padding:11px 14px;">'
                            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
                            f'<span class="sr-tick" style="filter:blur(6px);user-select:none;">{t}</span>'
                            f'<span class="sig-pill">{cat_icon(cat,13)}<span>{_clean_name(cat)}</span></span>'
                            f'<span style="font-size:10px;color:#4a5e7a;">{age}</span></div>'
                            f'<div style="margin-top:5px;font-size:12px;color:{GOLD};">🔒 Premium signal — upgrade to reveal the stock</div>'
                            f'</div>', unsafe_allow_html=True)
            else:
                # Event types lead with the filing detail; category entries lead with the score.
                detail_html = f'<span style="color:#a5b4fc;font-weight:600;">{rec}</span>' if rec else ""
                score_html = "" if is_evt else f'<span style="color:{sc_c};font-weight:700;">Score {sc}</span>'
                sec_html = f'<span style="color:#374f6e;">{sec}</span>' if (sec and not is_evt) else ""
                # % since the signal (same figure as Discover / category cards): live warm
                # price vs the trigger price, measured in the signal's own direction
                # (shorts show a decline as a positive, labeled short).
                since_html = ""
                try:
                    _cur = ((_warm_index().get(t) or {}).get("q") or {}).get("price", 0) or 0
                    _d, _sh = _since_signal_pct(cat, price, _cur)
                    if _d is not None:
                        _scol = GREEN if _d >= 0 else RED
                        _ssign = "+" if _d >= 0 else ""
                        _slbl = "% since (short)" if _sh else "% since"
                        since_html = (f'<span style="font-family:JetBrains Mono,monospace;font-weight:700;'
                                      f'color:{_scol};">{_ssign}{_d:.2f}{_slbl}</span>')
                except Exception:
                    pass
                st.markdown(f'<div class="sr" style="padding:11px 14px;">'
                            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
                            f'<span class="sr-tick">{t}</span>'
                            f'<span class="sig-pill">{cat_icon(cat,13)}<span>{_clean_name(cat)}</span></span>'
                            f'<span style="font-size:10px;color:#4a5e7a;">{age}</span></div>'
                            f'<div style="display:flex;gap:16px;margin-top:5px;font-size:12px;flex-wrap:wrap;align-items:center;">'
                            f'<span style="font-family:JetBrains Mono,monospace;color:#e2e8f0;font-weight:700;">${price:,.2f}</span>'
                            f'{since_html}{score_html}{detail_html}{sec_html}</div>'
                            f'</div>', unsafe_allow_html=True)
        with cb:
            st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)
            if locked:
                if st.button("🔒 Unlock", key=f"sig_lock_{e.get('id', t)}", use_container_width=True):
                    nav("pricing")
            elif st.button("View", key=f"sig_view_{e.get('id', t)}", use_container_width=True):
                st.session_state.detail_ticker = t
                st.session_state.detail_data = {}
                nav("stock_detail")
    st.markdown('</div>', unsafe_allow_html=True)

def page_signal_track():
    render_topbar("signal_track")
    st.markdown('<div class="page-wrap pw-narrow">' ,unsafe_allow_html=True)

    st.markdown("""
    <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:10px;">
        <div>
            <div style="font-size:24px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">\U0001F4C9 Full Track Record</div>
            <div style="font-size:13px;color:#374f6e;">The complete audit behind <b style="color:#a5b4fc;">Results</b>: every recommendation
            Scanviction surfaces is logged with its entry price and timestamp, then scored at 1 / 3 / 5 / 10 / 30-day horizons
            against a +/-3% threshold and vs the S&P 500.</div>
        </div>
        <span style="background:rgba(168,85,247,0.15);color:#c084fc;border:1px solid rgba(168,85,247,0.35);
              font-size:11px;font-weight:700;padding:6px 14px;border-radius:20px;margin-top:4px;">
            \u2728 Proprietary Scanviction Data
        </span>
    </div>
    """, unsafe_allow_html=True)

    # Evaluate every stored recommendation against horizon outcomes.
    with st.spinner("Scoring recommendation outcomes\u2026"):
        try:
            evals = evaluate_all_recommendations()
        except Exception:
            evals = []

    if not evals:
        st.markdown("""<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);
                           border-radius:12px;padding:36px;text-align:center;">
            <div style="font-size:30px;margin-bottom:10px;">\U0001F4CA</div>
            <div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">No tracked recommendations yet</div>
            <div style="font-size:13px;color:#374f6e;">As you browse Discover categories, each surfaced signal is recorded here with its
            entry price. Outcomes populate automatically as the 1\u201330 day horizons elapse.</div>
        </div>""", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # ── Horizon selector ──
    hz = st.radio("Evaluation horizon", HORIZONS, index=2, horizontal=True,
                  format_func=lambda d: f"{d}-day", key="tr_horizon")

    # ── Summary KPIs at the selected horizon ──
    resolved = [e for e in evals if e["horizons"].get(hz,{}).get("label","pending") != "pending"]
    wins   = [e for e in resolved if e["horizons"][hz]["label"] == "success"]
    losses = [e for e in resolved if e["horizons"][hz]["label"] == "failure"]
    win_rate = round(len(wins)/len(resolved)*100,1) if resolved else 0.0
    rets = [e["horizons"][hz]["return_pct"] for e in resolved if e["horizons"][hz]["return_pct"] is not None]
    avg_ret = round(sum(rets)/len(rets),2) if rets else 0.0
    pending = [e for e in evals if e["horizons"].get(hz,{}).get("label","pending") == "pending"]

    kc = st.columns(5)
    for col, val, lbl, color in [
        (kc[0], len(evals),                                  "Total Signals",      "#818cf8"),
        (kc[1], f"{win_rate}%",                              f"{hz}-Day Win Rate", "#4ade80" if win_rate>=50 else "#fbbf24"),
        (kc[2], f"{len(wins)} / {len(resolved)}",            "Wins / Resolved",    "#4ade80"),
        (kc[3], (f"+{avg_ret}%" if avg_ret>=0 else f"{avg_ret}%"), f"Avg {hz}-Day Return", "#4ade80" if avg_ret>=0 else "#f87171"),
        (kc[4], len(pending),                                "Pending Outcomes",   "#fbbf24"),
    ]:
        col.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px;">
            <div style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:900;color:{color};">{val}</div>
            <div style="font-size:11px;color:#374f6e;margin-top:3px;">{lbl}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)

    tr_tabs = st.tabs(["\U0001F4CB Recommendations", "\U0001F4CA Category Hit-Rate", "\U0001F916 ML Model", "\U0001F4E5 Research Export"])

    # ── TAB 1: labeled recommendation table ──
    with tr_tabs[0]:
        rows = sorted(evals, key=lambda e: (e["horizons"].get(hz,{}).get("return_pct") or -999), reverse=True)
        for e in rows:
            hzd = e["horizons"].get(hz,{})
            label = hzd.get("label","pending")
            lcolor = {"success":"#4ade80","failure":"#f87171","neutral":"#94a3b8","pending":"#fbbf24"}.get(label,"#94a3b8")
            licon  = {"success":"\u2705","failure":"\u274C","neutral":"\u2796","pending":"\u23F3"}.get(label,"")
            ret = hzd.get("return_pct")
            ret_s = (f"+{ret}%" if (ret is not None and ret>=0) else (f"{ret}%" if ret is not None else "\u2014"))
            rel = hzd.get("rel_label","")
            rel_s = {"outperform":"\u25B2 vs SPY","underperform":"\u25BC vs SPY","inline":"\u2248 SPY","pending":""}.get(rel,"")
            mu = e.get("max_upside_pct"); md = e.get("max_drawdown_pct")
            st.markdown(
                '<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.07);border-radius:10px;'
                'padding:12px 16px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'
                f'<div style="min-width:200px;"><span style="font-family:\'JetBrains Mono\',monospace;font-size:15px;font-weight:800;color:#818cf8;">{e["ticker"]}</span>'
                f'<span style="font-size:11px;color:#4a5e7a;margin-left:8px;">{e["category"]}</span>'
                f'<div style="font-size:10px;color:#4a5e7a;margin-top:3px;">Signal {e["age"]} @ ${e["entry_price"]:,.2f} \u00B7 now ${e["current_price"]:,.2f} \u00B7 max +{mu if mu is not None else 0}% / {md if md is not None else 0}%</div></div>'
                f'<div style="text-align:right;"><span style="font-size:11px;font-weight:800;color:{lcolor};">{licon} {label.upper()}</span>'
                f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:14px;font-weight:700;color:{lcolor};">{ret_s}</div>'
                f'<div style="font-size:10px;color:#4a5e7a;">{rel_s}</div></div>'
                '</div>', unsafe_allow_html=True)

    # ── TAB 2: category hit-rate ──
    with tr_tabs[1]:
        hr = category_hit_rates(evals, horizon=hz)
        if not hr:
            st.info(f"No resolved outcomes at the {hz}-day horizon yet. Check back as signals mature.")
        else:
            for cat, a in sorted(hr.items(), key=lambda x: x[1]["hit_rate"], reverse=True):
                hrate = a["hit_rate"]
                bar_c = "#4ade80" if hrate>=50 else "#fbbf24" if hrate>=33 else "#f87171"
                st.markdown(
                    '<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:12px 16px;margin-bottom:6px;">'
                    f'<div style="display:flex;justify-content:space-between;margin-bottom:6px;"><span style="font-size:13px;font-weight:700;color:#e2e8f0;">{cat}</span>'
                    f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:13px;font-weight:800;color:{bar_c};">{hrate}% hit rate</span></div>'
                    f'<div style="background:rgba(255,255,255,0.06);border-radius:4px;height:6px;"><div style="background:{bar_c};width:{hrate}%;height:6px;border-radius:4px;"></div></div>'
                    f'<div style="font-size:10px;color:#4a5e7a;margin-top:5px;">{a["wins"]} wins \u00B7 {a["losses"]} losses \u00B7 {a["neutral"]} neutral \u00B7 {a["n"]} resolved</div>'
                    '</div>', unsafe_allow_html=True)

    # ── TAB 3: ML model — success prediction + evaluation ──
    with tr_tabs[2]:
        st.markdown('<div style="font-size:13px;color:#6b7fa0;margin-bottom:12px;">A gradient-boosted classifier predicts the probability a freshly-surfaced '
                    'signal becomes a success at the chosen horizon, trained on signal-time features only (no leakage) with a '
                    'time-aware split. Metrics below are computed on held-out <em>newer</em> signals.</div>', unsafe_allow_html=True)
        ml_hz = st.radio("Model horizon", HORIZONS, index=2, horizontal=True,
                         format_func=lambda d: f"{d}-day", key="tr_ml_horizon")
        with st.spinner("Training & evaluating model\u2026"):
            try:
                ml = train_and_evaluate_model(horizon=ml_hz)
            except Exception as _e:
                ml = {"status": "error", "message": str(_e)[:120]}
        status = ml.get("status")
        if status == "ok":
            m = ml["metrics"]
            mk = st.columns(5)
            for col, val, lbl in [
                (mk[0], f"{m['accuracy']*100:.0f}%", "Accuracy"),
                (mk[1], f"{m['precision']*100:.0f}%", "Precision"),
                (mk[2], f"{m['recall']*100:.0f}%", "Recall"),
                (mk[3], f"{m['f1']*100:.0f}%", "F1"),
                (mk[4], (f"{m['roc_auc']:.2f}" if m.get('roc_auc') is not None else "\u2014"), "ROC-AUC"),
            ]:
                col.markdown(f'<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px;">'
                             f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:20px;font-weight:900;color:#818cf8;">{val}</div>'
                             f'<div style="font-size:11px;color:#374f6e;margin-top:3px;">{lbl}</div></div>', unsafe_allow_html=True)
            st.caption(f"Model {ml['model_version']} \u00b7 trained on {ml['n_train']} older signals \u00b7 evaluated on {ml['n_test']} newer \u00b7 horizon {ml['horizon']}d")
            mlc1, mlc2 = st.columns(2, gap="small")
            with mlc1:
                st.markdown('<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin:14px 0 10px;">\U0001F50D Feature Importance</div>', unsafe_allow_html=True)
                if HAS_PLOTLY:
                    fi = ml["feature_importance"][:8]
                    names=[f for f,_ in fi][::-1]; vals=[v for _,v in fi][::-1]
                    fig=go.Figure(go.Bar(x=vals,y=names,orientation="h",marker_color="#818cf8",
                        text=[f"{v:.2f}" for v in vals],textposition="outside",
                        textfont=dict(size=11,family="JetBrains Mono",color="#94a3b8")))
                    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                        margin=dict(l=0,r=40,t=10,b=0),height=max(220,32*len(names)),
                        xaxis=dict(showgrid=False,color="#4a5e7a"),yaxis=dict(showgrid=False,color="#94a3b8",tickfont=dict(size=11)))
                    st.plotly_chart(fig,use_container_width=True)
            with mlc2:
                st.markdown('<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin:14px 0 10px;">\U0001F9EE Confusion Matrix</div>', unsafe_allow_html=True)
                cm = ml["confusion_matrix"]  # [[TN,FP],[FN,TP]]
                st.markdown(
                    '<div style="display:grid;grid-template-columns:auto auto auto;gap:6px;font-size:12px;max-width:340px;">'
                    '<div></div><div style="text-align:center;color:#4a5e7a;">Pred Fail</div><div style="text-align:center;color:#4a5e7a;">Pred Success</div>'
                    f'<div style="color:#4a5e7a;">Actual Fail</div><div style="background:rgba(34,197,94,0.12);border-radius:6px;padding:10px;text-align:center;font-family:\'JetBrains Mono\',monospace;font-weight:800;color:#4ade80;">{cm[0][0]}</div><div style="background:rgba(239,68,68,0.12);border-radius:6px;padding:10px;text-align:center;font-family:\'JetBrains Mono\',monospace;font-weight:800;color:#f87171;">{cm[0][1]}</div>'
                    f'<div style="color:#4a5e7a;">Actual Success</div><div style="background:rgba(239,68,68,0.12);border-radius:6px;padding:10px;text-align:center;font-family:\'JetBrains Mono\',monospace;font-weight:800;color:#f87171;">{cm[1][0]}</div><div style="background:rgba(34,197,94,0.12);border-radius:6px;padding:10px;text-align:center;font-family:\'JetBrains Mono\',monospace;font-weight:800;color:#4ade80;">{cm[1][1]}</div>'
                    '</div>', unsafe_allow_html=True)
        else:
            msg = ml.get("message", "Model unavailable.")
            st.markdown(f'<div style="background:#0d1525;border:1px solid rgba(251,191,36,0.3);border-radius:12px;padding:24px;">'
                        f'<div style="font-size:15px;font-weight:700;color:#fbbf24;margin-bottom:6px;">\u26A0\uFE0F Model not ready</div>'
                        f'<div style="font-size:13px;color:#6b7fa0;line-height:1.7;">{msg}</div>'
                        f'<div style="font-size:12px;color:#374f6e;margin-top:10px;">The pipeline is fully built and tested; it will train automatically '
                        f'once enough recommendations have resolved outcomes from live market data. No accuracy figures are shown until the model is genuinely trained.</div>'
                        f'</div>', unsafe_allow_html=True)

    # ── TAB 4: research export (professor deliverable) ──
    with tr_tabs[3]:
        st.markdown('<div style="font-size:13px;color:#6b7fa0;margin-bottom:12px;">Export the full labeled recommendation dataset \u2014 entry price, '
                    'every horizon return + outcome label, benchmark-relative result, max upside/drawdown, and duration \u2014 as CSV for independent analysis.</div>', unsafe_allow_html=True)
        export_rows = []
        for e in evals:
            row = {"ticker": e["ticker"], "category": e["category"],
                   "entry_price": e["entry_price"], "current_price": e["current_price"],
                   "duration_days": e["duration_days"],
                   "max_upside_pct": e["max_upside_pct"], "max_drawdown_pct": e["max_drawdown_pct"],
                   "realized_label": e["realized_label"], "profitable": e["profitable"]}
            for h in HORIZONS:
                hh = e["horizons"].get(h, {})
                row[f"ret_{h}d_pct"] = hh.get("return_pct")
                row[f"label_{h}d"] = hh.get("label")
                row[f"rel_{h}d"] = hh.get("rel_label")
            export_rows.append(row)
        import csv as _csv, io as _io
        buf = _io.StringIO()
        if export_rows:
            w = _csv.DictWriter(buf, fieldnames=list(export_rows[0].keys()))
            w.writeheader(); w.writerows(export_rows)
        st.download_button("\U0001F4E5 Download labeled dataset (CSV)", data=buf.getvalue(),
                           file_name="marketsignalpro_track_record.csv", mime="text/csv",
                           use_container_width=True, key="tr_export_csv")
        st.caption(f"{len(export_rows)} recommendations \u00B7 horizons: {', '.join(str(h)+'d' for h in HORIZONS)} \u00B7 success threshold: +/-{SUCCESS_PCT}%")

    st.markdown('</div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: BI ANALYTICS
# ─────────────────────────────────────────────────────────────
def page_bi():
    render_topbar("bi_dashboard")
    st.markdown('<div class="page-wrap pw-narrow">' ,unsafe_allow_html=True)
    back_button("bi_back")

    # ── Page intro ──
    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;">
        <div>
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;margin-bottom:4px;">📊 BI Analytics Dashboard</div>
            <div style="font-size:13px;color:#374f6e;">Market-wide intelligence across gainers, sectors, sentiment, volume surges, and composite signals.</div>
        </div>
        <span class="tag tag-live" style="font-size:11px;padding:5px 12px;margin-top:4px;">● Live</span>
    </div>
    """, unsafe_allow_html=True)

    with st.spinner("Loading analytics…"):
        movers=get_bi_movers(); secs=get_sectors(); idx=get_indexes(); hot=st_hot()

    gainers=sorted(movers,key=lambda x:x["pct"],reverse=True)
    losers=sorted(movers,key=lambda x:x["pct"])
    vol_ldrs=sorted(movers,key=lambda x:x["vr"],reverse=True)
    top_g=gainers[0] if gainers else {}; top_l=losers[0] if losers else {}; top_v=vol_ldrs[0] if vol_ldrs else {}
    bull_sec=max(secs,key=secs.get) if secs else "N/A"; avg_pct=sum(m["pct"] for m in movers)/len(movers) if movers else 0

    # ── Stat bar — bigger and clearer ──
    sw=st.columns(5)
    stat_data=[
        (top_g.get("t","—"), f"Top Gainer · +{top_g.get('pct',0):.1f}%", GREEN),
        (top_l.get("t","—"), f"Top Loser · {top_l.get('pct',0):.1f}%", RED),
        (top_v.get("t","—"), f"Volume King · {top_v.get('vr',0):.1f}× avg", "#818cf8"),
        (bull_sec, f"Strongest Sector · +{secs.get(bull_sec,0):.1f}%", GREEN),
        ("Bullish" if avg_pct>0.3 else "Bearish" if avg_pct<-0.3 else "Neutral", f"Market Mood · {avg_pct:+.2f}% avg", GREEN if avg_pct>0 else RED if avg_pct<0 else "#6b7fa0"),
    ]
    for col,(v,l,c) in zip(sw,stat_data):
        col.markdown(f'<div class="stat"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{c};margin-bottom:3px;">{v}</div><div style="font-size:11px;color:#374f6e;">{l}</div></div>',unsafe_allow_html=True)

    st.markdown("<br>",unsafe_allow_html=True)
    tabs=st.tabs([
        "📈 Leaderboards","🗺️ Sector Heatmap","📡 Sentiment","🔊 Volume","🎯 Opportunity Matrix",
        "💥 Squeeze Radar","🌪️ Risk vs Reward","📊 Score Distribution","🔄 Sector Rotation","⭐ Watchlist Analytics",
        "🎯 Signal Performance"
    ])

    CHART_LAYOUT = dict(
        paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0,r=70,t=10,b=0),height=320,
        font=dict(family="Inter",size=13,color="#94a3b8"),
        xaxis=dict(showgrid=False,color="#4a5e7a",tickfont=dict(size=12)),
        yaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=13,color="#818cf8")),
    )

    with tabs[0]:
        lc1,lc2,lc3=st.columns(3,gap="small")
        with lc1:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:{GREEN};margin-bottom:10px;">🏆 Top Gainers</div>',unsafe_allow_html=True)
            if HAS_PLOTLY:
                top10g=gainers[:10]
                fig=go.Figure(go.Bar(x=[m["pct"] for m in top10g],y=[m["t"] for m in top10g],orientation="h",marker_color=GREEN,text=[f"+{m['pct']:.1f}%" for m in top10g],textposition="outside",textfont=dict(color=GREEN,size=13,family="JetBrains Mono")))
                fig.update_layout(**CHART_LAYOUT); st.plotly_chart(fig,use_container_width=True)
        with lc2:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:{RED};margin-bottom:10px;">📉 Top Losers</div>',unsafe_allow_html=True)
            if HAS_PLOTLY:
                top10l=losers[:10]
                fig=go.Figure(go.Bar(x=[m["pct"] for m in top10l],y=[m["t"] for m in top10l],orientation="h",marker_color=RED,text=[f"{m['pct']:.1f}%" for m in top10l],textposition="outside",textfont=dict(color=RED,size=13,family="JetBrains Mono")))
                fig.update_layout(**CHART_LAYOUT); st.plotly_chart(fig,use_container_width=True)
        with lc3:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:#818cf8;margin-bottom:10px;">🔊 Volume Leaders</div>',unsafe_allow_html=True)
            if HAS_PLOTLY:
                top10v=vol_ldrs[:10]
                colors_v=[RED if m["vr"]>=3 else GOLD if m["vr"]>=2 else "#818cf8" for m in top10v]
                fig=go.Figure(go.Bar(x=[m["vr"] for m in top10v],y=[m["t"] for m in top10v],orientation="h",marker_color=colors_v,text=[f"{m['vr']:.1f}×" for m in top10v],textposition="outside",textfont=dict(size=13,family="JetBrains Mono")))
                fig.update_layout(**CHART_LAYOUT); st.plotly_chart(fig,use_container_width=True)

    # Export leaderboard data
    if movers:
        bi_export_rows=[{"Ticker":m["t"],"Price":f"${m['price']:,.2f}","Change %":f"{m['pct']:+.2f}%","Volume":f"{m['vol']:,}","Vol Ratio":f"{m['vr']:.1f}×"} for m in sorted(movers,key=lambda x:x["pct"],reverse=True)]
        export_button(bi_export_rows,"stockwins_bi_leaderboard.xlsx","📥 Export Leaderboard","bi_export")

    with tabs[1]:
        sec_sorted=sorted(secs.items(),key=lambda x:x[1],reverse=True)
        if HAS_PLOTLY:
            df_s=pd.DataFrame(sec_sorted,columns=["Sector","Change %"])
            colors=[f"rgba(34,197,94,{min(0.9,abs(c)/3+0.3)})" if c>0 else f"rgba(239,68,68,{min(0.9,abs(c)/3+0.3)})" for c in df_s["Change %"]]
            fig=go.Figure(go.Bar(x=df_s["Sector"],y=df_s["Change %"],marker_color=colors,text=[f"{'▲' if c>=0 else '▼'}{abs(c):.2f}%" for c in df_s["Change %"]],textposition="outside",textfont=dict(color="#94a3b8",size=13,family="JetBrains Mono")))
            fig.add_hline(y=0,line=dict(color="rgba(255,255,255,0.15)",width=1))
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=10,b=0),height=340,font=dict(size=13),xaxis=dict(showgrid=False,color="#4a5e7a",tickfont=dict(size=12)),yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#4a5e7a"))
            st.plotly_chart(fig,use_container_width=True)

    with tabs[2]:
        sc1,sc2=st.columns(2)
        with sc1:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">🔥 Trending on StockTwits</div>',unsafe_allow_html=True)
            if HAS_PLOTLY:
                sent_data=[{"ticker":t,"bull":st_sent(t)["bull"]} for t in hot[:8]]
                df_sent=pd.DataFrame(sent_data).sort_values("bull",ascending=False)
                colors_s=[GREEN if b>=60 else RED if b<40 else "#6b7fa0" for b in df_sent["bull"]]
                fig=go.Figure(go.Bar(x=df_sent["ticker"],y=df_sent["bull"],marker_color=colors_s,text=[f"{b}%" for b in df_sent["bull"]],textposition="outside",textfont=dict(size=13,family="JetBrains Mono")))
                fig.add_hline(y=50,line=dict(color="rgba(255,255,255,0.15)",width=1,dash="dot"))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=10,b=0),height=280,yaxis=dict(range=[0,115],showgrid=False,color="#4a5e7a"),xaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=13)))
                st.plotly_chart(fig,use_container_width=True)
        with sc2:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">👥 Most Watchlisted</div>',unsafe_allow_html=True)
            if HAS_PLOTLY:
                targets=["NVDA","TSLA","AMD","AAPL","MSTR","PLTR","GME","META"]
                wl_data=sorted([(t,st_sent(t)) for t in targets],key=lambda x:x[1].get("wl",0),reverse=True)
                wl_df=pd.DataFrame([{"t":t,"wl":s["wl"]} for t,s in wl_data])
                fig=go.Figure(go.Bar(x=wl_df["t"],y=wl_df["wl"],marker_color="rgba(129,140,248,0.7)",text=[f"{w:,}" for w in wl_df["wl"]],textposition="outside",textfont=dict(size=13,family="JetBrains Mono")))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=10,b=0),height=280,yaxis=dict(showgrid=False,color="#4a5e7a"),xaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=13)))
                st.plotly_chart(fig,use_container_width=True)

    with tabs[3]:
        surge=[m for m in movers if m["vr"]>=1.5]; surge.sort(key=lambda x:x["vr"],reverse=True)
        if surge and HAS_PLOTLY:
            sg_df=pd.DataFrame(surge[:15])
            fig=go.Figure(go.Scatter(x=sg_df["t"],y=sg_df["pct"],mode="markers",
                marker=dict(size=[min(max(vr*8,10),36) for vr in sg_df["vr"]],color=sg_df["vr"],colorscale=[[0,GREEN],[0.5,GOLD],[1,RED]],showscale=True,colorbar=dict(title="Vol×",tickfont=dict(color="#6b7fa0",size=11))),
                text=[f"{t}: {vr:.1f}×" for t,vr in zip(sg_df["t"],sg_df["vr"])],hoverinfo="text+y"))
            fig.add_hline(y=0,line=dict(color="rgba(255,255,255,0.1)",width=1))
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=60,t=10,b=0),height=360,xaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=13)),yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#4a5e7a",title="% Change"))
            st.plotly_chart(fig,use_container_width=True)
            st.caption("Bubble size = volume ratio. Green=1.5× | Amber=2× | Red=3×+")
        else: st.info("No significant volume surges right now.")

    with tabs[4]:
        st.markdown(f"""<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <div style="font-size:16px;font-weight:700;color:#e2e8f0;">🎯 Composite Opportunity Matrix</div>
            <span style="background:rgba(168,85,247,0.15);color:#c084fc;border:1px solid rgba(168,85,247,0.35);font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;">Scanviction Exclusive</span>
        </div>
        <div style="font-size:12px;color:#374f6e;margin-bottom:14px;">Signal strength across 10 tickers × 5 signal types. Darker green = stronger signal.</div>""",unsafe_allow_html=True)
        matrix_tickers=["NVDA","TSLA","AMD","AAPL","MSTR","GME","PLTR","META","MSFT","ARM"]
        signal_types=["Momentum","Trend","Volume","Sentiment","Squeeze"]
        max_vals={"Momentum":25,"Trend":20,"MACD":15,"Volume":15,"Sentiment":15,"Squeeze":10}
        matrix_data={}; prog=st.progress(0,"Computing matrix…")
        for i,t in enumerate(matrix_tickers):
            prog.progress((i+1)/len(matrix_tickers),f"Analyzing {t}…")
            df2=yf_ohlcv(t,60); info2=yf_fund(t); sent2=st_sent(t)
            _,bd2,_,_,_=compute_scores(df2,info2,sent2); matrix_data[t]=bd2
        prog.empty()
        if HAS_PLOTLY:
            z=[[matrix_data.get(t,{}).get(sig,0)/max_vals.get(sig,15) for sig in signal_types] for t in matrix_tickers]
            raw=[[matrix_data.get(t,{}).get(sig,0) for sig in signal_types] for t in matrix_tickers]
            max_v=[max_vals.get(s,15) for s in signal_types]
            fig=go.Figure(go.Heatmap(z=z,x=signal_types,y=matrix_tickers,
                text=[[f"{raw[ri][ci]}/{max_v[ci]}" for ci in range(len(signal_types))] for ri in range(len(matrix_tickers))],
                texttemplate="<b>%{text}</b>",textfont=dict(size=15,color="white",family="JetBrains Mono"),
                colorscale=[[0,"#080f1e"],[0.3,"#0a2818"],[0.6,"#0d5016"],[1,GREEN]],
                showscale=True,xgap=3,ygap=3,
                colorbar=dict(thickness=14,tickfont=dict(color="#6b7fa0",size=11),title=dict(text="Score",font=dict(color="#6b7fa0",size=11)))))
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=80,t=60,b=0),height=420,
                xaxis=dict(side="top",showgrid=False,color="#94a3b8",tickfont=dict(size=15,color="#94a3b8")),
                yaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=15,color="#818cf8")))
            st.plotly_chart(fig,use_container_width=True)
            descs={"Momentum":"RSI & momentum (max 25)","Trend":"MA alignment (max 20)","Volume":"vs 20d avg (max 15)","Sentiment":"Bullish % (max 15)","Squeeze":"Short float (max 10)"}
            mc_=st.columns(len(signal_types)) if signal_types else []
            for col,sig in zip(mc_,signal_types):
                col.markdown(f'<div style="text-align:center;font-size:11px;color:#374f6e;padding:6px 4px;background:#080b14;border-radius:6px;"><div style="font-weight:700;color:#94a3b8;margin-bottom:2px;">{sig}</div>{descs[sig]}</div>',unsafe_allow_html=True)

    # ── Module 6: Squeeze Radar ──
    with tabs[5]:
        st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">💥 Short Squeeze Radar</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Tickers ranked by short interest, days-to-cover, and momentum. Bigger bubbles = higher squeeze potential.</div>',unsafe_allow_html=True)
        if not is_premium():
            st.markdown(f'<div class="card card-gold"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Analytics</div><div style="font-size:12px;color:#374f6e;">Squeeze Radar combines short interest data, momentum, and sentiment.</div></div>',unsafe_allow_html=True)
            if gold_btn("Upgrade for Squeeze Radar","bi_sq_up"): nav("pricing")
        else:
            sq_universe=["GME","AMC","SPCE","BBIG","ATER","MSTR","BBAI","SOUN","HOOD","TSLA","AMD"]
            sq_data=[]; sq_prog=st.progress(0,"Scanning…")
            for i,t in enumerate(sq_universe):
                sq_prog.progress((i+1)/len(sq_universe))
                info=yf_fund(t); q=get_quote(t); sent=st_sent(t)
                if not info or not q: continue
                sf=(info.get("sf",0) or 0)*100
                dtc=info.get("dtc",0) or 0
                if sf<5: continue
                sq_score=min(100,int(sf*2+dtc*3+max(0,q["pct"])*2+sent["bull"]*0.3))
                sq_data.append({"t":t,"sf":sf,"dtc":dtc,"pct":q["pct"],"bull":sent["bull"],"score":sq_score,"price":q["price"]})
            sq_prog.empty()
            if sq_data and HAS_PLOTLY:
                sq_data.sort(key=lambda x:x["score"],reverse=True)
                df_sq=pd.DataFrame(sq_data)
                fig=go.Figure(go.Scatter(
                    x=df_sq["sf"],y=df_sq["dtc"],mode="markers+text",
                    marker=dict(size=[max(s/3,12) for s in df_sq["score"]],color=df_sq["score"],
                                colorscale=[[0,"#374f6e"],[0.5,GOLD],[1,RED]],showscale=True,
                                colorbar=dict(title="Squeeze",tickfont=dict(color="#6b7fa0",size=11))),
                    text=df_sq["t"],textposition="top center",textfont=dict(color="#e2e8f0",size=11,family="JetBrains Mono")))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=60,t=20,b=20),height=420,
                    xaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#94a3b8",title="Short Float %"),
                    yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#94a3b8",title="Days to Cover"))
                st.plotly_chart(fig,use_container_width=True)
                st.markdown('<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin:10px 0 6px;">🏆 Top Squeeze Candidates</div>',unsafe_allow_html=True)
                for r in sq_data[:5]:
                    sc=RED if r["score"]>=70 else GOLD if r["score"]>=50 else "#6b7fa0"
                    st.markdown(f'<div class="card" style="padding:10px 14px;margin-bottom:5px;display:flex;justify-content:space-between;align-items:center;"><div><span style="font-family:\'JetBrains Mono\',monospace;font-size:14px;font-weight:800;color:#818cf8;">{r["t"]}</span><span style="font-size:11px;color:#374f6e;margin-left:10px;">SF: {r["sf"]:.1f}% · DTC: {r["dtc"]:.1f}d · Bull: {r["bull"]}%</span></div><div style="display:flex;align-items:center;gap:14px;"><span style="font-family:\'JetBrains Mono\',monospace;font-size:13px;color:#e2e8f0;">${r["price"]:,.2f}</span><span style="background:{sc}22;color:{sc};font-size:11px;font-weight:800;padding:4px 10px;border-radius:6px;border:1px solid {sc}44;">{r["score"]}/100</span></div></div>',unsafe_allow_html=True)
            else: st.info("No squeeze candidates above threshold right now.")

    # ── Module 7: Risk vs Reward Quadrant ──
    with tabs[6]:
        st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">🌪️ Risk vs Reward Quadrant</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Stocks plotted by Scanviction score (reward) vs volatility (risk). Top-right = best opportunities.</div>',unsafe_allow_html=True)
        if not is_premium():
            st.markdown(f'<div class="card card-gold"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Analytics</div></div>',unsafe_allow_html=True)
            if gold_btn("Upgrade for Risk Analysis","bi_rr_up"): nav("pricing")
        else:
            rr_tickers=["NVDA","TSLA","AMD","AAPL","MSTR","GME","PLTR","META","MSFT","ARM","SMCI","NIO","RIVN","HOOD"]
            rr_data=[]; rr_prog=st.progress(0,"Computing risk/reward…")
            for i,t in enumerate(rr_tickers):
                rr_prog.progress((i+1)/len(rr_tickers))
                df=yf_ohlcv(t,30); info=yf_fund(t); sent=st_sent(t); q=get_quote(t)
                if df is None or df.empty or not q: continue
                sc,bd,op,risk,conf=compute_scores(df,info,sent)
                returns=df["close"].pct_change().dropna()
                volatility=float(returns.std()*100) if len(returns)>0 else 0
                rr_data.append({"t":t,"score":sc,"vol":volatility,"price":q["price"]})
            rr_prog.empty()
            if rr_data and HAS_PLOTLY:
                df_rr=pd.DataFrame(rr_data)
                colors_rr=[GREEN if r["score"]>=65 else GOLD if r["score"]>=45 else RED for _,r in df_rr.iterrows()]
                fig=go.Figure(go.Scatter(
                    x=df_rr["vol"],y=df_rr["score"],mode="markers+text",
                    marker=dict(size=22,color=colors_rr,line=dict(width=2,color="#0d1525")),
                    text=df_rr["t"],textposition="middle center",textfont=dict(color="#0d1525",size=11,family="JetBrains Mono")))
                med_vol=df_rr["vol"].median()
                fig.add_vline(x=med_vol,line=dict(color="rgba(255,255,255,0.1)",width=1,dash="dash"))
                fig.add_hline(y=50,line=dict(color="rgba(255,255,255,0.1)",width=1,dash="dash"))
                fig.add_annotation(x=df_rr["vol"].max()*0.9,y=90,text="🏆 HIGH/HIGH",showarrow=False,font=dict(color=RED,size=10))
                fig.add_annotation(x=df_rr["vol"].min()*1.1,y=90,text="⭐ HIGH/LOW",showarrow=False,font=dict(color=GREEN,size=10))
                fig.add_annotation(x=df_rr["vol"].max()*0.9,y=15,text="⚠️ LOW/HIGH",showarrow=False,font=dict(color="#6b7fa0",size=10))
                fig.add_annotation(x=df_rr["vol"].min()*1.1,y=15,text="😴 LOW/LOW",showarrow=False,font=dict(color="#6b7fa0",size=10))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=20,t=10,b=20),height=440,
                    xaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#94a3b8",title="Volatility (Risk) %"),
                    yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#94a3b8",title="Scanviction Score (Reward)",range=[0,105]))
                st.plotly_chart(fig,use_container_width=True)

    # ── Module 8: Score Distribution ──
    with tabs[7]:
        st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">📊 Score Distribution</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">How tickers in the Scanviction universe distribute by composite score.</div>',unsafe_allow_html=True)
        if not is_premium():
            st.markdown(f'<div class="card card-gold"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Analytics</div></div>',unsafe_allow_html=True)
            if gold_btn("Upgrade for Distribution Analysis","bi_sd_up"): nav("pricing")
        else:
            sd_tickers=["NVDA","TSLA","AMD","AAPL","MSTR","GME","PLTR","META","MSFT","ARM","SMCI","NIO","RIVN","HOOD","CRM","ORCL","BBAI","ASTS","IONQ","SOUN"]
            sd_scores=[]; sd_prog=st.progress(0,"Computing scores…")
            for i,t in enumerate(sd_tickers):
                sd_prog.progress((i+1)/len(sd_tickers))
                df=yf_ohlcv(t,60); info=yf_fund(t); sent=st_sent(t)
                if df is None or df.empty: continue
                sc,_,_,_,_=compute_scores(df,info,sent)
                sd_scores.append({"t":t,"score":sc})
            sd_prog.empty()
            if sd_scores and HAS_PLOTLY:
                df_sd=pd.DataFrame(sd_scores)
                bins=[0,20,40,60,80,100]
                df_sd["bucket"]=pd.cut(df_sd["score"],bins=bins,labels=["0-20","20-40","40-60","60-80","80-100"])
                bucket_counts=df_sd["bucket"].value_counts().sort_index()
                colors_sd=[RED,"#fb923c",GOLD,"#84cc16",GREEN]
                fig=go.Figure(go.Bar(x=list(bucket_counts.index),y=list(bucket_counts.values),
                    marker_color=colors_sd[:len(bucket_counts)],
                    text=[f"{v} stocks" for v in bucket_counts.values],textposition="outside",
                    textfont=dict(color="#94a3b8",size=12,family="JetBrains Mono")))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=20,b=0),height=320,
                    xaxis=dict(showgrid=False,color="#94a3b8"),
                    yaxis=dict(showgrid=True,gridcolor="rgba(255,255,255,0.04)",color="#4a5e7a",title="Number of Stocks"))
                st.plotly_chart(fig,use_container_width=True)
                st.markdown('<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin:10px 0 6px;">🏆 Top Scoring Stocks</div>',unsafe_allow_html=True)
                top_scored=sorted(sd_scores,key=lambda x:x["score"],reverse=True)[:5]
                for r in top_scored:
                    sc=GREEN if r["score"]>=65 else GOLD if r["score"]>=45 else RED
                    st.markdown(f'<div class="card" style="padding:10px 14px;margin-bottom:5px;display:flex;justify-content:space-between;align-items:center;"><span style="font-family:\'JetBrains Mono\',monospace;font-size:14px;font-weight:800;color:#818cf8;">{r["t"]}</span><span style="background:{sc}22;color:{sc};font-size:13px;font-weight:800;padding:4px 14px;border-radius:6px;border:1px solid {sc}44;font-family:\'JetBrains Mono\',monospace;">{r["score"]}/100</span></div>',unsafe_allow_html=True)

    # ── Module 9: Sector Rotation ──
    with tabs[8]:
        st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">🔄 Sector Rotation Analysis</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Which sectors are gaining/losing momentum. Identifies where money is flowing.</div>',unsafe_allow_html=True)
        if not is_premium():
            st.markdown(f'<div class="card card-gold"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Analytics</div></div>',unsafe_allow_html=True)
            if gold_btn("Upgrade for Sector Rotation","bi_rot_up"): nav("pricing")
        else:
            sec_sorted2=sorted(secs.items(),key=lambda x:x[1],reverse=True)
            lc_r,rc_r=st.columns(2)
            with lc_r:
                st.markdown(f'<div style="font-size:13px;font-weight:700;color:{GREEN};margin-bottom:8px;">🚀 SECTOR LEADERS</div>',unsafe_allow_html=True)
                top_secs=sec_sorted2[:5]
                for sec,chg in top_secs:
                    intensity=min(abs(chg)/3,1.0)
                    bg=f"rgba(34,197,94,{0.1+intensity*0.4})"
                    st.markdown(f'<div style="background:{bg};border:1px solid rgba(34,197,94,0.3);border-radius:8px;padding:12px 16px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;"><div><div style="font-size:13px;font-weight:700;color:#e2e8f0;">{sec}</div><div style="font-size:11px;color:#374f6e;">Money flowing in</div></div><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{GREEN};">▲{chg:.2f}%</div></div>',unsafe_allow_html=True)
            with rc_r:
                st.markdown(f'<div style="font-size:13px;font-weight:700;color:{RED};margin-bottom:8px;">📉 SECTOR LAGGARDS</div>',unsafe_allow_html=True)
                bottom_secs=sec_sorted2[-5:][::-1]
                for sec,chg in bottom_secs:
                    intensity=min(abs(chg)/3,1.0)
                    bg=f"rgba(239,68,68,{0.1+intensity*0.4})"
                    st.markdown(f'<div style="background:{bg};border:1px solid rgba(239,68,68,0.3);border-radius:8px;padding:12px 16px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;"><div><div style="font-size:13px;font-weight:700;color:#e2e8f0;">{sec}</div><div style="font-size:11px;color:#374f6e;">Money flowing out</div></div><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{RED};">▼{abs(chg):.2f}%</div></div>',unsafe_allow_html=True)
            if top_secs and bottom_secs:
                st.markdown(f'<div style="background:#0d1525;border:1px solid rgba(99,102,241,0.25);border-radius:10px;padding:16px;margin-top:16px;"><div style="font-size:13px;font-weight:700;color:#a5b4fc;margin-bottom:6px;">💡 Rotation Insight</div><div style="font-size:12px;color:#374f6e;line-height:1.7;">Money appears to be rotating <strong style="color:#4ade80;">into {top_secs[0][0]}</strong> and <strong style="color:#f87171;">out of {bottom_secs[0][0]}</strong>. This shift can suggest changing investor sentiment or sector-specific catalysts.</div></div>',unsafe_allow_html=True)

    # ── Module 10: Watchlist Analytics ──
    with tabs[9]:
        st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">⭐ Your Watchlist Analytics</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Performance and signal analysis for stocks you\'re tracking.</div>',unsafe_allow_html=True)
        wl=st.session_state.get("watchlist",[])
        if not wl:
            st.markdown(f'<div style="background:#0d1525;border:1px solid {BORDER};border-radius:10px;padding:32px;text-align:center;"><div style="font-size:32px;margin-bottom:10px;">📋</div><div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">Your watchlist is empty</div><div style="font-size:12px;color:#374f6e;margin-bottom:16px;">Add stocks to see analytics here.</div></div>',unsafe_allow_html=True)
            if st.button("→ Browse Stocks to Add",key="bi_wl_empty",type="primary",use_container_width=True): nav("discover")
        else:
            wl_data=[]; wl_prog=st.progress(0,f"Loading {len(wl)} watchlist stocks…")
            for i,t in enumerate(wl):
                wl_prog.progress((i+1)/len(wl))
                df=yf_ohlcv(t,30); info=yf_fund(t); sent=st_sent(t); q=get_quote(t)
                if df is None or df.empty or not q: continue
                sc,bd,op,risk,conf=compute_scores(df,info,sent)
                wl_data.append({"t":t,"price":q["price"],"pct":q["pct"],"score":sc,"risk":risk,"bull":sent["bull"]})
            wl_prog.empty()
            if wl_data:
                avg_score=sum(r["score"] for r in wl_data)/len(wl_data)
                avg_pct=sum(r["pct"] for r in wl_data)/len(wl_data)
                bull_count=sum(1 for r in wl_data if r["pct"]>0)
                ws_cols=st.columns(4)
                ws_cols[0].markdown(f'<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:14px;border-radius:10px;"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:#e2e8f0;">{len(wl_data)}</div><div style="font-size:11px;color:#374f6e;">Stocks Tracked</div></div>',unsafe_allow_html=True)
                ws_cols[1].markdown(f'<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:14px;border-radius:10px;"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{GREEN if avg_score>=60 else GOLD if avg_score>=40 else RED};">{avg_score:.0f}</div><div style="font-size:11px;color:#374f6e;">Avg Score</div></div>',unsafe_allow_html=True)
                ws_cols[2].markdown(f'<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:14px;border-radius:10px;"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{GREEN if avg_pct>0 else RED};">{avg_pct:+.2f}%</div><div style="font-size:11px;color:#374f6e;">Avg Today</div></div>',unsafe_allow_html=True)
                ws_cols[3].markdown(f'<div class="stat" style="background:#080b14;border:1px solid {BORDER};padding:14px;border-radius:10px;"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{GREEN};">{bull_count}/{len(wl_data)}</div><div style="font-size:11px;color:#374f6e;">Bullish Today</div></div>',unsafe_allow_html=True)
                st.markdown('<div style="height:14px;"></div>',unsafe_allow_html=True)
                if HAS_PLOTLY:
                    df_wl=pd.DataFrame(wl_data).sort_values("score",ascending=False)
                    colors_wl=[GREEN if s>=65 else GOLD if s>=45 else RED for s in df_wl["score"]]
                    fig=go.Figure(go.Bar(x=df_wl["t"],y=df_wl["score"],marker_color=colors_wl,
                        text=[f"{s}" for s in df_wl["score"]],textposition="outside",
                        textfont=dict(size=13,family="JetBrains Mono",color="#94a3b8")))
                    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",margin=dict(l=0,r=0,t=10,b=0),height=300,
                        yaxis=dict(range=[0,110],showgrid=False,color="#4a5e7a",title="Score"),
                        xaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=12)))
                    st.plotly_chart(fig,use_container_width=True)

    # ── TAB 11: Signal Performance (recommendation lifecycle analytics) ──
    with tabs[10]:
        st.markdown('<div style="font-size:13px;color:#6b7fa0;margin-bottom:12px;">How Scanviction\'s own recommendations have performed — '
                    'win rate and outcome mix by category, scored against the configurable horizon. Powered by the historical labeling engine.</div>',
                    unsafe_allow_html=True)
        sp_hz = st.radio("Horizon", HORIZONS, index=2, horizontal=True,
                         format_func=lambda d: f"{d}-day", key="bi_sp_horizon")
        try:
            sp_evals = evaluate_all_recommendations()
        except Exception:
            sp_evals = []
        if not sp_evals:
            st.info("No recommendation history yet. As Discover surfaces signals, their outcomes populate here automatically.")
        else:
            hr = category_hit_rates(sp_evals, horizon=sp_hz)
            # KPI summary
            resolved = [e for e in sp_evals if e["horizons"].get(sp_hz,{}).get("label","pending") != "pending"]
            wins = [e for e in resolved if e["horizons"][sp_hz]["label"]=="success"]
            overall = round(len(wins)/len(resolved)*100,1) if resolved else 0.0
            spk = st.columns(4)
            spk[0].markdown(f'<div class="stat"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:#818cf8;">{len(sp_evals)}</div><div style="font-size:11px;color:#374f6e;">Tracked Signals</div></div>',unsafe_allow_html=True)
            spk[1].markdown(f'<div class="stat"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:{"#4ade80" if overall>=50 else "#fbbf24"};">{overall}%</div><div style="font-size:11px;color:#374f6e;">{sp_hz}-Day Win Rate</div></div>',unsafe_allow_html=True)
            spk[2].markdown(f'<div class="stat"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:#4ade80;">{len(wins)}/{len(resolved)}</div><div style="font-size:11px;color:#374f6e;">Wins / Resolved</div></div>',unsafe_allow_html=True)
            spk[3].markdown(f'<div class="stat"><div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:800;color:#c084fc;">{len(hr)}</div><div style="font-size:11px;color:#374f6e;">Categories Scored</div></div>',unsafe_allow_html=True)
            st.markdown("<br>",unsafe_allow_html=True)
            if hr and HAS_PLOTLY:
                spc1, spc2 = st.columns(2, gap="small")
                with spc1:
                    st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">🎯 Hit Rate by Category</div>',unsafe_allow_html=True)
                    cats_sorted = sorted(hr.items(), key=lambda x:x[1]["hit_rate"], reverse=True)
                    names=[c for c,_ in cats_sorted]; rates=[a["hit_rate"] for _,a in cats_sorted]
                    bar_cols=[GREEN if r>=50 else GOLD if r>=33 else RED for r in rates]
                    fig=go.Figure(go.Bar(x=rates,y=names,orientation="h",marker_color=bar_cols,
                        text=[f"{r}%" for r in rates],textposition="outside",
                        textfont=dict(size=12,family="JetBrains Mono",color="#94a3b8")))
                    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                        margin=dict(l=0,r=50,t=10,b=0),height=max(220,40*len(names)),
                        xaxis=dict(range=[0,115],showgrid=False,color="#4a5e7a",title="Hit rate %"),
                        yaxis=dict(showgrid=False,color="#94a3b8",tickfont=dict(size=11)))
                    st.plotly_chart(fig,use_container_width=True)
                with spc2:
                    st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">📊 Outcome Distribution</div>',unsafe_allow_html=True)
                    tot_w=sum(a["wins"] for a in hr.values()); tot_l=sum(a["losses"] for a in hr.values()); tot_n=sum(a["neutral"] for a in hr.values())
                    if (tot_w+tot_l+tot_n)>0:
                        fig=go.Figure(go.Pie(labels=["Success","Failure","Neutral"],values=[tot_w,tot_l,tot_n],
                            marker=dict(colors=[GREEN,RED,"#6b7fa0"]),hole=0.55,
                            textinfo="label+percent",textfont=dict(size=12,family="Inter")))
                        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                            margin=dict(l=0,r=0,t=10,b=0),height=280,showlegend=False,
                            font=dict(color="#94a3b8"))
                        st.plotly_chart(fig,use_container_width=True)
                    else:
                        st.info(f"No resolved outcomes at {sp_hz}-day horizon yet.")
            elif not hr:
                st.info(f"No resolved outcomes at the {sp_hz}-day horizon yet — signals are still maturing.")
    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: WATCHLIST
# ─────────────────────────────────────────────────────────────
def page_watchlist():
    render_topbar("watchlist")
    st.markdown('<div class="page-wrap pw-narrow">' ,unsafe_allow_html=True)

    wl=st.session_state.get("watchlist",[])
    back_button("wl_back")
    hdr1,hdr2=st.columns([3,1])
    with hdr1: st.markdown('<div style="font-size:22px;font-weight:800;color:#e2e8f0;margin-bottom:4px;">⭐ My Watchlist</div>',unsafe_allow_html=True)
    with hdr2:
        if wl and st.button("🗑 Clear All",key="wl_clear_top",use_container_width=True):
            st.session_state.watchlist=[]; st.rerun()

    if not wl:
        st.markdown(f'''<div class="card" style="text-align:center;padding:48px 24px;">
            <div style="font-size:36px;margin-bottom:12px;">📋</div>
            <div style="font-size:18px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">Your watchlist is empty</div>
            <div style="font-size:13px;color:#374f6e;margin-bottom:20px;">Browse composite categories and click ➕ Watchlist on any stock to track it here.</div>
        </div>''',unsafe_allow_html=True)
        if st.button("Browse Stocks →",key="wl_browse3",type="primary"): nav("discover")
        st.markdown('</div>',unsafe_allow_html=True)
        return

    # Load watchlist data. Reuse the warm scan (instant for any scanned name — its already-computed
    # quote/score/info) and only cold-fetch genuinely off-scan tickers. One clean spinner instead of
    # the per-ticker progress bar that read as cramped/unpolished.
    rows=[]
    with st.spinner("Loading your watchlist…"):
        for t in wl:
            try:
                wr=_warm_row_for(t)
                if wr and wr.get("q"):
                    q=wr["q"]; sc=int(wr.get("sc",0) or 0); bd=wr.get("bd") or {}
                    info=wr.get("info") or {}; risk=wr.get("risk","") or ""
                else:
                    q=get_quote(t)
                    if not q: continue
                    df=yf_ohlcv(t,30); info=yf_fund(t); sent=st_sent(t)
                    sc,bd,op,risk,_=compute_scores(df,info,sent)
                if not q: continue
                rec_lbl,rec_clr,_=get_recommendation(sc,bd,info)
                pct=q.get("pct",0); price=q.get("price",0)
                cc_=GREEN if pct>=0 else RED
                rows.append({
                    "Ticker":t,"Name":q.get("name","")[:22],"Price":f"${price:,.2f}",
                    "Change":f"{pct:+.2f}%","Signal":rec_lbl,"Score":sc,
                    "Risk":risk,"Sector":info.get("sector","N/A"),
                    "Short Float":f"{(info.get('sf',0) or 0)*100:.1f}%",
                    "_pct":pct,"_cc":cc_,"_rec_clr":rec_clr
                })
            except Exception: continue

    if not rows:
        st.info("Could not load watchlist data. Try again in a moment.")
        st.markdown('</div>',unsafe_allow_html=True)
        return

    # ── Summary stats strip ──
    avg_score = sum(r["Score"] for r in rows) / len(rows) if rows else 0
    bull_count = sum(1 for r in rows if r["_pct"] > 0)
    avg_pct = sum(r["_pct"] for r in rows) / len(rows) if rows else 0
    best_perf = max(rows, key=lambda x: x["_pct"]) if rows else {}
    worst_perf = min(rows, key=lambda x: x["_pct"]) if rows else {}

    summary_cols = st.columns(5)
    summary_data = [
        (len(rows), "Stocks", "#818cf8"),
        (f"{avg_score:.0f}", "Avg Score", GREEN if avg_score >= 60 else GOLD if avg_score >= 40 else RED),
        (f"{avg_pct:+.2f}%", "Avg Today", GREEN if avg_pct >= 0 else RED),
        (f"{best_perf.get('Ticker','—')}", f"Best ({best_perf.get('_pct',0):+.1f}%)" if best_perf else "Best", GREEN),
        (f"{bull_count}/{len(rows)}", "Bullish", GREEN),
    ]
    for col, (val, lbl, color) in zip(summary_cols, summary_data):
        col.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:12px;text-align:center;">
            <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:800;color:{color};">{val}</div>
            <div style="font-size:11px;color:#374f6e;margin-top:3px;">{lbl}</div>
        </div>""", unsafe_allow_html=True)
    st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)

    # Premium score chart
    if is_premium() and HAS_PLOTLY:
        st.markdown('<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">📊 Score Distribution</div>',unsafe_allow_html=True)
        scores=[r["Score"] for r in rows]; tickers=[r["Ticker"] for r in rows]
        bar_colors=[GREEN if s>=65 else GOLD if s>=40 else RED for s in scores]
        fig=go.Figure(go.Bar(x=tickers,y=scores,marker_color=bar_colors,
            text=scores,textposition="outside",textfont=dict(size=13,family="JetBrains Mono")))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0,r=0,t=0,b=0),height=160,
            yaxis=dict(range=[0,110],showgrid=False,color="#4a5e7a"),
            xaxis=dict(showgrid=False,color="#818cf8",tickfont=dict(family="JetBrains Mono",size=12)))
        st.plotly_chart(fig,use_container_width=True)

    # Stock rows
    display_rows=[{k:v for k,v in r.items() if not k.startswith("_")} for r in rows]
    # Export button
    ex1,ex2=st.columns([1,3])
    with ex1:
        export_button(display_rows, "stockwins_watchlist.xlsx", "📥 Export Watchlist", "wl_export")

    # Sort selector
    with ex2:
        sort_options = ["Score (high→low)", "Change % (best→worst)", "Change % (worst→best)", "Ticker A→Z", "Risk (low→high)"]
        sort_choice = st.selectbox("Sort by", sort_options, key="wl_sort", label_visibility="collapsed")
        if sort_choice == "Score (high→low)":           rows.sort(key=lambda x: x["Score"], reverse=True)
        elif sort_choice == "Change % (best→worst)":     rows.sort(key=lambda x: x["_pct"], reverse=True)
        elif sort_choice == "Change % (worst→best)":     rows.sort(key=lambda x: x["_pct"])
        elif sort_choice == "Ticker A→Z":                rows.sort(key=lambda x: x["Ticker"])
        elif sort_choice == "Risk (low→high)":
            risk_order = {"Low":1, "Medium":2, "High":3, "Very High":4}
            rows.sort(key=lambda x: risk_order.get(x["Risk"], 5))

    st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)

    # ── Load notes (per ticker) ──
    db_user_wl = st.session_state.users_db.get(st.session_state.user.get("email",""),{}) if is_authed() else {}
    wl_notes = db_user_wl.get("watchlist_notes", {})

    # ── Card-style rows ──
    st.markdown('<div style="margin-top:12px;">',unsafe_allow_html=True)
    for r in rows:
        t=r["Ticker"]; cc_=r["_cc"]; rec_clr=r["_rec_clr"]
        existing_note = wl_notes.get(t, "")
        r1,r3,r4=st.columns([3,1,1],gap="small")
        with r1:
            note_badge = f'<span style="background:rgba(168,85,247,0.15);color:#c084fc;font-size:9px;font-weight:700;padding:2px 7px;border-radius:4px;border:1px solid rgba(168,85,247,0.3);margin-left:6px;">📝 Note</span>' if existing_note else ''
            st.markdown(f'''<div class="sr" style="padding:10px 14px;">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span class="sr-tick">{t}</span>
                    <span style="font-size:11px;color:#374f6e;">{r["Name"]}</span>
                    <span style="background:{rec_clr}22;color:{rec_clr};font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;border:1px solid {rec_clr}44;">{r["Signal"]}</span>{note_badge}
                </div>
                <div style="display:flex;gap:16px;margin-top:4px;font-size:12px;flex-wrap:wrap;">
                    <span style="font-family:'JetBrains Mono',monospace;color:#e2e8f0;font-weight:700;">{r["Price"]}</span>
                    <span style="font-weight:700;color:{cc_};">{r["Change"]}</span>
                    <span style="color:#374f6e;">Score: {r["Score"]}</span>
                    <span style="color:#374f6e;">{r["Risk"]} Risk</span>
                    <span style="color:#374f6e;">{r["Sector"]}</span>
                </div>
            </div>''',unsafe_allow_html=True)
        with r3:
            if st.button("📝 Note",key=f"wl_note_{t}",use_container_width=True):
                st.session_state[f"_editing_note_{t}"] = True
                st.rerun()
        with r4:
            if st.button("✕ Remove",key=f"wl_rm_{t}",use_container_width=True):
                _toggle_watchlist(t)
                st.toast(f"Removed {t}", icon="✅")
                st.rerun()

        # Inline note editor
        if st.session_state.get(f"_editing_note_{t}"):
            with st.form(f"note_form_{t}", clear_on_submit=False):
                note_text = st.text_area(f"Note for {t}", value=existing_note,
                                          placeholder="e.g. Watching for earnings catalyst on Nov 15. Entry zone: $145-150.",
                                          height=80, key=f"note_text_{t}")
                nc1, nc2 = st.columns(2)
                with nc1:
                    save_note = st.form_submit_button("💾 Save Note", type="primary", use_container_width=True)
                with nc2:
                    cancel_note = st.form_submit_button("Cancel", use_container_width=True)
                if save_note:
                    if is_authed():
                        uemail = st.session_state.user["email"]
                        if uemail in st.session_state.users_db:
                            if "watchlist_notes" not in st.session_state.users_db[uemail]:
                                st.session_state.users_db[uemail]["watchlist_notes"] = {}
                            if note_text.strip():
                                st.session_state.users_db[uemail]["watchlist_notes"][t] = note_text.strip()
                            else:
                                st.session_state.users_db[uemail]["watchlist_notes"].pop(t, None)
                            save_user_to_file(uemail, st.session_state.users_db[uemail])
                            st.toast(f"📝 Note saved for {t}", icon="✅")
                    st.session_state.pop(f"_editing_note_{t}", None)
                    st.rerun()
                if cancel_note:
                    st.session_state.pop(f"_editing_note_{t}", None)
                    st.rerun()
        elif existing_note:
            # Show note preview
            st.markdown(f'''<div style="background:rgba(168,85,247,0.06);border-left:3px solid #c084fc;
                        padding:8px 14px;margin:2px 0 8px;font-size:12px;color:#cbd5e1;font-style:italic;">
                📝 {existing_note}
            </div>''', unsafe_allow_html=True)

    # ── Compare Mode (Premium) ──
    if is_premium() and len(rows) >= 2:
        st.markdown('<div class="div-line"></div>', unsafe_allow_html=True)
        cmp_header_col1, cmp_header_col2 = st.columns([3,1])
        with cmp_header_col1:
            st.markdown(f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;">⚖️ Compare Mode</div><div style="font-size:11px;color:#374f6e;">Select 2-4 stocks to compare side-by-side</div>', unsafe_allow_html=True)
        with cmp_header_col2:
            cmp_active = st.toggle("Enable", key="cmp_toggle", value=st.session_state.get("_cmp_active", False))
            st.session_state["_cmp_active"] = cmp_active

        if cmp_active:
            all_tickers = [r["Ticker"] for r in rows]
            sel_compare = st.multiselect("Pick 2-4 tickers", all_tickers,
                                          default=all_tickers[:min(3, len(all_tickers))],
                                          max_selections=4, key="cmp_sel")
            if len(sel_compare) >= 2:
                cmp_data = [r for r in rows if r["Ticker"] in sel_compare]

                # Comparison columns
                cmp_cols = st.columns(len(cmp_data)) if cmp_data else []
                for cc, r in zip(cmp_cols, cmp_data):
                    rec_clr = r["_rec_clr"]
                    cc_color = r["_cc"]
                    cc.markdown(f"""<div style="background:#080b14;border:1px solid {rec_clr}44;border-radius:12px;padding:16px;">
                        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                            <span style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:900;color:#818cf8;">{r['Ticker']}</span>
                            <span style="background:{rec_clr}22;color:{rec_clr};font-size:9px;font-weight:800;padding:3px 8px;border-radius:5px;border:1px solid {rec_clr}44;">{r['Signal']}</span>
                        </div>
                        <div style="font-size:11px;color:#6b7fa0;margin-bottom:12px;line-height:1.5;">{r['Name'][:24]}</div>
                        <table style="width:100%;font-size:12px;color:#e2e8f0;border-collapse:collapse;">
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">Price</td><td style="padding:6px 0;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:700;">{r['Price']}</td></tr>
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">Today</td><td style="padding:6px 0;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:700;color:{cc_color};">{r['Change']}</td></tr>
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">SW Score</td><td style="padding:6px 0;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:700;color:{'#4ade80' if r['Score']>=65 else '#fbbf24' if r['Score']>=45 else '#f87171'};">{r['Score']}/100</td></tr>
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">Risk</td><td style="padding:6px 0;text-align:right;font-weight:700;">{r['Risk']}</td></tr>
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">Short Float</td><td style="padding:6px 0;text-align:right;font-family:'JetBrains Mono',monospace;">{r['Short Float']}</td></tr>
                            <tr style="border-top:1px solid rgba(255,255,255,0.06);"><td style="padding:6px 0;color:#6b7fa0;">Sector</td><td style="padding:6px 0;text-align:right;font-size:11px;">{r['Sector'][:18]}</td></tr>
                        </table>
                    </div>""", unsafe_allow_html=True)

                # Winner banner
                best_cmp = max(cmp_data, key=lambda x: x["Score"])
                st.markdown(f"""<div style="background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.3);
                            border-radius:10px;padding:12px 16px;margin-top:14px;text-align:center;font-size:12px;color:#4ade80;">
                    🏆 <strong>{best_cmp['Ticker']}</strong> has the highest Scanviction score in this comparison
                    ({best_cmp['Score']}/100, {best_cmp['Signal']})
                </div>""", unsafe_allow_html=True)
            else:
                st.info("Select at least 2 tickers to compare.")

    st.markdown('</div>',unsafe_allow_html=True)
    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: SCREENER
# ─────────────────────────────────────────────────────────────
# REMOVED: page_screener() ("Refine"). It filtered the CURRENT scanned universe by
# category, conviction, direction and technicals — which is what the Daily Brief's own
# filters now do, on the feed you're already looking at. A separate tab for narrowing
# the same data was a surface users had to remember; the /?page=screener and
# /?page=bi_dashboard routes now land on the Brief so old links keep working.


def page_settings():
    render_topbar("settings")
    st.markdown('<div class="page-wrap">' ,unsafe_allow_html=True)
    back_button("set_back")
    # Settings header with user info
    db_u_hdr = st.session_state.users_db.get(st.session_state.user["email"],{}) if is_authed() else {}
    role_disp = {"owner":"👑 Owner","admin":"🛡️ Admin","premium":"⭐ Premium","free":"👤 Free"}.get(st.session_state.get("role","free"),"👤 Free")
    plan_disp = db_u_hdr.get("plan","Free")
    st.markdown(f'''<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;">
        <div>
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">⚙️ Account Settings</div>
            <div style="font-size:13px;color:#374f6e;margin-top:3px;">{_esc(st.session_state.user.get("name",""))} · {_esc(st.session_state.user.get("email",""))}</div>
        </div>
        <div style="text-align:right;">
            <div style="font-size:13px;font-weight:700;color:{GOLD if is_premium() else "#6b7fa0"};">{role_disp}</div>
            <div style="font-size:11px;color:#2a3a52;">Billing: {plan_disp}</div>
        </div>
    </div>''',unsafe_allow_html=True)
    db_user=st.session_state.users_db.get(st.session_state.user["email"],{}) if is_authed() else {}
    email=st.session_state.user["email"] if is_authed() else ""

    # Resolve the LIVE Telegram bot name once (from the configured token) so every label reads the
    # real bot — not a stale hardcoded one. bot_username() does one getMe then caches (module global).
    try:
        import telegram_link as _tgl_name
        _tg_bot_name = _tgl_name.bot_username(st.secrets.get("TELEGRAM_BOT_TOKEN","") or "") or "Scanvictionbot"
    except Exception:
        _tg_bot_name = "Scanvictionbot"

    _base_tabs = ["👤 Profile","🔐 Security","🔔 Alerts","📨 Notifications","📧 Email Digest","📊 Subscription"]
    # System tab is normally owner/admin-only. ?diag=1 in the URL also reveals
    # it to any logged-in user — useful for owner-recovery / first-deploy
    # diagnostics when you can't yet sign in as owner. The panel shows data
    # source health and storage status (no credentials), so it's safe to
    # expose temporarily; just don't share the ?diag=1 URL.
    try:
        _diag_url = (st.query_params.get("diag", "") or "") == "1"
    except Exception:
        _diag_url = False
    _show_system = is_admin() or _diag_url
    if _show_system:
        _base_tabs.append("🩺 System")
    tabs=st.tabs(_base_tabs)

    with tabs[0]:
        st.markdown(f'<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:12px;">👤 Personal Information</div>',unsafe_allow_html=True)

        verified_email = db_user.get("verified", False)
        current_tg_id  = db_user.get("telegram_chat_id", "")
        push_subscribed = db_user.get("push_subscribed", False)

        with st.form("pf"):
            nn = st.text_input("Display Name", value=st.session_state.user.get("name",""),
                                help="Shown throughout the app")

            ec1, ec2 = st.columns([4,1])
            with ec1:
                st.text_input("Email Address", value=email, disabled=True,
                              help="Email cannot be changed. Contact support to migrate accounts.")
            with ec2:
                st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
                if verified_email:
                    st.markdown(f'<div style="background:rgba(34,197,94,0.1);color:{GREEN};border:1px solid rgba(34,197,94,0.3);border-radius:6px;padding:8px 10px;text-align:center;font-size:11px;font-weight:700;">✅ Verified</div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div style="background:rgba(245,158,11,0.1);color:{GOLD};border:1px solid rgba(245,158,11,0.3);border-radius:6px;padding:8px 10px;text-align:center;font-size:11px;font-weight:700;">⚠ Unverified</div>', unsafe_allow_html=True)

            if st.form_submit_button("💾 Save Profile Changes", type="primary", use_container_width=True):
                if nn and nn != st.session_state.user.get("name",""):
                    st.session_state.user["name"] = nn
                    if email in st.session_state.users_db:
                        st.session_state.users_db[email]["name"] = nn
                    _save_global_db(st.session_state.users_db)
                    save_user_to_file(email, st.session_state.users_db[email])
                    st.success("✅ Profile updated!")
                    st.rerun()

        # ── 🔔 PUSH NOTIFICATIONS Setup (OneSignal Web Push) ──
        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
        os_app_id = ""
        try: os_app_id = st.secrets.get("ONESIGNAL_APP_ID","")
        except Exception: pass

        push_status_color = GREEN if push_subscribed else GOLD
        push_status_text  = "✅ Active on this device" if push_subscribed else "⚠️ Not Enabled"
        st.markdown(f'''<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:10px;">
            <div>
                <div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:4px;">🔔 Push Notifications — The App-Style Experience</div>
                <div style="font-size:12px;color:#374f6e;line-height:1.7;">Instant push to your phone or desktop. Tap "Add to Home Screen" on mobile for a real app feel — zero install, zero fees.</div>
            </div>
            <div style="background:rgba({"34,197,94" if push_subscribed else "245,158,11"},0.1);color:{push_status_color};
                        border:1px solid rgba({"34,197,94" if push_subscribed else "245,158,11"},0.3);
                        border-radius:6px;padding:8px 14px;font-size:11px;font-weight:700;">{push_status_text}</div>
        </div>''', unsafe_allow_html=True)

        if HAS_WEBPUSH_NATIVE:
            # ── Native Web Push (VAPID) — self-hosted, no OneSignal, no third party ──
            _sid_tok = st.session_state.get("_sid", "")
            _vapid_pub = _webpush.get_public_key() if _webpush else ""
            native_push_html = f"""
            <div style="background:#080b14;border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 18px;margin-bottom:12px;font-family:Inter,sans-serif;font-size:12px;color:#374f6e;line-height:1.7;">
                <strong style="color:#e2e8f0;">How to enable on this device:</strong><br>
                <strong style="color:#a5b4fc;">1.</strong> Click the button below — your browser will ask permission to send notifications<br>
                <strong style="color:#a5b4fc;">2.</strong> Click <strong style="color:#4ade80;">Allow</strong> in the browser pop-up<br>
                <strong style="color:#a5b4fc;">3.</strong> On mobile: tap the share icon → <strong style="color:#4ade80;">Add to Home Screen</strong> for the full app feel
                <br><br>
                <button id="np-enable-btn" style="background:linear-gradient(135deg,#4f46e5,#6366f1);color:#fff;border:none;padding:10px 20px;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;width:100%;">
                    🔔 Enable Push Notifications
                </button>
                <div id="np-status" style="font-size:11px;color:#6b7fa0;margin-top:8px;text-align:center;"></div>
            </div>
            <script>
            function _npStatus(m) {{ var s = document.getElementById('np-status'); if (s) s.innerHTML = m; }}
            function _npKey(base64) {{
                var pad = '='.repeat((4 - base64.length % 4) % 4);
                var b = (base64 + pad).replace(/\\-/g, '+').replace(/_/g, '/');
                var raw = atob(b); var out = new Uint8Array(raw.length);
                for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
                return out;
            }}
            document.getElementById('np-enable-btn').onclick = async function () {{
                try {{
                    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {{
                        _npStatus('⚠️ This browser does not support Web Push.'); return;
                    }}
                    _npStatus('Registering…');
                    var reg = await navigator.serviceWorker.register('/app/static/sw.js');
                    var perm = await Notification.requestPermission();
                    if (perm !== 'granted') {{ _npStatus('⚠️ Permission denied — check browser settings.'); return; }}
                    var sub = await reg.pushManager.getSubscription();
                    if (!sub) {{
                        sub = await reg.pushManager.subscribe({{
                            userVisibleOnly: true,
                            applicationServerKey: _npKey('{_vapid_pub}')
                        }});
                    }}
                    var b64 = btoa(JSON.stringify(sub)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
                    _npStatus('✅ Enabled! Registering with Scanviction…');
                    setTimeout(function () {{
                        var base = window.top.location.pathname;
                        window.top.location.href = base + '?page=settings&push_sub=' + b64 + '&sid={_sid_tok}';
                    }}, 600);
                }} catch (e) {{
                    _npStatus('❌ ' + (e && e.message ? e.message : e));
                }}
            }};
            </script>
            """
            try:
                components.html(native_push_html, height=240)
            except Exception:
                st.markdown(native_push_html, unsafe_allow_html=True)
            st.caption("📲 Runs entirely on Scanviction's own push keys — no third-party service. "
                       "On iPhone you need iOS 16.4+ and the app installed via Safari → Share → Add to Home Screen.")
        elif not os_app_id:
            st.markdown(f'''<div style="background:#0d1525;border:1px solid rgba(245,158,11,0.3);border-radius:10px;padding:14px 18px;margin-bottom:12px;font-size:12px;color:#374f6e;line-height:1.7;">
                ⚠️ <strong style="color:{GOLD};">Push notifications not yet configured.</strong> Generate a free VAPID key pair and add
                <code style="background:#060a12;color:#4ade80;padding:1px 6px;border-radius:3px;">VAPID_PUBLIC_KEY</code> +
                <code style="background:#060a12;color:#4ade80;padding:1px 6px;border-radius:3px;">VAPID_PRIVATE_KEY</code> to Secrets
                (see webpush.py for the one-liner). Self-hosted — no OneSignal or other third-party account needed.
            </div>''', unsafe_allow_html=True)
        else:
            # Inject OneSignal SDK init when configured
            push_html = f"""
            <div id="onesignal-bell-container" style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px 18px;margin-bottom:12px;font-size:12px;color:#374f6e;line-height:1.7;">
                <strong style="color:#e2e8f0;">How to enable on this device:</strong><br>
                <strong style="color:#a5b4fc;">1.</strong> Click the button below — your browser will ask permission to send notifications<br>
                <strong style="color:#a5b4fc;">2.</strong> Click <strong style="color:#4ade80;">Allow</strong> in the browser pop-up<br>
                <strong style="color:#a5b4fc;">3.</strong> On mobile: tap the share icon → <strong style="color:#4ade80;">Add to Home Screen</strong> for the full app feel
                <br><br>
                <button id="ps-enable-btn" style="background:linear-gradient(135deg,#4f46e5,#6366f1);color:#fff;border:none;padding:10px 20px;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;width:100%;">
                    🔔 Enable Push Notifications
                </button>
                <div id="ps-status" style="font-size:11px;color:#6b7fa0;margin-top:8px;text-align:center;"></div>
            </div>
            <script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
            <script>
            window.OneSignalDeferred = window.OneSignalDeferred || [];
            OneSignalDeferred.push(async function(OneSignal) {{
                await OneSignal.init({{
                    appId: "{os_app_id}",
                    allowLocalhostAsSecureOrigin: true,
                    notifyButton: {{ enable: false }},
                }});
                document.getElementById("ps-enable-btn").onclick = async () => {{
                    const status = document.getElementById("ps-status");
                    status.textContent = "Requesting permission...";
                    try {{
                        await OneSignal.Notifications.requestPermission();
                        const isSubscribed = OneSignal.User.PushSubscription.optedIn;
                        if (isSubscribed) {{
                            const subId = OneSignal.User.PushSubscription.id;
                            // Tag the user with their email for targeting
                            await OneSignal.login("{email}");
                            status.innerHTML = "✅ Push enabled! Registering with Scanviction...";
                            // Redirect with push_sub_id so the Python backend can save it
                            setTimeout(() => {{
                                // window.top + &sid= : the srcdoc iframe's own pathname is about:srcdoc,
                                // and a redirect without the session token logged the user out — so the
                                // subscription was never saved (is_authed() was false on return).
                                window.top.location.href = window.top.location.pathname + "?page=settings&push_sub_id=" + encodeURIComponent(subId) + "&sid={st.session_state.get('_sid','')}";
                            }}, 800);
                        }} else {{
                            status.textContent = "⚠️ Permission denied. Check browser settings.";
                        }}
                    }} catch (e) {{
                        status.textContent = "❌ Error: " + e.message;
                    }}
                }};
            }});
            </script>
            """
            try:
                # KEPT on components.html (deprecated) deliberately: this loads the external
                # OneSignal SDK, which needs a real same-origin context. components.html uses
                # a srcdoc iframe (inherits the page origin); a data: URL would give it an
                # opaque origin (SDK breaks) and inline st.html can't be verified here (no
                # ONESIGNAL_APP_ID in test). Not worth risking a working feature for a soft
                # deprecation. See _html_iframe for the migrated visual blocks.
                components.html(push_html, height=240)
            except Exception:
                st.markdown(push_html, unsafe_allow_html=True)
            st.caption("📲 Tip: On iPhone, you need iOS 16.4+ and to install the app via Safari → Share → Add to Home Screen for push to work.")

        # ── ✈️ TELEGRAM Setup ──
        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
        if not is_premium():
            # Telegram is a PREMIUM channel (pricing markets "Real-Time Telegram Alerts") and
            # delivery is premium-gated (_deliver_new_signals). So free users get an upgrade
            # prompt here instead of a dead-end connect flow that would never deliver. In-app
            # badge/toast/banner still work for them (tier-filtered).
            st.markdown(f'''<div style="background:linear-gradient(135deg,{GOLD}14,transparent);border:1px solid {GOLD}44;border-radius:12px;padding:16px 18px;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
                    <span style="font-size:15px;">✈️</span>
                    <span style="font-size:14px;font-weight:800;color:#e2e8f0;">Telegram Alerts</span>
                    <span style="font-size:10px;font-weight:800;color:{GOLD};background:{GOLD}1f;border:1px solid {GOLD}55;border-radius:5px;padding:2px 8px;">🔒 PREMIUM</span>
                </div>
                <div style="font-size:12px;color:#374f6e;line-height:1.7;">Get every new signal pushed to Telegram in real time. Upgrade to Premium to connect your account.</div>
            </div>''', unsafe_allow_html=True)
            if st.button("⭐ Upgrade to unlock Telegram alerts →", key="tg_upsell", use_container_width=True):
                nav("pricing")
        else:
            tg_status_color = GREEN if current_tg_id else GOLD
            tg_status_text  = "✅ Connected" if current_tg_id else "⚠️ Not Connected"
            st.markdown(f'''<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:10px;">
                <div>
                    <div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:4px;">✈️ Telegram — Backup Channel</div>
                    <div style="font-size:12px;color:#374f6e;line-height:1.7;">Prefer Telegram? Use it instead of (or alongside) push notifications.</div>
                </div>
                <div style="background:rgba({"34,197,94" if current_tg_id else "245,158,11"},0.1);color:{tg_status_color};
                            border:1px solid rgba({"34,197,94" if current_tg_id else "245,158,11"},0.3);
                            border-radius:6px;padding:8px 14px;font-size:11px;font-weight:700;">{tg_status_text}</div>
            </div>''', unsafe_allow_html=True)

            # One-tap connect (primary): the user taps a deep link + presses Start; the background
            # listener (msp-tg-listener) INSTANTLY auto-links their chat_id and replies in Telegram.
            # The UI finishes by picking up that completed link — it does NOT poll getUpdates itself
            # (the listener is the single consumer). Manual Chat-ID form below = fallback + Disconnect.
            import telegram_link as _tglink
            try: _tg_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "") or ""
            except Exception: _tg_token = ""
            if _tg_token:
                _tglink.start_listener(_tg_token)   # idempotent — ensure the bot answers Start

            def _tg_apply(cid):
                """Persist a freshly linked chat_id to this user and flip to Connected."""
                st.session_state.users_db[email]["telegram_chat_id"] = cid
                _save_global_db(st.session_state.users_db)
                save_user_to_file(email, st.session_state.users_db[email])
                st.session_state.pop("tg_link_deep", None)
                st.toast("✈️ Telegram connected!", icon="✅")
                st.success("✅ Connected — your alerts will come through Telegram.")
                time.sleep(0.8); st.rerun()

            if not current_tg_id and _tg_token:
                _auto = _tglink.pop_completed_link(email)   # listener already linked it? finish silently
                if _auto:
                    _tg_apply(_auto)
                _bot = _tg_bot_name
                if not st.session_state.get("tg_link_deep"):
                    try: _tok, _deep = _tglink.make_link_token(email, _bot)
                    except Exception: _tok, _deep = "", f"https://t.me/{_bot}"
                    st.session_state["tg_link_deep"] = _deep
                    st.session_state["tg_link_code"] = _tok
                _deep = st.session_state["tg_link_deep"]; _code = st.session_state.get("tg_link_code", "")
                st.markdown(f'''<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px 18px;margin-bottom:10px;font-size:12px;color:#374f6e;line-height:1.95;">
                    <strong style="color:#a5b4fc;">1.</strong> Tap <a href="{_deep}" target="_blank" style="color:#818cf8;font-weight:700;text-decoration:none;">Open @{_bot} →</a> and press <code style="background:#1a1f2e;color:#4ade80;padding:1px 6px;border-radius:3px;">Start</code> — the bot replies instantly.<br>
                    <strong style="color:#a5b4fc;">2.</strong> Already use the bot? Send it this code instead → <code style="background:#1a1f2e;color:#fbbf24;padding:2px 8px;border-radius:3px;font-weight:700;">{_code}</code><br>
                    <strong style="color:#a5b4fc;">3.</strong> Then tap <strong style="color:#e2e8f0;">Check connection</strong>.
                </div>''', unsafe_allow_html=True)
                if st.button("🔗 Check connection", key="tg_check", type="primary", use_container_width=True):
                    _cid = _tglink.pop_completed_link(email)
                    if not _cid and not _tglink.is_listening():
                        # No background listener (rare) — fall back to a one-shot poll from the UI.
                        for _em, _c in _tglink.poll_links(_tg_token):
                            if _em in st.session_state.users_db:
                                st.session_state.users_db[_em]["telegram_chat_id"] = _c
                                save_user_to_file(_em, st.session_state.users_db[_em])
                            if _em == email: _cid = _c
                        if _cid: _save_global_db(st.session_state.users_db)
                    if _cid:
                        _tg_apply(_cid)
                    else:
                        st.warning("Not linked yet — open the bot via the link above, tap **Start**, then check again.")
                st.caption("Or paste a Chat ID manually below.")
            elif not current_tg_id and not _tg_token:
                st.markdown(f'''<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:12px 18px;margin-bottom:12px;font-size:12px;color:#374f6e;line-height:1.8;">
                    Telegram isn't configured on this deployment yet (<code style="background:#1a1f2e;color:#94a3b8;padding:1px 6px;border-radius:3px;">TELEGRAM_BOT_TOKEN</code> missing in secrets).
                </div>''', unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="font-size:11px;color:#4a5e7a;margin-bottom:8px;">Linked Chat ID: <code style="background:#080b14;color:#4ade80;padding:2px 8px;border-radius:4px;">{current_tg_id}</code></div>', unsafe_allow_html=True)

            with st.form("tg_setup"):
                tg_id = st.text_input("Telegram Chat ID", value=current_tg_id,
                                        placeholder="e.g. 1234567890",
                                        help=f"The number @{_tg_bot_name} replied with")
                tg_c1, tg_c2 = st.columns(2)
                with tg_c1:
                    tg_save = st.form_submit_button(
                        "✅ Save & Test" if not current_tg_id else "💾 Update",
                        type="primary", use_container_width=True)
                with tg_c2:
                    tg_remove = st.form_submit_button("🗑 Disconnect", use_container_width=True,
                                                        disabled=not current_tg_id)
                if tg_save:
                    clean = "".join(c for c in tg_id if c.isdigit() or c == "-")
                    if not clean:
                        st.error(f"Chat ID must be a number (the ID @{_tg_bot_name} replies with) — not the bot token.")
                    else:
                        # VALIDATE before saving. A chat_id only works if it's real AND the user
                        # has tapped Start (Telegram refuses the send otherwise), so a successful
                        # test message is proof of a genuine connection. Only persist + show
                        # "connected" when it actually delivers — never save an unverified ID.
                        ok, info = _send_telegram(clean, "✅ <b>Scanviction Telegram alerts connected!</b>\n\nYou'll get instant alerts here.")
                        if ok:
                            st.session_state.users_db[email]["telegram_chat_id"] = clean
                            _save_global_db(st.session_state.users_db)
                            save_user_to_file(email, st.session_state.users_db[email])
                            st.toast("✈️ Telegram connected — check your phone!", icon="✅")
                            st.success("✅ Connected — a test message just hit your Telegram.")
                            time.sleep(1); st.rerun()
                        else:
                            st.error(f"That Chat ID didn't work, so it was NOT saved. Tap **Start** in @{_tg_bot_name}, then paste the number it replies with (not the bot token). [{info}]")
                if tg_remove:
                    st.session_state.users_db[email]["telegram_chat_id"] = ""
                    _save_global_db(st.session_state.users_db)
                    save_user_to_file(email, st.session_state.users_db[email])
                    st.toast("✈️ Telegram disconnected", icon="✅"); st.rerun()

        # ── Email verification ──
        if not verified_email:
            st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">📧 Verify Your Email</div>',unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:10px;">Send a verification code to <strong style="color:#e2e8f0;">{_esc(email)}</strong></div>',unsafe_allow_html=True)
            if st.button("📧 Send Email Verification Code", key="email_send_code", type="primary", use_container_width=True):
                code = str(random.randint(100000, 999999))
                st.session_state["_verify_code"] = code
                st.session_state["_verify_email"] = email
                st.session_state["_verify_user"] = {"name": st.session_state.user.get("name","")}
                ok, info = _send_verification_email(email, code)
                if not ok and info and info.startswith("DEMO_CODE:"):
                    st.session_state["_demo_code"] = info.split(":",1)[1]
                nav("verify_email")

    with tabs[1]:
        st.markdown(f'<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:12px;">🔐 Change Password</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px 18px;margin-bottom:14px;font-size:12px;color:#374f6e;line-height:1.7;">Strong passwords protect your account. Use 8+ characters mixing letters, numbers, and symbols.</div>',unsafe_allow_html=True)

        with st.form("pwf"):
            cp = st.text_input("Current Password", type="password", help="Required to change your password")
            np_ = st.text_input("New Password", type="password", placeholder="Minimum 8 characters")
            np2 = st.text_input("Confirm New Password", type="password")
            if st.form_submit_button("🔐 Update Password", type="primary", use_container_width=True):
                if not cp or not np_ or not np2:
                    st.error("Please fill in all fields.")
                elif not verify_pw(cp, db_user.get("pw","")):
                    st.error("❌ Current password is incorrect.")
                elif np_ != np2:
                    st.error("New passwords don't match.")
                elif len(np_) < 8:
                    st.error("Password must be at least 8 characters.")
                elif np_ == cp:
                    st.error("New password must be different from current.")
                else:
                    st.session_state.users_db[email]["pw"] = hp(np_)
                    _save_global_db(st.session_state.users_db)
                    save_user_to_file(email, st.session_state.users_db[email])
                    st.toast("🔐 Password changed!", icon="✅")
                    st.success("✅ Password updated successfully!")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">🚪 Account Sessions</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Currently signed in as <strong style="color:#e2e8f0;">{_esc(email)}</strong></div>',unsafe_allow_html=True)
        if st.button("🚪 Sign Out of This Session", key="set_logout", use_container_width=True):
            logout()

    with tabs[2]:
        # ── Section 1: Proprietary Signals (default ON for premium) ──
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#04200d,#0d1525);border:1px solid rgba(34,197,94,0.25);
                    border-radius:12px;padding:16px 20px;margin-bottom:16px;">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
                <div>
                    <div style="font-size:13px;font-weight:700;color:#4ade80;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:4px;">✨ YOUR DAILY BRIEF</div>
                    <div style="font-size:13px;color:#e2e8f0;font-weight:600;margin-bottom:4px;">Default ON for Premium subscribers</div>
                    <div style="font-size:12px;color:#6b7fa0;line-height:1.7;">Your Daily Brief pushes what's NEW since you last looked — stocks newly entering a signal category, plus insider buys, 8-K filings and short-interest surges. It's the "what changed" companion to Discover (the full ranked board). No configuration needed.</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if is_premium():
            db_user_a = st.session_state.users_db.get(st.session_state.user["email"],{}) if is_authed() else {}
            prop_signals_enabled = db_user_a.get("prop_signals_enabled", True)  # Default ON
            current_cat_alerts = db_user_a.get("category_alerts", list(COMPOSITE_CATS.keys()) + list(EVENT_ALERT_TYPES))

            colp1, colp2 = st.columns([3,1], gap="small")
            with colp1:
                st.markdown(f"""<div style="background:#0d1525;border:1px solid {BORDER};border-radius:10px;padding:14px;">
                    <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">📡 Your Daily Brief covers {len(current_cat_alerts)} of {len(COMPOSITE_CATS)} signal categories</div>
                    <div style="font-size:11px;color:#374f6e;line-height:1.7;">{', '.join([c.split(' ')[1] if ' ' in c else c for c in current_cat_alerts[:6]])}{'...' if len(current_cat_alerts)>6 else ''}</div>
                </div>""", unsafe_allow_html=True)
            with colp2:
                new_state = st.toggle("Auto-Signals", value=prop_signals_enabled, key="prop_toggle",
                                       help="Toggle automatic proprietary signal notifications")
                if new_state != prop_signals_enabled:
                    st.session_state.users_db[st.session_state.user["email"]]["prop_signals_enabled"] = new_state
                    save_user_to_file(st.session_state.user["email"], st.session_state.users_db[st.session_state.user["email"]])
                    st.toast(f"{'✅ Proprietary signals enabled' if new_state else '⏸ Proprietary signals paused'}", icon="🔔")

            with st.expander("⚙️ Customize your Daily Brief", expanded=False):
                st.markdown('<div style="font-size:12px;color:#374f6e;margin-bottom:8px;">Uncheck any category you don\'t want in your Daily Brief.</div>', unsafe_allow_html=True)
                selected_cats = []
                st.markdown('<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:1.5px;text-transform:uppercase;margin:2px 0 6px;">Signal categories</div>', unsafe_allow_html=True)
                cat_cols = st.columns(2, gap="small")
                for idx, cat in enumerate(list(COMPOSITE_CATS.keys())):
                    with cat_cols[idx % 2]:
                        if st.checkbox(_clean_name(cat), value=cat in current_cat_alerts, key=f"propcat_{idx}"):
                            selected_cats.append(cat)
                st.markdown('<div style="font-size:11px;font-weight:700;color:#4a5e7a;letter-spacing:1.5px;text-transform:uppercase;margin:14px 0 6px;">Filing &amp; data events</div>', unsafe_allow_html=True)
                _ev_labels = {EVT_INSIDER: "Insider buys (SEC Form 4)", EVT_8K: "Fresh 8-K filings", EVT_SHORT: "Short-interest surges"}
                ev_cols = st.columns(2, gap="small")
                for idx, ev in enumerate(EVENT_ALERT_TYPES):
                    with ev_cols[idx % 2]:
                        if st.checkbox(_ev_labels.get(ev, ev), value=ev in current_cat_alerts, key=f"propevt_{idx}"):
                            selected_cats.append(ev)
                if st.button("💾 Save alert preferences", key="save_prop_cats", type="primary", use_container_width=True):
                    st.session_state.users_db[st.session_state.user["email"]]["category_alerts"] = selected_cats
                    save_user_to_file(st.session_state.user["email"], st.session_state.users_db[st.session_state.user["email"]])
                    st.success(f"✅ You'll receive {len(selected_cats)} alert type{'s' if len(selected_cats)!=1 else ''}")
                    st.rerun()
        else:
            st.markdown(f"""<div class="card card-gold">
                <div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Required</div>
                <div style="font-size:12px;color:#374f6e;line-height:1.7;">Automatic proprietary signal alerts (Squeeze Setup, Hidden Mover, Sentiment Flip, Smart Money Signal, etc.) are part of the Premium plan.</div>
            </div>""", unsafe_allow_html=True)
            if gold_btn("Upgrade to Receive Auto-Signals","prop_upgrade"): nav("pricing")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # ── Section 2: Custom Alerts (user-configured for specific tickers) ──
        alerts=st.session_state.get("alerts",[])
        st.markdown(f'<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">🎯 Custom Alerts</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:14px;">Set up alerts for specific tickers and conditions (price, volume, RSI, etc.). These run alongside the automatic proprietary signals above.</div>',unsafe_allow_html=True)

        with st.form("af",clear_on_submit=True):
            fc1,fc2,fc3=st.columns(3)
            with fc1: at=st.text_input("Ticker",placeholder="AAPL",label_visibility="visible").upper().strip()
            with fc2:
                atype_lbl=st.selectbox("Alert Type",["Price Above","Price Below","% Change Up",
                    "Volume Spike (×avg)","RSI Oversold (<30)","RSI Overbought (>70)","Sentiment Bullish %"],label_visibility="visible")
            with fc3: ap=st.number_input("Threshold",value=100.0,min_value=0.0,step=0.5,label_visibility="visible")
            ch_col1,ch_col2,ch_col3=st.columns(3)
            with ch_col1: ch_email=st.checkbox("📧 Email",value=True)
            with ch_col2: ch_tg=st.checkbox("✈️ Telegram",value=False,disabled=not is_premium(),help="Premium only")
            with ch_col3: ch_push=st.checkbox("🔔 Browser Push",value=False,disabled=not is_premium(),help="Premium only")
            if st.form_submit_button("➕ Add Custom Alert",type="primary",use_container_width=True) and at:
                type_map={"Price Above":"price_above","Price Below":"price_below","% Change Up":"pct_change",
                          "Volume Spike (×avg)":"volume_spike","RSI Oversold (<30)":"rsi_oversold",
                          "RSI Overbought (>70)":"rsi_overbought","Sentiment Bullish %":"sentiment_flip"}
                channels=[]
                if ch_email: channels.append("email")
                if ch_tg and is_premium(): channels.append("telegram")
                if ch_push and is_premium(): channels.append("browser")
                new_a={"id":f"{at}_{time.time():.0f}","ticker":at,"type":type_map.get(atype_lbl,"price_above"),
                       "threshold":ap,"label":f"{at} {atype_lbl} {ap}","channels":channels or ["email"],
                       "active":True,"created":datetime.now().strftime("%Y-%m-%d %H:%M")}
                alerts.append(new_a); st.session_state.alerts=alerts
                if is_authed(): save_alerts_to_file(st.session_state.user["email"], alerts)
                st.toast(f"🔔 Alert set: {at} {atype_lbl} {ap}", icon="✅")
                st.success(f"✅ Alert active: {at} will notify you via {', '.join(channels or ['email'])}")

        if alerts:
            st.caption(f"{len(alerts)} active custom alert{'s' if len(alerts)!=1 else ''}")
            for i,a in enumerate(alerts):
                ac1,ac2,ac3=st.columns([5,1,1])
                with ac1:
                    ticker=a.get("ticker",""); lbl=a.get("label",ticker)
                    chs=", ".join(["📧" if c=="email" else "✈️" if c=="telegram" else "🔔" for c in a.get("channels",["email"])])
                    dot=f'<span style="color:{"#4ade80" if a.get("active",True) else "#4a5e7a"};">●</span>'
                    st.markdown(f'<div class="card" style="padding:9px 14px;margin-bottom:4px;">{dot} <span style="font-family:\'JetBrains Mono\',monospace;color:#818cf8;font-weight:700;">{ticker}</span> <span style="font-size:12px;color:#374f6e;margin-left:8px;">{lbl}</span><span style="font-size:11px;color:#2a3a52;float:right;">{chs}</span></div>',unsafe_allow_html=True)
                with ac2:
                    tog="Pause" if a.get("active",True) else "Resume"
                    if st.button(tog,key=f"tg_{i}",use_container_width=True):
                        alerts[i]["active"]=not a.get("active",True); st.session_state.alerts=alerts
                        if is_authed(): save_alerts_to_file(st.session_state.user["email"], alerts)
                        st.rerun()
                with ac3:
                    if st.button("🗑",key=f"da_{i}",use_container_width=True):
                        alerts.pop(i); st.session_state.alerts=alerts
                        if is_authed(): save_alerts_to_file(st.session_state.user["email"], alerts)
                        st.rerun()
        else:
            st.caption("No custom alerts yet. Use the form above to add one.")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # Telegram setup for premium
        if is_premium():
            st.markdown('<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">✈️ Telegram Setup</div>',unsafe_allow_html=True)
            try: tg_token_set = bool(st.secrets.get("TELEGRAM_BOT_TOKEN",""))
            except: tg_token_set = False
            db_user2=st.session_state.users_db.get(st.session_state.user["email"],{}) if is_authed() else {}
            current_tg=db_user2.get("telegram_chat_id","")
            if not tg_token_set:
                st.markdown(f'<div class="card" style="font-size:12px;color:#374f6e;">Add <code>TELEGRAM_BOT_TOKEN</code> to Streamlit Secrets to enable Telegram alerts.</div>',unsafe_allow_html=True)
            else:
                if current_tg:
                    st.markdown(f'<div style="font-size:12px;color:#4ade80;margin-bottom:8px;">✅ Telegram connected (Chat ID: {current_tg})</div>',unsafe_allow_html=True)
                else:
                    st.markdown(f'<div style="font-size:12px;color:#374f6e;line-height:1.8;margin-bottom:8px;">1. Open Telegram → search <strong style="color:#e2e8f0;">@{_tg_bot_name}</strong><br>2. Send /start to get your Chat ID<br>3. Paste it below</div>',unsafe_allow_html=True)
                with st.form("tg_form"):
                    tg_id=st.text_input("Your Telegram Chat ID",value=current_tg,placeholder="1234567890",label_visibility="visible",help="Your numeric Chat ID — NOT the bot token. Easiest: use the one-tap connect in Settings → Profile.")
                    if st.form_submit_button("Save Telegram Connection",type="primary"):
                        uemail=st.session_state.user["email"]
                        _clean=("".join(c for c in tg_id if c.isdigit() or c=="-")).strip()
                        if not _clean:
                            st.error("Chat ID must be a number (not the bot token).")
                        else:
                            # Validate via a real test send — only mark connected if it delivers.
                            _ok,_info=_send_telegram(_clean,"✅ <b>Scanviction Telegram alerts connected!</b>\n\nYou'll get your signal alerts here.")
                            if _ok:
                                st.session_state.users_db[uemail]["telegram_chat_id"]=_clean
                                save_user_to_file(uemail, st.session_state.users_db[uemail])
                                _save_global_db(st.session_state.users_db)
                                st.success("✅ Telegram connected — test message sent!"); st.rerun()
                            else:
                                st.error(f"That Chat ID didn't work, so it was NOT saved. Tap Start in @{_tg_bot_name} and paste the number it replies with. [{_info}]")

        if not is_premium():
            st.markdown(f'<div class="card card-gold" style="margin-top:12px;"><div style="font-size:12px;font-weight:700;color:{GOLD};margin-bottom:4px;">👑 Premium Alert Channels</div><div style="font-size:12px;color:#374f6e;">Upgrade to Premium for instant Telegram alerts and real-time browser push notifications on composite category signals.</div></div>',unsafe_allow_html=True)

    with tabs[3]:
        # ── Notification Preferences (master toggles) ──
        st.markdown(f'<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">📨 Notification Preferences</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:18px;">Master controls for all notifications. Use Alerts tab for specific ticker alerts.</div>',unsafe_allow_html=True)

        # Default preferences if not set
        notif_prefs = db_user.get("notif_prefs", {
            "email_enabled": True,
            "push_enabled": True,
            "telegram_enabled": True if db_user.get("telegram_chat_id","") else False,
            "daily_digest": False,
            "weekly_digest": False,
            "proprietary_signals": True,
            "watchlist_alerts": True,
            "category_alerts": True,
            "marketing": False,
        })

        # ── Channel Toggles (3 channels: Push, Telegram, Email) ──
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#a5b4fc;margin-bottom:10px;">📡 DELIVERY CHANNELS</div>',unsafe_allow_html=True)
        nc1, nc2, nc3 = st.columns(3)
        with nc1:
            new_push = st.toggle("🔔 Push notifications",
                                  value=notif_prefs.get("push_enabled", True),
                                  key="np_push",
                                  help="Browser/mobile push via OneSignal — enable on the Profile tab first")
        with nc2:
            tg_disabled = not bool(db_user.get("telegram_chat_id",""))
            tg_help = "Connect Telegram in Profile tab first" if tg_disabled else f"Push messages via @{_tg_bot_name}"
            new_telegram = st.toggle("✈️ Telegram alerts",
                                       value=notif_prefs.get("telegram_enabled", False),
                                       key="np_telegram", disabled=tg_disabled, help=tg_help)
        with nc3:
            new_email = st.toggle("📧 Email notifications",
                                    value=notif_prefs.get("email_enabled", True),
                                    key="np_email")
        if tg_disabled:
            st.caption(f"⚠️ Connect Telegram in the Profile tab to enable @{_tg_bot_name} alerts.")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # ── Digest Toggles ──
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#a5b4fc;margin-bottom:10px;">📨 DIGESTS</div>',unsafe_allow_html=True)
        dc1, dc2 = st.columns(2)
        with dc1:
            new_daily = st.toggle("📅 Daily digest", value=notif_prefs.get("daily_digest",False), key="np_daily", help="Daily email at 7am ET with top opportunities")
        with dc2:
            new_weekly = st.toggle("📆 Weekly digest", value=notif_prefs.get("weekly_digest",False), key="np_weekly", help="Weekly email Monday 7am ET")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # ── Alert Type Toggles ──
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#a5b4fc;margin-bottom:10px;">🔔 ALERT CATEGORIES</div>',unsafe_allow_html=True)
        ac1, ac2 = st.columns(2)
        with ac1:
            new_prop = st.toggle("✨ Proprietary signal alerts", value=notif_prefs.get("proprietary_signals",True), key="np_prop", disabled=not is_premium(), help="Squeeze, Hidden Mover, Sentiment Flip, etc.")
            new_wl = st.toggle("⭐ Watchlist alerts", value=notif_prefs.get("watchlist_alerts",True), key="np_wl")
        with ac2:
            new_cat = st.toggle("📊 Category alerts", value=notif_prefs.get("category_alerts",True), key="np_cat", disabled=not is_premium())
            new_mkt = st.toggle("📢 Product updates & news", value=notif_prefs.get("marketing",False), key="np_mkt")

        if not is_premium():
            st.caption("👑 Some alert categories require Premium.")

        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)

        # ── Save button ──
        if st.button("💾 Save Notification Preferences", key="save_notif_prefs", type="primary", use_container_width=True):
            new_prefs = {
                "push_enabled": new_push,
                "telegram_enabled": new_telegram,
                "email_enabled": new_email,
                "daily_digest": new_daily, "weekly_digest": new_weekly,
                "proprietary_signals": new_prop, "watchlist_alerts": new_wl,
                "category_alerts": new_cat, "marketing": new_mkt,
            }
            st.session_state.users_db[email]["notif_prefs"] = new_prefs
            _save_global_db(st.session_state.users_db)
            save_user_to_file(email, st.session_state.users_db[email])
            st.toast("📨 Preferences saved!", icon="✅")
            st.success("✅ Notification preferences updated!")

    with tabs[4]:
        st.markdown('<div class="sec-hd" style="font-size:13px;">📧 Email Digest Settings</div>',unsafe_allow_html=True)
        if not is_premium():
            st.markdown(f'<div class="card card-gold"><div style="font-size:13px;font-weight:700;color:{GOLD};margin-bottom:6px;">👑 Premium Feature</div><div style="font-size:13px;color:#374f6e;">Email digests require a Premium subscription. Upgrade to receive daily or weekly summaries of your watchlist signals and new BUY opportunities.</div></div>',unsafe_allow_html=True)
            if gold_btn("Upgrade to Enable Email Digest","set_digest_up"): nav("pricing")
        else:
            enabled=st.toggle("Enable email digest",value=st.session_state.get("email_digest_enabled",False))
            st.session_state.email_digest_enabled=enabled
            if enabled:
                freq=st.selectbox("Frequency",["Daily (7am ET)","Weekly (Monday 7am ET)","Real-time Alerts"])
                st.session_state.digest_frequency=freq
                st.text_input("Send to",value=email,disabled=True,label_visibility="visible")
                st.markdown('<div style="font-size:12px;color:#374f6e;margin-top:8px;line-height:1.7;">Digest includes: Top BUY signals from your watchlist · New composite category hits · Volume surge alerts · Squeeze setup notifications<br><span style="color:#2a3a52;">(Email delivery handled by alerts_worker.py)</span></div>',unsafe_allow_html=True)
                if st.button("Save Digest Settings",type="primary"):
                    st.session_state.users_db[email]["digest_prefs"] = {"frequency": freq, "enabled": True}
                    save_user_to_file(email, st.session_state.users_db[email])
                    st.success("✅ Digest preferences saved!")

    with tabs[5]:
        role = st.session_state.get("role","free")
        rl   = {"free":"Free","premium":"Premium Monthly","admin":"Admin","owner":"Owner"}.get(role,"Free")
        rc_  = {"free":"#6b7fa0","premium":"#a78bfa","admin":"#a5b4fc","owner":GOLD}.get(role,"#6b7fa0")
        plan_detail = db_user.get("plan","Free")

        st.markdown(f"""<div class="card card-blue" style="margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                <div>
                    <div style="font-size:15px;font-weight:800;color:#e2e8f0;">
                        Current Plan: <span style="color:{rc_};">{rl}</span>
                    </div>
                    <div style="font-size:12px;color:#374f6e;margin-top:4px;">Member since {db_user.get('joined','N/A')}</div>
                    <div style="font-size:12px;color:#374f6e;">Billing: {plan_detail}</div>
                </div>
                {'<span style="background:rgba(34,197,94,0.12);color:#4ade80;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;border:1px solid rgba(34,197,94,0.3);">ACTIVE</span>' if is_premium() else '<span style="background:rgba(100,116,139,0.12);color:#64748b;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;border:1px solid rgba(100,116,139,0.3);">FREE PLAN</span>'}
            </div>
        </div>""", unsafe_allow_html=True)

        if not is_premium():
            st.markdown('<div style="font-size:12px;color:#374f6e;margin-bottom:10px;">Upgrade to unlock all 23 composite categories, advanced Brief filters, signal charts, and more.</div>',unsafe_allow_html=True)
            uc1,uc2=st.columns(2,gap="small")
            with uc1:
                if gold_btn("👑 Upgrade to Premium — $19/mo","set_prem_mo"):
                    if stripe_configured():
                        url,err=create_checkout_session("premium",st.session_state.user["email"])
                        if url: st.session_state["_redirect_url"]=url; st.rerun()
                        else: st.error(err)
                    else: nav("pricing")
            with uc2:
                if st.button("👑 Get Annual — $149/yr (Save 35%)",key="set_prem_yr",use_container_width=True):
                    if stripe_configured():
                        url,err=create_checkout_session("annual",st.session_state.user["email"])
                        if url: st.session_state["_redirect_url"]=url; st.rerun()
                        else: st.error(err)
                    else: nav("pricing")
        else:
            st.markdown(f'<div style="font-size:13px;font-weight:600;color:#e2e8f0;margin-bottom:10px;">Manage Your Subscription</div>',unsafe_allow_html=True)
            if stripe_configured():
                if st.button("🔗 Open Billing Portal →", key="set_portal", type="primary", use_container_width=False):
                    url, err = create_portal_session(st.session_state.user["email"])
                    if url:
                        st.session_state["_portal_url"] = url
                    else:
                        st.session_state.pop("_portal_url", None)
                        st.error(err)
                # A real link button — the old window.open() ran on a rerun (not a direct
                # user gesture), so popup blockers silently killed it and "nothing happened".
                if st.session_state.get("_portal_url"):
                    st.link_button("→ Continue to Stripe Billing Portal", st.session_state["_portal_url"],
                                   type="primary", use_container_width=False)
                    st.caption("Opens Stripe's secure portal. The link is valid for a few minutes.")
                st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-top:8px;line-height:1.7;">The billing portal lets you: update payment method · view invoices · cancel subscription · download receipts</div>',unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="background:#0e1421;border:1px solid {BORDER};border-radius:7px;padding:12px 14px;font-size:12px;color:#374f6e;">To manage your subscription, email <span style="color:#a5b4fc;font-weight:600;">support@scanviction.com</span></div>',unsafe_allow_html=True)

        if is_premium():
            st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)
            st.markdown('<div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">Subscription Details</div>',unsafe_allow_html=True)
            sc1,sc2,sc3=st.columns(3)
            sc1.markdown(f'<div class="stat"><div class="stat-v" style="font-size:14px;color:#a78bfa;">{rl}</div><div class="stat-l">Current Plan</div></div>',unsafe_allow_html=True)
            sc2.markdown(f'<div class="stat"><div class="stat-v" style="font-size:14px;color:{GREEN};">Active</div><div class="stat-l">Status</div></div>',unsafe_allow_html=True)
            sc3.markdown(f'<div class="stat"><div class="stat-v" style="font-size:14px;color:#e2e8f0;">{plan_detail}</div><div class="stat-l">Billing Cycle</div></div>',unsafe_allow_html=True)

    # ── System / Data Health (admin only) ──
    if _show_system:
        with tabs[6]:
            st.markdown('<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">🩺 Live Data Health</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:12px;color:#6b7fa0;margin-bottom:14px;">Real-time status of external data sources. Green means live data is flowing; '
                        'red means the app is serving fallbacks. Use this to confirm market data works on your deployment.</div>', unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:12px;color:#94a3b8;margin-bottom:12px;">Storage backend: '
                        f'<span style="font-weight:700;color:{GREEN if storage_backend()=="postgres" else GOLD};">{storage_backend().upper()}</span></div>', unsafe_allow_html=True)

            # ── Twelve Data daily credit budget tracker (only when actually configured) ──
            # NOTE the distinction this panel kept blurring: Twelve Data is an OPTIONAL
            # per-ticker MARKET-DATA top-up; Postgres (above) is where CUSTOMER data —
            # accounts, alerts, sessions, signal history — is stored. They're unrelated.
            if get_td_key():
                _td = td_usage_today()
                _pct = (_td["used"] / max(1, _td["budget"])) * 100
                _bar_color = GREEN if _pct < 60 else (GOLD if _pct < 90 else RED)
                st.markdown(f'<div style="background:#0d1525;border:1px solid {BORDER};border-radius:10px;'
                            f'padding:12px 16px;margin-bottom:14px;">'
                            f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">'
                            f'<span style="font-size:13px;font-weight:700;color:#e2e8f0;">Twelve Data credits today</span>'
                            f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:12px;color:#94a3b8;">{_td["used"]} / {_td["budget"]} · {_td["remaining"]} left</span>'
                            f'</div>'
                            f'<div style="height:6px;background:#06090f;border-radius:3px;overflow:hidden;">'
                            f'<div style="height:100%;width:{min(100,_pct):.1f}%;background:{_bar_color};"></div>'
                            f'</div></div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.08);border-radius:10px;'
                            'padding:10px 16px;margin-bottom:14px;font-size:12px;color:#6b7fa0;">'
                            'Twelve Data: <b>not configured</b> — optional per-ticker quote top-up '
                            '(market data runs fully on Polygon without it). Customer/account data is unrelated: '
                            'it lives in the storage backend shown above.</div>', unsafe_allow_html=True)
            # ── Persistence durability warning ──
            _is_db = storage_backend() == "postgres"
            _ephemeral = (not _is_db) and ("/tmp" in USERS_DB_PATH)
            if _is_db:
                st.markdown('<div style="background:#0d1525;border:1px solid rgba(34,197,94,0.25);border-radius:10px;'
                            'padding:12px 16px;margin-bottom:14px;font-size:12px;color:#4ade80;">✅ <b>Durable storage active.</b> '
                            'Accounts and data persist across restarts (Postgres via DATABASE_URL).</div>', unsafe_allow_html=True)
            elif _ephemeral:
                st.markdown('<div style="background:#1a0000;border:1px solid rgba(239,68,68,0.35);border-radius:10px;'
                            'padding:12px 16px;margin-bottom:14px;font-size:12px;color:#f87171;">⚠️ <b>Ephemeral storage.</b> '
                            'Data is in <code>/tmp</code>, which most hosts WIPE on restart — registered accounts will not survive a reboot. '
                            'Set <code>DATABASE_URL</code> (Postgres) for durable persistence, or <code>MSP_DATA_DIR</code> to a persistent path.</div>', unsafe_allow_html=True)
            else:
                # Show the exact resolved path, whether it's writable, and how
                # many users are currently persisted — so it's diagnosable.
                _wok = False
                try:
                    _tf = _os.path.join(_DEFAULT_DATA_DIR, ".writetest")
                    with open(_tf, "w") as _fh: _fh.write("ok")
                    _os.remove(_tf); _wok = True
                except Exception:
                    _wok = False
                try:
                    _nusers = len(load_all_users_from_file())
                except Exception:
                    _nusers = 0
                st.markdown(f'<div style="background:#1a1400;border:1px solid rgba(245,158,11,0.3);border-radius:10px;'
                            f'padding:12px 16px;margin-bottom:8px;font-size:12px;color:{GOLD};">⚠️ <b>File storage.</b> '
                            f'Data is saved to:<br><code style="color:#e2e8f0;word-break:break-all;">{USERS_DB_PATH}</code><br>'
                            f'Writable: <b style="color:{GREEN if _wok else RED};">{"yes" if _wok else "NO"}</b> · '
                            f'Users on disk: <b style="color:#e2e8f0;">{_nusers}</b><br><br>'
                            f'This survives a normal restart only if your host preserves that path. '
                            f'<b>On Streamlit Community Cloud and most PaaS hosts it does NOT survive a redeploy/reboot.</b> '
                            f'For guaranteed persistence, set <code>DATABASE_URL</code> (free Postgres from Supabase or Neon).</div>', unsafe_allow_html=True)
            health = data_health_snapshot()
            if not health:
                st.info("No data fetches have run yet this session. Open Discover or the dashboard to trigger live fetches, then return here.")
            else:
                for src, h in sorted(health.items()):
                    total = h["ok"] + h["fail"]
                    rate = round(h["ok"] / total * 100, 1) if total else 0
                    live = h["last_ok"] and (time.time() - h["last_ok"] < 600)
                    dot = GREEN if (live and rate >= 50) else (GOLD if rate >= 20 else RED)
                    last_ok = _humanize_age(h["last_ok"]) if h.get("last_ok") else "never"
                    err = f' · last error: {h["last_err"]}' if h.get("last_err") and rate < 100 else ""
                    st.markdown(
                        f'<div style="background:#0d1525;border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:12px 16px;margin-bottom:6px;">'
                        f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">'
                        f'<span style="font-size:13px;font-weight:700;color:#e2e8f0;"><span style="color:{dot};">●</span> {src}</span>'
                        f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:12px;color:#94a3b8;">{h["ok"]} ok · {h["fail"]} fail · {rate}% · last ok {last_ok} · {h.get("last_ms",0)}ms</span>'
                        f'</div>'
                        f'{("<div style=font-size:11px;color:#f87171;margin-top:4px;>"+err+"</div>") if err else ""}'
                        f'</div>', unsafe_allow_html=True)
            # ── Inline diagnostic: fetch one quote synchronously and show the
            #    full result/error inline. This bypasses the worker entirely so
            #    you can SEE whether Twelve Data / yfinance work right now from
            #    this Streamlit Cloud process. If the worker has been silently
            #    crashing, this still works and surfaces the actual error.
            _dc1, _dc2 = st.columns(2, gap="small")
            with _dc1:
                _refresh_clicked = st.button("🔄 Refresh data now", key="health_refresh", use_container_width=True)
            with _dc2:
                _diag_clicked = st.button("🩺 Test data fetch (live)", key="health_diag", use_container_width=True)
            if _refresh_clicked:
                try: _refresh_universe_now()
                except Exception: pass
                st.rerun()
            if _diag_clicked:
                import traceback as _tb
                st.markdown('<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-top:14px;">Running direct quote fetch for AAPL…</div>', unsafe_allow_html=True)
                _diag_box = st.empty()
                try:
                    _t0 = time.time()
                    _q = _raw_quote("AAPL")
                    _dt = int((time.time()-_t0)*1000)
                    if _q:
                        _diag_box.success(f"✅ Got quote in {_dt}ms: AAPL @ ${_q.get('price',0):.2f} ({_q.get('pct',0):+.2f}%) · name='{_q.get('name','?')}'")
                    else:
                        _diag_box.error(f"❌ _raw_quote returned None after {_dt}ms. Check Data Health cards below for the per-source error.")
                except Exception as _e:
                    _diag_box.error(f"❌ Exception during fetch: {type(_e).__name__}: {_e}")
                    st.code(_tb.format_exc())
                # Also try Twelve Data direct (bypasses _raw_quote logic) so we
                # know if your TD key works at all from this host.
                try:
                    _tdk = ""
                    try: _tdk = st.secrets.get("TWELVE_DATA_API_KEY","") or ""
                    except: pass
                    if _tdk:
                        import requests as _rq
                        _t1 = time.time()
                        _r = _rq.get(f"https://api.twelvedata.com/quote?symbol=AAPL&apikey={_tdk}", timeout=8)
                        _dt2 = int((time.time()-_t1)*1000)
                        _body = _r.text[:240]
                        if _r.status_code == 200 and '"close"' in _body:
                            st.success(f"✅ Twelve Data API reachable ({_dt2}ms, HTTP {_r.status_code})")
                        else:
                            st.error(f"❌ Twelve Data API HTTP {_r.status_code} after {_dt2}ms: {_body}")
                    else:
                        st.warning("⚠️ No TWELVE_DATA_API_KEY found in secrets — set it for reliable quotes on Streamlit Cloud.")
                except Exception as _e2:
                    st.error(f"❌ Twelve Data direct call failed: {_e2}")
                # Intentionally NO st.rerun() here — we want the result visible.

    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# PAGE: ADMIN
# ─────────────────────────────────────────────────────────────
def page_admin():
    if not is_admin(): st.error("Access denied."); return
    render_topbar("admin")
    back_button("ad_back")
    st.markdown('<div class="page-wrap">' ,unsafe_allow_html=True)
    st.markdown('<div class="sec-hd">🛠️ Admin Panel</div>',unsafe_allow_html=True)

    # Admin role badge
    cur_role = st.session_state.get("role","admin")
    role_colors = {"owner":GOLD,"admin":"#a5b4fc"}
    st.markdown(f"""<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div style="font-size:22px;font-weight:800;color:#e2e8f0;">🛠️ Admin Panel</div>
        <div style="background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;
                    padding:6px 14px;font-size:12px;font-weight:700;color:{role_colors.get(cur_role,'#a5b4fc')};">
            {cur_role.title()} Access
        </div>
    </div>""", unsafe_allow_html=True)
    # Security checklist
    st.markdown(f"""<div class="card" style="border-left:3px solid {RED};margin-bottom:16px;">
        <div style="font-size:13px;font-weight:700;color:#f87171;margin-bottom:6px;">🔒 Production Security Checklist</div>
        <div style="font-size:12px;color:#374f6e;line-height:1.9;">
        ✅ Keep <code style="background:#1a0000;color:#f87171;padding:1px 5px;border-radius:3px;">secrets.toml</code> in Render → Environment → Secret Files (POLYGON_API_KEY, DATABASE_URL, Stripe, Resend, Telegram, Anthropic…)<br>
        ✅ Set <code style="background:#1a0000;color:#f87171;padding:1px 5px;border-radius:3px;">owner_email</code> and <code style="background:#1a0000;color:#f87171;padding:1px 5px;border-radius:3px;">owner_pw_hash</code> in that file<br>
        ✅ Never commit real passwords or keys to GitHub<br>
        ✅ Change demo account passwords before public launch
        </div></div>""",unsafe_allow_html=True)

    tabs=st.tabs(["📊 Overview","👥 Users","🔑 API & Secrets","📈 Analytics","🔧 Site Config","📉 Signal Engine"])

    with tabs[0]:
        # REAL numbers only (the old hardcoded 1847/312/634 demo stats misled the owner).
        _udb = st.session_state.users_db or {}
        _total_accounts = len(_udb)
        _premium_ct = sum(1 for u in _udb.values() if u.get("role") == "premium")
        _paid_ct = sum(1 for u in _udb.values() if u.get("role") in ("premium", "admin", "owner"))
        # Signups verified from the real funnel; fall back to account count.
        try:
            _fc, _ = read_funnel_events()
            _signups = (_fc.get("signup_verified", {}) or {}).get("n") or _total_accounts
        except Exception:
            _signups = _total_accounts
        # DAU: unique users with any tracked event in the last 24h (Postgres); "—" without a DB.
        try:
            _dau = analytics_store.daily_active(24)
        except Exception:
            _dau = None
        _dau_disp = _dau if _dau is not None else "—"
        _conv = (_premium_ct / _total_accounts * 100.0) if _total_accounts else 0.0
        oc=st.columns(5)
        for col,(v,l,c) in zip(oc,[(_signups,"Signups","#a5b4fc"),(_premium_ct,"Premium","#a78bfa"),(_dau_disp,"Daily Active (24h)",GREEN),(f"{_conv:.1f}%","Conversion",GOLD),(_total_accounts,"Total Accounts","#94a3b8")]):
            col.markdown(f'<div class="stat"><div class="stat-v" style="color:{c};">{v}</div><div class="stat-l">{l}</div></div>',unsafe_allow_html=True)
        st.markdown("<br>",unsafe_allow_html=True)
        # Data-source tiles reflect the REAL current stack (Yahoo/StockTwits are gone).
        _health = data_health_snapshot()
        def _src_status(hkey, key_ok=True, keyless=False):
            h = _health.get(hkey) or {}
            if not key_ok:
                return "⚠️ Not Set", GOLD
            if h.get("last_err") and not h.get("last_ok"):
                return "❌ Erroring", RED
            if keyless or h.get("last_ok") or key_ok:
                return "✅ Active", GREEN
            return "— Idle", "#94a3b8"
        _poly_ok = bool(get_polygon_key())
        _td_ok = bool(get_td_key())
        _tiles = [
            ("Polygon.io", *_src_status("polygon", key_ok=_poly_ok), "Primary market data · whole-market scan"),
            ("SEC EDGAR", "✅ Active", GREEN, "Free · insider Form 4 + 8-K filings"),
            ("FINRA Short Interest", *_src_status("polygon", key_ok=_poly_ok), "Via Polygon · squeeze fuel"),
            ("ApeWisdom", "✅ Active", GREEN, "Free · social buzz volume"),
            ("Twelve Data", *_src_status("twelvedata", key_ok=_td_ok), "Optional · per-ticker quote top-up"),
        ]
        hc=st.columns(5)
        for col,(name,status,c_,note) in zip(hc,_tiles):
            col.markdown(f'<div class="card"><div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:4px;">{name}</div><div style="font-size:12px;font-weight:700;color:{c_};">{status}</div><div style="font-size:11px;color:#2a3a52;margin-top:3px;">{note}</div></div>',unsafe_allow_html=True)

    with tabs[1]:
        for email,u in list(st.session_state.users_db.items()):
            uc1,uc2,uc3,uc4=st.columns([3,1,2,1])
            with uc1:
                v_icon="✅" if u.get("verified") else "⚠️"
                st.markdown(f'<div style="padding:8px 0;"><div style="font-size:13px;font-weight:600;color:#e2e8f0;">{_esc(u["name"])}</div><div style="font-size:11px;color:#2a3a52;">{v_icon} {_esc(email)}</div></div>',unsafe_allow_html=True)
            with uc2:
                rc_={"owner":GOLD,"admin":"#a5b4fc","premium":"#a78bfa","free":"#4a5e7a"}.get(u["role"],"#4a5e7a")
                st.markdown(f'<div style="padding:10px 0;"><span style="font-size:10px;font-weight:700;color:{rc_};">{u["role"].upper()}</span></div>',unsafe_allow_html=True)
            with uc3:
                if is_owner() and u["role"]!="owner":
                    nr=st.selectbox("",["free","premium","admin"],index=["free","premium","admin"].index(u["role"]) if u["role"] in ["free","premium","admin"] else 0,key=f"role_{email}",label_visibility="collapsed")
                    if st.button("Update",key=f"upd_{email}",use_container_width=True):
                        # PERSIST the change — previously this only touched session_state, so
                        # the upgrade/downgrade silently reverted on the next restart and was
                        # never visible to the alert worker (which reads the shared store).
                        st.session_state.users_db[email]["role"]=nr
                        _save_global_db(st.session_state.users_db)
                        save_user_to_file(email, st.session_state.users_db[email])
                        st.toast(f"✅ {email} → {nr}", icon="👑")
                        st.rerun()
            with uc4:
                if is_owner() and email!=st.session_state.user.get("email",""):
                    if st.button("🗑",key=f"del_{email}",use_container_width=True):
                        del st.session_state.users_db[email]
                        _save_global_db(st.session_state.users_db)   # persist the deletion
                        st.rerun()
            st.markdown(f'<div style="border-bottom:1px solid rgba(255,255,255,.04);margin-bottom:4px;"></div>',unsafe_allow_html=True)

    with tabs[2]:
        st.markdown(f"""<div class="card card-blue">
            <div style="font-size:13px;font-weight:700;color:#a5b4fc;margin-bottom:8px;">Render Secrets Setup</div>
            <div style="font-size:12px;color:#374f6e;line-height:1.9;">Render dashboard → your service → <strong style="color:#e2e8f0;">Environment → Secret Files</strong> → edit the file named <code style="background:#060a12;color:#a5b4fc;padding:2px 6px;border-radius:3px;">secrets.toml</code> (mounted to .streamlit/secrets.toml on boot). Core keys:</div>
            <pre style="background:#060a12;border:1px solid {BORDER};border-radius:7px;padding:12px;font-size:11px;color:#4ade80;margin-top:10px;overflow-x:auto;">POLYGON_API_KEY = "…"        # market data (required)\nDATABASE_URL = "postgres://…" # durable accounts/signals (required)\nSTRIPE_SECRET_KEY = "…"       # billing\nRESEND_API_KEY = "…"          # email\nTELEGRAM_BOT_TOKEN = "…"      # alerts\nANTHROPIC_API_KEY = "…"       # AI support chat\nTWELVE_DATA_API_KEY = "…"     # optional quote top-up\nowner_email = "your@email.com"\nowner_pw_hash = "sha256_or_bcrypt_hash"</pre>
            <div style="font-size:11px;color:#374f6e;margin-top:8px;">Generate hash: <code style="background:#060a12;color:#a5b4fc;padding:2px 6px;border-radius:3px;">python3 -c "import hashlib; print(hashlib.sha256(b'YourPassword').hexdigest())"</code> · Changes to the secret file require a service restart.</div>
        </div>""",unsafe_allow_html=True)

        # ── Data-source panel: what's ALREADY integrated vs what's worth adding ──
        st.markdown('<div class="sec-hd" style="font-size:13px;margin-top:16px;">✅ Integrated Now (all free / already paid)</div>',unsafe_allow_html=True)
        api_have=[
            ("Polygon.io","PAID (Starter) — whole-market OHLCV, snapshots, FINRA short interest + short volume, TTM EPS. The scan's backbone.",GREEN),
            ("SEC EDGAR","FREE, keyless — insider Form-4 open-market buys + fresh 8-K catalysts. Powers Insider Cluster & 8-K alerts.",GREEN),
            ("FRED","FREE — VIX, credit spreads, yield curve → the Risk-On/Off market-regime backdrop on Discover.",GOLD),
            ("ApeWisdom","FREE, keyless — Reddit/StockTwits buzz volume + trend for Social Buzz & Squeeze categories.","#818cf8"),
            ("News + VADER","FREE — headline sentiment direction (keyless Yahoo-news fallback; Finnhub-powered when a key is set).","#f97316"),
        ]
        cols=st.columns(3,gap="small")
        for i,(name,desc,color) in enumerate(api_have):
            with cols[i%3]:
                st.markdown(f'<div class="card" style="border-left:3px solid {color};min-height:90px;"><div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:4px;">{name}</div><div style="font-size:11px;color:#374f6e;line-height:1.6;">{desc}</div></div>',unsafe_allow_html=True)
        st.markdown('<div class="sec-hd" style="font-size:13px;margin-top:16px;">➕ Worth Adding — usable free tiers</div>',unsafe_allow_html=True)
        api_recs=[
            ("Finnhub (free key)","FREE ~60 calls/min. Add FINNHUB_API_KEY to Secrets and the app AUTOMATICALLY upgrades news-direction sentiment (already wired). Also has an earnings calendar worth integrating.",GREEN),
            ("Alpha Vantage","FREE but only ~25 requests/day now — enough for a once-daily earnings-calendar or macro pull, not per-ticker work.",BLUE),
            ("Twelve Data","FREE 800 credits/day — already wired as an optional per-ticker quote top-up (TWELVE_DATA_API_KEY).",GOLD),
            ("Unusual Whales","PAID — options flow / dark-pool prints. Strong premium differentiator later; no usable free tier.","#a78bfa"),
            ("Benzinga","PAID (enterprise pricing) — news/earnings headlines. Skip until revenue justifies it.","#f97316"),
            ("Finviz Elite","PAID (~$40/mo) — screener/breadth export. The scan already covers most of this in-house.","#818cf8"),
        ]
        cols=st.columns(3,gap="small")
        for i,(name,desc,color) in enumerate(api_recs):
            with cols[i%3]:
                st.markdown(f'<div class="card" style="border-left:3px solid {color};min-height:90px;"><div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:4px;">{name}</div><div style="font-size:11px;color:#374f6e;line-height:1.6;">{desc}</div></div>',unsafe_allow_html=True)

        if is_admin():
            st.markdown('<div class="sec-hd" style="font-size:13px;margin-top:16px;">Session API Key Override</div>',unsafe_allow_html=True)
            with st.form("api_f"):
                nk=st.text_input("Twelve Data API Key",type="password",placeholder="Session only — use Secrets for production",label_visibility="visible")
                if st.form_submit_button("Save for Session",type="primary"):
                    st.session_state._admin_td_key=nk; st.success("✅ Session key saved.")
            if st.button("Clear Key",key="clr_api"): st.session_state._admin_td_key=""; st.success("Cleared.")

    with tabs[3]:
        st.markdown('<div class="sec-hd" style="font-size:13px;">🎯 Acquisition Funnel (real)</div>',unsafe_allow_html=True)
        _fd, _frecent = read_funnel_events()
        if not _fd:
            st.markdown('<div class="disc">No events logged yet. The funnel fills in as visitors hit the site — landing views, sign-ups, verifications, logins, and payments are recorded to <code>.msp_data/events.jsonl</code>.</div>',unsafe_allow_html=True)
        else:
            _steps=[("landing_view","Landing views","#818cf8"),
                    ("signup_started","Sign-ups started","#a5b4fc"),
                    ("signup_verified","Sign-ups verified","#34d399"),
                    ("login_success","Logins","#22c55e"),
                    ("payment_success","Payments","#f59e0b")]
            _maxn=max((_fd.get(k,{}).get("n",0) for k,_,_ in _steps), default=0) or 1
            _bars=""
            for k,label,col in _steps:
                n=_fd.get(k,{}).get("n",0); u=_fd.get(k,{}).get("u",0)
                w=max(2,int(n/_maxn*100))
                _bars+=(f'<div style="margin:7px 0;"><div style="display:flex;justify-content:space-between;font-size:12px;color:#94a3b8;margin-bottom:3px;">'
                        f'<span>{label}</span><span style="font-family:JetBrains Mono,monospace;color:#e2e8f0;">{n:,}<span style="color:#5d6b86;"> · {u:,} uniq</span></span></div>'
                        f'<div style="background:rgba(255,255,255,.05);border-radius:4px;height:10px;"><div style="width:{w}%;height:10px;border-radius:4px;background:{col};"></div></div></div>')
            st.markdown(f'<div class="card">{_bars}</div>',unsafe_allow_html=True)
            _lv=_fd.get("landing_view",{}).get("n",0); _sv=_fd.get("signup_verified",{}).get("n",0); _pay=_fd.get("payment_success",{}).get("n",0)
            c1,c2,c3=st.columns(3)
            c1.metric("Visitor → Verified", f"{(_sv/_lv*100):.1f}%" if _lv else "—")
            c2.metric("Verified → Paid", f"{(_pay/_sv*100):.1f}%" if _sv else "—")
            c3.metric("Total payments", f"{_pay:,}")
            _src = ("✅ Durable <strong>Postgres</strong> — events survive Cloud reboots." if analytics_store.enabled()
                    else "⚠️ <code>events.jsonl</code> on this instance (ephemeral — resets on reboot). Set <code>DATABASE_URL</code> for durable analytics.")
            st.markdown(f'<div class="disc" style="margin-top:10px;">Source: {_src}</div>',unsafe_allow_html=True)
            with st.expander("Recent events"):
                for e in _frecent[:25]:
                    _extra = " · "+_esc(str(e.get("plan"))) if e.get("plan") else ""
                    st.markdown(f'<div style="font-size:11px;color:#6b7fa0;font-family:JetBrains Mono,monospace;">{_esc(e.get("ts",""))} · {_esc(e.get("event",""))}{_extra}</div>',unsafe_allow_html=True)

    with tabs[4]:
        st.markdown('<div class="sec-hd" style="font-size:13px;">Ranking & Display Controls</div>',unsafe_allow_html=True)
        rc1,rc2=st.columns(2,gap="small")
        with rc1:
            sort_by=st.selectbox("Default sort order",["SW Score","% Change Today","Volume Ratio","Short Float","Social Sentiment"],key="ranking_sort_ctrl")
            st.session_state.ranking_sort=sort_by
        with rc2:
            filter_by=st.selectbox("Default category filter",["All","Free Only","Premium Only","Composite Only"],key="ranking_filter_ctrl")
            st.session_state.ranking_filter=filter_by
        if st.button("Save Ranking Controls",type="primary"): st.success("✅ Saved!")
        st.markdown('<div class="sec-hd" style="font-size:13px;margin-top:16px;">💳 Stripe Billing Configuration</div>',unsafe_allow_html=True)

        # Live status
        stripe_ok = stripe_configured()
        scolor = GREEN if stripe_ok else GOLD
        sstatus = "✅ Stripe Connected" if stripe_ok else "⚠️ Stripe Not Configured"
        st.markdown(f'<div style="background:{"#04200d" if stripe_ok else "#1a1000"};border:1px solid {"rgba(34,197,94,0.3)" if stripe_ok else "rgba(245,158,11,0.3)"};border-radius:8px;padding:10px 14px;font-size:13px;font-weight:700;color:{scolor};margin-bottom:16px;">{sstatus}</div>',unsafe_allow_html=True)

        st.markdown(f"""<div class="card">
            <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">Step-by-Step Stripe Setup</div>
            <div style="font-size:12px;color:#374f6e;line-height:2.0;">
            <strong style="color:#e2e8f0;">1. Create a Stripe account</strong> at <a href="https://stripe.com" target="_blank" style="color:#a5b4fc;">stripe.com</a><br>
            <strong style="color:#e2e8f0;">2. Create Products & Prices</strong> in Stripe Dashboard → Products:<br>
            &nbsp;&nbsp;&nbsp;&nbsp;• Scanviction Premium Monthly → Recurring $19/mo → copy Price ID<br>
            &nbsp;&nbsp;&nbsp;&nbsp;• Scanviction Annual Plan → Recurring $149/yr → copy Price ID<br>
            <strong style="color:#e2e8f0;">3. Get your Secret Key</strong> from Stripe Dashboard → Developers → API Keys<br>
            <strong style="color:#e2e8f0;">4. Add to Streamlit Secrets</strong> (Settings → Secrets in your app dashboard):<br>
            </div>
            <pre style="background:#060a12;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px;font-size:11px;color:#4ade80;margin:10px 0;overflow-x:auto;">STRIPE_SECRET_KEY = "sk_live_..."
STRIPE_PRICE_MONTHLY = "price_xxx"
STRIPE_PRICE_ANNUAL  = "price_yyy"
APP_URL = "https://your-app.streamlit.app"</pre>
            <div style="font-size:12px;color:#374f6e;line-height:2.0;">
            <strong style="color:#e2e8f0;">5. Customer Portal</strong> — Enable in Stripe Dashboard → Billing → Customer Portal<br>
            <strong style="color:#e2e8f0;">6. Test Mode</strong> — Use <code style="background:#0e1421;color:#a5b4fc;padding:1px 5px;border-radius:3px;">sk_test_...</code> keys first, then switch to live<br>
            <strong style="color:#e2e8f0;">7. Test card</strong>: <code style="background:#0e1421;color:#a5b4fc;padding:1px 5px;border-radius:3px;">4242 4242 4242 4242</code> · any future exp · any CVC<br>
            </div>
        </div>""",unsafe_allow_html=True)

        st.markdown(f"""<div class="card card-blue" style="margin-top:8px;">
            <div style="font-size:12px;font-weight:700;color:#a5b4fc;margin-bottom:6px;">⚠️ Webhook Note for Streamlit</div>
            <div style="font-size:12px;color:#374f6e;line-height:1.8;">
            Streamlit Community Cloud can't receive webhooks directly. Scanviction uses <strong style="color:#e2e8f0;">Checkout Session verification</strong> on the success redirect URL instead. This handles new subscriptions reliably.<br>
            For subscription renewals, cancellations, and failed payments in production, you have two options:<br>
            • <strong style="color:#e2e8f0;">Option A</strong>: Add a lightweight webhook endpoint (Flask/FastAPI on Render.com, free tier) that updates a shared DB<br>
            • <strong style="color:#e2e8f0;">Option B</strong>: Use Stripe's <code style="background:#0e1421;color:#a5b4fc;">payment_behavior: allow_incomplete</code> + manual user verification via the Users tab<br>
            For MVP/early-stage, Option B is fine. Upgrade to Option A when you have 50+ paying subscribers.
            </div>
        </div>""",unsafe_allow_html=True)

        if stripe_ok and is_owner():
            st.markdown('<div class="sec-hd" style="font-size:12px;margin-top:16px;">Quick Actions</div>',unsafe_allow_html=True)
            qa1,qa2=st.columns(2,gap="small")
            with qa1:
                manual_email=st.text_input("User email to upgrade",placeholder="user@email.com",key="admin_upgrade_email",label_visibility="visible")
                manual_plan=st.selectbox("Plan",["premium","annual"],key="admin_upgrade_plan",label_visibility="visible")
                if st.button("↑ Manually Upgrade User",key="admin_do_upgrade",type="primary",use_container_width=True):
                    if manual_email and manual_email in st.session_state.users_db:
                        st.session_state.users_db[manual_email]["role"]="premium"
                        st.session_state.users_db[manual_email]["plan"]="Monthly" if manual_plan=="premium" else "Annual"
                        st.success(f"✅ {manual_email} upgraded to {manual_plan}")
                    elif manual_email: st.error("User not found")
            with qa2:
                downgrade_email=st.text_input("User email to downgrade",placeholder="user@email.com",key="admin_downgrade_email",label_visibility="visible")
                if st.button("↓ Downgrade to Free",key="admin_do_downgrade",use_container_width=True):
                    if downgrade_email and downgrade_email in st.session_state.users_db:
                        st.session_state.users_db[downgrade_email]["role"]="free"
                        st.session_state.users_db[downgrade_email]["plan"]="Free"
                        st.success(f"✅ {downgrade_email} downgraded")
                    elif downgrade_email: st.error("User not found")

    # ── Tab 5: Signal Engine ──
    with tabs[5]:
        st.markdown(f'<div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:12px;">📉 Signal Engine Health & Performance</div>',unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:14px;">Monitor the proprietary signal tracking system. View aggregate performance, recent triggers, and signal health.</div>',unsafe_allow_html=True)

        # Get stats from signal engine
        try:
            all_events = get_recent_signal_events(limit=500)
            perf_stats = get_category_performance_stats()
        except Exception:
            all_events = []
            perf_stats = {}

        # KPI strip
        total_signals = len(all_events)
        resolved = [e for e in all_events if e.get("outcomes",{}).get("label","pending") != "pending"]
        wins = [e for e in resolved if e.get("outcomes",{}).get("label") == "success"]
        win_rate = round(len(wins)/len(resolved)*100, 1) if resolved else 0
        avg_5d = round(sum(e["outcomes"].get("5d_pct",0) or 0 for e in resolved)/max(1,len(resolved)), 2)
        pending = [e for e in all_events if e.get("outcomes",{}).get("label","pending") == "pending"]
        unique_tickers = len(set(e.get("ticker") for e in all_events))

        adm_cols = st.columns(5)
        adm_kpis = [
            (total_signals, "Total Signals", "#818cf8"),
            (f"{win_rate}%", "Win Rate", "#4ade80" if win_rate >= 55 else "#fbbf24"),
            (f"+{avg_5d}%" if avg_5d >= 0 else f"{avg_5d}%", "Avg 5-Day", "#4ade80" if avg_5d >= 0 else "#f87171"),
            (len(pending), "Pending", "#fbbf24"),
            (unique_tickers, "Unique Tickers", "#a78bfa"),
        ]
        for col, (val, lbl, color) in zip(adm_cols, adm_kpis):
            col.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:14px;">
                <div style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:900;color:{color};">{val}</div>
                <div style="font-size:11px;color:#374f6e;margin-top:3px;">{lbl}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # Category performance table
        if perf_stats:
            st.markdown(f'<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">📊 Performance by Category</div>',unsafe_allow_html=True)
            for cat, stats in sorted(perf_stats.items(), key=lambda x: x[1].get("win_rate",0), reverse=True):
                wr = stats.get("win_rate", 0)
                wr_color = "#4ade80" if wr>=60 else "#fbbf24" if wr>=45 else "#f87171"
                st.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:8px;padding:10px 14px;margin-bottom:5px;display:flex;justify-content:space-between;align-items:center;">
                    <div>
                        <span style="font-size:13px;font-weight:700;color:#e2e8f0;">{cat}</span>
                        <span style="font-size:11px;color:#374f6e;margin-left:8px;">{stats.get("count",0)} signals · {stats.get("wins",0)}W/{stats.get("losses",0)}L</span>
                    </div>
                    <div style="display:flex;gap:14px;align-items:center;">
                        <span style="font-size:11px;color:#374f6e;">Win: <strong style="color:{wr_color};">{wr}%</strong></span>
                        <span style="font-size:11px;color:#374f6e;">5d Avg: <strong style="font-family:'JetBrains Mono',monospace;color:{'#4ade80' if (stats.get('avg_5d') or 0)>=0 else '#f87171'};">{('+' if (stats.get('avg_5d') or 0)>=0 else '')}{stats.get('avg_5d') or 0}%</strong></span>
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.info("No signal performance data yet. Signals are recorded as users browse composite categories.")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # Recent signals table
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">🕒 Recent Signal Events (Last 20)</div>',unsafe_allow_html=True)
        if all_events:
            recent = sorted(all_events, key=lambda x: x.get("triggered_at",""), reverse=True)[:20]
            for ev in recent:
                outs = ev.get("outcomes", {})
                curr_pct = outs.get("current_pct", 0) or 0
                label = outs.get("label", "pending")
                lc = "#4ade80" if label=="success" else "#f87171" if label=="failure" else "#fbbf24" if label=="pending" else "#94a3b8"
                trigger_dt = datetime.fromisoformat(ev.get("triggered_at", datetime.now().isoformat()))
                time_str = _humanize_age(trigger_dt.timestamp())   # unified age (was a bespoke reimpl)
                st.markdown(f"""<div style="background:#080b14;border:1px solid {BORDER};border-radius:6px;padding:8px 12px;margin-bottom:3px;font-size:11px;display:flex;justify-content:space-between;">
                    <div>
                        <span style="font-family:'JetBrains Mono',monospace;font-weight:700;color:#818cf8;">{ev.get("ticker","?")}</span>
                        <span style="color:#94a3b8;margin-left:6px;">{ev.get("category","")[:30]}</span>
                        <span style="color:#374f6e;margin-left:8px;">· {time_str}</span>
                    </div>
                    <div>
                        <span style="color:#374f6e;">Score: <strong style="color:#e2e8f0;">{ev.get("score_at_trigger",0)}</strong></span>
                        <span style="margin-left:10px;font-family:'JetBrains Mono',monospace;font-weight:700;color:{lc};">{'+' if curr_pct>=0 else ''}{curr_pct:.1f}%</span>
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.caption("No signal events recorded yet.")

        st.markdown('<div class="div-line"></div>',unsafe_allow_html=True)

        # Signal history maintenance tools
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">🛠️ Signal History Tools</div>',unsafe_allow_html=True)
        st.markdown('<div style="font-size:11px;color:#374f6e;margin-bottom:10px;">'
                    '<b style="color:#a5b4fc;">Refresh Outcomes</b> re-prices every tracked signal (fills the 1d/3d/5d/10d/20d forward returns, max upside/drawdown and the win/loss label — events refreshed in the last 2h are skipped). It also runs automatically each scan cycle. '
                    '<b style="color:#a5b4fc;">Purge Demo Data</b> removes the seeded example signals so the stats above reflect ONLY real tracked signals. '
                    '<b style="color:#a5b4fc;">Seed Demo</b> re-adds those examples (dev/demo use only).</div>', unsafe_allow_html=True)
        seed_c1, seed_c2, seed_c3 = st.columns(3)
        with seed_c1:
            if st.button("🔄 Refresh Outcomes Now", key="adm_refresh", use_container_width=True):
                try:
                    # Polygon is the licensed price source (the old yf_ohlcv/Yahoo path was
                    # removed — it silently returned nothing, which is why this button
                    # "didn't do anything"). Falls back to the app's per-ticker OHLCV.
                    def _price_fetch(t, days):
                        try:
                            if _poly is not None and _POLYGON_KEY_CAPTURED:
                                bars = _poly.daily_bars(_POLYGON_KEY_CAPTURED, t, days=max(int(days), 30))
                                if bars:
                                    return pd.DataFrame(bars).rename(
                                        columns={"o": "open", "h": "high", "l": "low",
                                                 "c": "close", "v": "volume", "date": "datetime"})
                        except Exception:
                            pass
                        try:
                            return _raw_ohlcv(t, max(int(days), 30))
                        except Exception:
                            return None
                    updated = update_signal_outcomes(_price_fetch)
                    st.success(f"✅ Outcomes refreshed across {len(updated)} tracked events (recently-updated ones skipped)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Refresh failed: {e}")
        with seed_c2:
            if st.button("🧹 Purge Demo Data", key="adm_purge_demo", use_container_width=True):
                try:
                    removed = purge_demo_signal_history()
                    st.success(f"✅ Removed {removed} demo event(s) — stats now show real signals only")
                    st.rerun()
                except Exception as e:
                    st.error(f"Purge failed: {e}")
        with seed_c3:
            if st.button("🌱 Seed Demo Signal History", key="adm_seed", use_container_width=True):
                try:
                    seed_demo_signal_history(_demo_price)
                    st.success("✅ Demo data seeded (tagged demo — purge anytime)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Seed failed: {e}")

    st.markdown('</div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# EMAIL VERIFICATION
# ─────────────────────────────────────────────────────────────
def _send_verification_email(email, code):
    """
    Send verification email. Requires RESEND_API_KEY or SENDGRID_API_KEY in Secrets.
    Falls back to simulated mode (shows code in UI) if not configured.
    Returns (True, None) or (False, error_msg).
    """
    # Try Resend
    try:
        resend_key = st.secrets.get("RESEND_API_KEY","")
        if resend_key:
            import requests as _r
            resp = _r.post("https://api.resend.com/emails",
                headers={"Authorization":f"Bearer {resend_key}","Content-Type":"application/json"},
                json={"from":st.secrets.get("EMAIL_FROM","Scanviction <support@scanviction.com>"),
                      "to":[email],
                      "subject":"Welcome to Scanviction — confirm your email",
                      "html":_verify_email_html(code)})
            if resp.status_code in (200,201): return True,None
            return False, f"Email send failed: {resp.text}"
    except: pass
    # Simulated — show code in UI
    return False, f"DEMO_CODE:{code}"

def page_verify_email():
    render_topbar()
    _,cc,_=st.columns([1,2,1])
    with cc:
        email = st.session_state.get("_verify_email","")
        st.markdown(f"""<div style="text-align:center;padding:40px 0 24px;">
            <div style="font-size:32px;margin-bottom:12px;">📧</div>
            <div style="font-size:24px;font-weight:800;color:#e2e8f0;margin-bottom:8px;">Check Your Email</div>
            <div style="font-size:13px;color:#374f6e;">We sent a 6-digit verification code to<br>
            <strong style="color:#a5b4fc;">{email}</strong></div>
        </div>""",unsafe_allow_html=True)

        # Show demo code if email not configured
        demo = st.session_state.get("_demo_code","")
        if demo:
            st.markdown(f'''<div style="background:#0d1525;border:1px solid rgba(99,102,241,0.3);border-radius:10px;padding:16px;margin-bottom:12px;">
                <div style="font-size:12px;font-weight:700;color:#818cf8;margin-bottom:6px;">📋 Demo Mode — Email Sending Not Configured</div>
                <div style="font-size:11px;color:#374f6e;margin-bottom:8px;">Add <code style="background:#060a12;color:#4ade80;padding:1px 5px;border-radius:3px;">RESEND_API_KEY</code> to Streamlit Secrets to enable real email verification.</div>
                <div style="font-size:14px;font-weight:700;color:#e2e8f0;">Your code: <span style="font-family:'JetBrains Mono',monospace;font-size:22px;color:#6366f1;letter-spacing:4px;">{demo}</span></div>
            </div>''', unsafe_allow_html=True)

        with st.form("vf"):
            code_in = st.text_input("Enter 6-digit code", placeholder="123456", max_chars=6)
            if st.form_submit_button("Verify Email →", type="primary", use_container_width=True):
                stored = st.session_state.get("_verify_code","")
                if code_in.strip() == stored:
                    uemail = st.session_state.get("_verify_email","")
                    if uemail in st.session_state.users_db:
                        st.session_state.users_db[uemail]["verified"] = True
                        _save_global_db(st.session_state.users_db)
                        save_user_to_file(uemail, st.session_state.users_db[uemail])
                    # Complete login
                    udata = st.session_state.get("_verify_user",{})
                    st.session_state.user = {"email":uemail,"name":udata.get("name","")}
                    st.session_state.role = "free"
                    for k in ["_verify_code","_verify_email","_verify_user","_demo_code"]:
                        st.session_state.pop(k,None)
                    st.session_state["_signup_success"] = udata.get("name","")
                    track_event("signup_verified", {"email": uemail})
                    st.success("✅ Email verified! Welcome to Scanviction.")
                    time.sleep(0.3)
                    nav("dashboard")
                else:
                    st.error("❌ Incorrect code. Please try again.")

        st.markdown("<br>",unsafe_allow_html=True)
        if st.button("Resend code", key="resend_v"):
            code = str(random.randint(100000,999999))
            st.session_state["_verify_code"] = code
            ok,info = _send_verification_email(email, code)
            if not ok and info and info.startswith("DEMO_CODE:"):
                st.session_state["_demo_code"] = info.split(":",1)[1]
                st.success("Code regenerated (demo mode — shown above)")
            elif ok: st.success("✅ New code sent!")
            else: st.error(f"Send failed: {info}")
        if st.button("← Back to Sign Up", key="v_back"):
            for k in ["_verify_code","_verify_email","_verify_user","_demo_code"]:
                st.session_state.pop(k,None)
            nav("signup")

# ─────────────────────────────────────────────────────────────
# PAGE: CONTACT
# ─────────────────────────────────────────────────────────────
def _lsec(n, title, *paras):
    ps = "".join(f'<p style="margin:8px 0 0;">{p}</p>' for p in paras)
    return f'<h2 style="font-size:16px;font-weight:700;color:#e2e8f0;margin:22px 0 4px;">{n}. {title}</h2>{ps}'

def _legal_shell(title, updated, body_html, key):
    render_topbar()
    st.markdown('<div class="page-wrap pw-narrow">', unsafe_allow_html=True)
    st.markdown(f"""<div style="max-width:820px;margin:0 auto;padding:16px 0 6px;">
      <h1 style="font-size:28px;font-weight:800;color:#e2e8f0;margin-bottom:2px;">{title}</h1>
      <div style="font-size:12px;color:#5d6b86;margin-bottom:14px;">Last updated: {updated}</div>
      <div style="background:#0e1421;border:1px solid rgba(245,158,11,0.2);border-radius:8px;padding:12px 14px;font-size:12px;color:#94a3b8;margin-bottom:6px;">
        Scanviction is an <strong>educational software and market-data tool</strong> — <strong>not</strong> investment,
        financial, legal, or tax advice. We are not a registered investment adviser or broker-dealer.
      </div>
      <div style="font-size:14px;color:#c5cfdd;line-height:1.7;">{body_html}</div>
    </div>""", unsafe_allow_html=True)
    if st.button("← Back to Home", key=key):
        nav("landing")
    st.markdown('</div>', unsafe_allow_html=True)
    render_footer()

def page_terms():
    _mail = f'<a href="mailto:{SUPPORT_EMAIL}" style="color:#818cf8;text-decoration:none;">{SUPPORT_EMAIL}</a>'
    body = (
        _lsec(1, "Acceptance of these Terms",
              'By creating an account or using Scanviction (the "Service"), you agree to these Terms of Service. If you do not agree, do not use the Service.')
        + _lsec(2, "What Scanviction is — and is not",
              'The Service provides algorithmic, <strong>educational</strong> market data, technical-analysis "signals," screening, watchlists, and alerts covering U.S.-listed securities. The same rules-based information is published to all subscribers.',
              'The Service is <strong>not</strong> investment, financial, legal, or tax advice, is <strong>not</strong> tailored to your personal circumstances, and creates no advisory or fiduciary relationship. We do not manage money, hold customer funds, execute trades, or tell any specific person to buy or sell any security. You are solely responsible for your own investment decisions.')
        + _lsec(3, "Eligibility",
              'You must be at least 18 years old and able to form a binding contract, and you are responsible for complying with all laws that apply to you.')
        + _lsec(4, "Your account",
              'Provide accurate information and keep your password secure. You are responsible for all activity under your account, and you agree to notify us promptly of any unauthorized use.')
        + _lsec(5, "Subscriptions, billing &amp; cancellation",
              'We offer a free tier and paid subscriptions (currently $19/month or $149/year). Paid plans are billed in advance through our payment processor, Stripe, and <strong>renew automatically</strong> each period until cancelled.',
              'You may cancel at any time; cancellation stops future renewals and your paid features remain active through the end of the current billing period. Prices may change on a prospective basis with notice.')
        + _lsec(6, "Refunds",
              'You may request a refund within <strong>30 days</strong> of your first paid purchase by emailing ' + _mail + '. After that window, payments for the current period are non-refundable, but you may cancel to avoid future charges.')
        + _lsec(7, "Acceptable use",
              'You agree not to resell or redistribute the Service or its data; scrape, crawl, or bulk-export content except as expressly permitted; reverse engineer or disrupt the Service; or use it for any unlawful purpose, including trading on material non-public information.')
        + _lsec(8, "Intellectual property",
              'The Service, its content, scores, and software are owned by Scanviction and protected by law. We grant you a limited, personal, non-transferable license to use the Service for your own individual purposes.')
        + _lsec(9, "Market data",
              'Prices, filings, and third-party inputs (e.g., exchange data, SEC filings, short-interest and sentiment data) are provided by third parties, may be <strong>delayed, incomplete, or inaccurate</strong>, and are used on an as-available basis. We do not guarantee the accuracy, timeliness, or completeness of any data or signal.')
        + _lsec(10, "No warranties",
              'The Service is provided "AS IS" and "AS AVAILABLE," without warranties of any kind, express or implied, including merchantability, fitness for a particular purpose, and non-infringement. We do not warrant that any signal will be profitable or that the Service will be uninterrupted or error-free.')
        + _lsec(11, "Limitation of liability",
              'To the maximum extent permitted by law, Scanviction and its operators will not be liable for any trading or investment losses, or for any indirect, incidental, special, or consequential damages. Our total liability for any claim will not exceed the amount you paid us in the 12 months before the claim.')
        + _lsec(12, "Indemnification",
              'You agree to indemnify and hold Scanviction harmless from claims arising out of your use of the Service or your violation of these Terms.')
        + _lsec(13, "Termination",
              'We may suspend or terminate accounts that violate these Terms or that we reasonably believe create risk. You may stop using the Service and delete your account at any time.')
        + _lsec(14, "Changes to these Terms",
              'We may update these Terms from time to time. Material changes will be posted here with a new "Last updated" date; continued use after changes means you accept them.')
        + _lsec(15, "Governing law",
              'These Terms are governed by the laws of the State of Ohio, United States, without regard to conflict-of-laws rules, and disputes will be resolved in the state or federal courts located in Ohio.')
        + _lsec(16, "Contact",
              'Questions about these Terms? Email ' + _mail + '.')
    )
    _legal_shell("Terms of Service", "July 2, 2026", body, "terms_back")

def page_privacy():
    _mail = f'<a href="mailto:{SUPPORT_EMAIL}" style="color:#818cf8;text-decoration:none;">{SUPPORT_EMAIL}</a>'
    body = (
        _lsec(1, "Information we collect",
              '<strong>Account information</strong> you provide: your name and email address, and a securely hashed password. <strong>Usage information</strong>: pages viewed and product events (e.g., sign-up, log-in) used to improve the Service, with identifiers stored in hashed form. <strong>Payment information</strong>: handled by Stripe — we receive confirmation of your subscription but <strong>never see or store your full card number</strong>. <strong>Technical data</strong>: standard log and device data such as browser type and approximate location from your IP.')
        + _lsec(2, "How we use your information",
              'To operate your account and deliver signals and alerts; to process subscriptions and send transactional email (verification, password reset, receipts); to secure the Service and prevent abuse; to understand and improve product usage; and to comply with legal obligations.')
        + _lsec(3, "How we share information",
              'We do <strong>not</strong> sell your personal information. We share it only with service providers that help us run the Service — for example <strong>Stripe</strong> (payments), <strong>Resend</strong> (email delivery), and our hosting, database, and market-data providers — and when required by law or to protect our rights.')
        + _lsec(4, "Cookies &amp; sessions",
              'We use a session token to keep you logged in and to operate the Service. We do not use third-party advertising cookies.')
        + _lsec(5, "Data retention",
              'We keep account information while your account is active and as needed for legal, security, and record-keeping purposes. You can request deletion at any time (see "Your rights").')
        + _lsec(6, "Security",
              'Passwords are stored using strong one-way hashing (bcrypt), data is transmitted over encrypted connections (HTTPS/TLS), and payments run entirely through Stripe. No method of storage or transmission is 100% secure, but we work to protect your information.')
        + _lsec(7, "Your rights",
              'You may access, correct, or delete your account information, or ask what we hold about you, by emailing ' + _mail + '. We will respond within a reasonable time.')
        + _lsec(8, "Children",
              'The Service is intended for adults (18+) and is not directed to children. We do not knowingly collect information from anyone under 18.')
        + _lsec(9, "United States",
              'The Service is operated from the United States and your information is processed there. By using the Service you consent to this processing.')
        + _lsec(10, "Changes to this policy",
              'We may update this Privacy Policy; material changes will be posted here with a new "Last updated" date.')
        + _lsec(11, "Contact",
              'Privacy questions? Email ' + _mail + '.')
    )
    _legal_shell("Privacy Policy", "July 2, 2026", body, "privacy_back")

def page_contact():
    render_topbar()
    back_button("ct_back")
    st.markdown('<div class="page-wrap">' ,unsafe_allow_html=True)
    st.markdown(f"""<div style="text-align:center;padding:32px 0 24px;">
        <div style="font-size:11px;font-weight:700;color:{BLUE};letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">We're Here to Help</div>
        <div style="font-size:34px;font-weight:900;color:#f1f5f9;letter-spacing:-1px;margin-bottom:8px;">Contact & Support</div>
        <div style="font-size:14px;color:#374f6e;">Reach us by email or chat with our AI support assistant below.</div>
    </div>""",unsafe_allow_html=True)

    top1,top2,top3 = st.columns(3,gap="small")
    with top1:
        st.markdown(f'''<div class="card card-blue" style="text-align:center;padding:24px;">
            <div style="font-size:28px;margin-bottom:10px;">📧</div>
            <div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">Email Support</div>
            <div style="font-size:12px;color:#374f6e;margin-bottom:12px;">For account, billing, and general questions</div>
            <div style="font-size:13px;font-weight:700;color:#a5b4fc;">support@scanviction.com</div>
            <div style="font-size:11px;color:#2a3a52;margin-top:6px;">Use the form below ↓ · reply within 24h</div>
        </div>''',unsafe_allow_html=True)
    with top2:
        st.markdown(f'''<div class="card" style="text-align:center;padding:24px;">
            <div style="font-size:28px;margin-bottom:10px;">💬</div>
            <div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">AI Support Chat</div>
            <div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Instant answers to platform questions</div>
            <div style="font-size:13px;font-weight:700;color:#4ade80;">● Available Now</div>
            <div style="font-size:11px;color:#2a3a52;margin-top:6px;">Scroll down to chat</div>
        </div>''',unsafe_allow_html=True)
    with top3:
        st.markdown(f'''<div class="card card-gold" style="text-align:center;padding:24px;">
            <div style="font-size:28px;margin-bottom:10px;">👑</div>
            <div style="font-size:14px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">Priority Support</div>
            <div style="font-size:12px;color:#374f6e;margin-bottom:12px;">For Annual plan subscribers</div>
            <div style="font-size:13px;font-weight:700;color:{GOLD};">4-hour response time</div>
            <div style="font-size:11px;color:#2a3a52;margin-top:6px;">Annual plan feature</div>
        </div>''',unsafe_allow_html=True)

    # ── In-app email webform (sends straight to support — no mail app) ──
    st.markdown("<br>",unsafe_allow_html=True)
    st.markdown('<div class="sec-hd">✉️ Send Us a Message</div>',unsafe_allow_html=True)
    st.markdown(f'<div style="font-size:12px;color:#374f6e;margin-bottom:12px;">Goes straight to <b style="color:#a5b4fc;">{SUPPORT_EMAIL}</b> — we reply within 24 hours. No email app required.</div>',unsafe_allow_html=True)
    _pf_name = st.session_state.user.get("name","") if is_authed() else ""
    _pf_email = st.session_state.user.get("email","") if is_authed() else ""
    with st.form("contact_form", clear_on_submit=True):
        cf1,cf2 = st.columns(2,gap="small")
        with cf1: cf_name = st.text_input("Your name", value=_pf_name, placeholder="Jane Doe")
        with cf2: cf_email = st.text_input("Your email", value=_pf_email, placeholder="you@example.com")
        cf_subj = st.text_input("Subject", placeholder="Billing question")
        cf_msg = st.text_area("Message", placeholder="How can we help?", height=130)
        if st.form_submit_button("Send message →", type="primary", use_container_width=True):
            if not cf_email or "@" not in cf_email:
                st.error("Please enter a valid email so we can reply.")
            elif not (cf_msg or "").strip():
                st.error("Please enter a message.")
            else:
                ok, info = _send_support_email(cf_name, cf_email, cf_subj, cf_msg)
                if ok:
                    st.success(f"✅ Sent! We'll reply to {cf_email} within 24 hours.")
                else:
                    st.success(f"✅ Message received — we'll reply to {cf_email} within 24 hours.")
                    if info == "DEMO":
                        st.caption("Live email delivery needs RESEND_API_KEY in Secrets; your message was saved locally for now.")

    st.markdown("<br>",unsafe_allow_html=True)
    st.markdown('<div class="sec-hd">💬 AI Support Assistant</div>',unsafe_allow_html=True)
    st.markdown('<div style="font-size:12px;color:#374f6e;margin-bottom:14px;">Ask anything about Scanviction — features, categories, signals, billing, or how to use the platform.</div>',unsafe_allow_html=True)

    # Chat history
    if "support_chat" not in st.session_state:
        st.session_state.support_chat = [
            {"role":"assistant","content":"Hi! I'm the Scanviction support assistant. I can help with questions about the platform, our composite signal categories, billing, features, or anything else. What can I help you with today?"}
        ]

    # Display chat
    for msg in st.session_state.support_chat:
        if msg["role"]=="assistant":
            st.markdown(f'''<div style="display:flex;gap:10px;margin-bottom:12px;align-items:flex-start;">
                <div style="background:{BLUE};border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">SW</div>
                <div style="background:{CARD};border:1px solid {BORDER};border-radius:0 10px 10px 10px;padding:10px 14px;font-size:13px;color:#d1d9e6;max-width:80%;line-height:1.6;">{_esc(msg["content"])}</div>
            </div>''',unsafe_allow_html=True)
        else:
            st.markdown(f'''<div style="display:flex;gap:10px;margin-bottom:12px;align-items:flex-start;flex-direction:row-reverse;">
                <div style="background:#2a3a52;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;">You</div>
                <div style="background:#0a1628;border:1px solid rgba(99,102,241,0.2);border-radius:10px 0 10px 10px;padding:10px 14px;font-size:13px;color:#d1d9e6;max-width:80%;line-height:1.6;">{_esc(msg["content"])}</div>
            </div>''',unsafe_allow_html=True)

    # Input
    with st.form("support_form", clear_on_submit=True):
        uc1,uc2=st.columns([5,1],gap="small")
        with uc1: user_q=st.text_input("",placeholder="Ask a question...",label_visibility="collapsed")
        with uc2: send=st.form_submit_button("Send →",type="primary",use_container_width=True)
        if send and user_q.strip():
            st.session_state.support_chat.append({"role":"user","content":user_q.strip()})
            # Call Anthropic API for support response
            with st.spinner(""):
                try:
                    import requests as _r
                    sys_prompt = """You are the Scanviction support assistant. Scanviction is a premium stock-signal web app for retail traders.

WHAT IT DOES
- Scans ~2,500 of the most liquid U.S. stocks every session and scores each 0–100 (the "Conviction Score") by blending live price action, SEC insider filings, FINRA short interest and money flow.
- Surfaces the strongest setups first ("Top Signals" on the home page).

SIGNAL CATEGORIES (23 total; each stock is assigned ONE best-fit category, no overlap)
- Bullish examples: Momentum Leaders, Breakout Watch, Relative Strength, Oversold Reversal, Volatility Squeeze, VCP Volume Dry-Up, Insider Cluster, Short Squeeze, Quiet Accumulation, Pullback Buy, Capitulation Bottom.
- Bearish / SHORT setups (newer): Breakdown, Distribution, Overbought Fade — flagged with a SHORT badge and scored on a separate short-side conviction model.

KEY FEATURES
- Discover: Top Signals home + a themed category browser.
- Daily Brief filters: the Brief feed can be narrowed by event type, signal category, minimum score, direction (long/short) and minimum category win rate. (These absorbed the old separate Refine / Screener / BI pages.)
- Stock detail page: a "Signal on the Chart" view that draws the detected pattern (breakout line, bull flag, squeeze bands, breakdown, reversal) right on the candles, plus a conviction-score breakdown, recent alerts and fundamentals.
- Alerts: notifies you when a stock newly enters a category, when insiders buy, when an 8-K drops, or on short-interest surges — in-app feed plus optional email/Telegram/push.
- Watchlist and a signal track-record (outcome tracking).

DATA SOURCES: Polygon.io (whole-market price/volume), SEC EDGAR (insider Form 4 + 8-K), FINRA (short interest), ApeWisdom (social buzz).

PLANS
- Free: market overview, 7 free categories, RSI/MACD signals, watchlist (10 stocks), BUY/AVOID.
- Premium $19/mo: all 23 categories incl. bear/short, advanced Brief filters (direction + category win rate), squeeze scanner, conviction breakdowns, signal charts, unlimited watchlist.
- Annual $149/yr: everything in Premium + priority support, CSV export, early access.
- Billing is via Stripe (in-page checkout). Cancel anytime in Settings → Subscription.

ALERTS & NOTIFICATIONS
- In-app alert feed for all users; email alerts on any tier; Telegram + browser push for Premium.
- Telegram setup: Settings → Profile → one-tap Telegram connect (or send /start to the bot and paste the Chat ID).
- Alert types: category entries, insider (Form 4) buys, fresh 8-K filings, short-interest surges, plus custom price / % move / volume / RSI alerts.

ACCOUNT & BILLING BASICS
- Sign up free with email; verify via the emailed code. Password reset is on the login page.
- Upgrade: Pricing page → Premium ($19/mo) or Annual ($149/yr) → secure Stripe checkout in-page.
- Manage/cancel: Settings → Subscription → Open Billing Portal (Stripe) — cancel anytime, access runs to the end of the paid period.
- Refunds: within 30 days of subscription start, via support@scanviction.com.

IMPORTANT: signals are algorithmic and EDUCATIONAL ONLY — never financial advice. If asked for a specific buy/sell call or investment advice, give general educational context, remind them it's not financial advice, and suggest a licensed advisor. For billing/account issues, point them to the contact form above or support@scanviction.com.

Be helpful, concise and friendly. If you genuinely don't know an answer, say so and point to support@scanviction.com rather than guessing."""
                    # Anthropic's Messages API requires the FIRST message to be role "user" and
                    # roles to strictly alternate. Our history starts with the assistant greeting
                    # (and "Clear Chat" re-adds one), which made every call fail with HTTP 400.
                    # Build the payload from the first user turn onward and merge any accidental
                    # same-role runs so the request is always valid.
                    msgs = []
                    for m in st.session_state.support_chat:
                        if not msgs and m["role"] != "user":
                            continue  # skip leading assistant greeting(s)
                        if msgs and msgs[-1]["role"] == m["role"]:
                            msgs[-1]["content"] += "\n\n" + m["content"]
                        else:
                            msgs.append({"role": m["role"], "content": m["content"]})
                    # Get Anthropic API key from secrets
                    try: anth_key = st.secrets.get("ANTHROPIC_API_KEY","")
                    except: anth_key = ""
                    if not anth_key:
                        answer = "Support chat requires ANTHROPIC_API_KEY in Streamlit Secrets. In the meantime, email support@scanviction.com — we respond within 24 hours!"
                    else:
                        resp = _r.post("https://api.anthropic.com/v1/messages",
                            headers={"Content-Type":"application/json",
                                     "x-api-key":anth_key,
                                     "anthropic-version":"2023-06-01"},
                            json={"model":"claude-haiku-4-5-20251001","max_tokens":400,
                                  "system":sys_prompt,"messages":msgs},
                            timeout=20)
                        if resp.status_code==200:
                            answer = resp.json()["content"][0]["text"]
                        else:
                            answer = f"I'm having trouble right now (status {resp.status_code}). Please email support@scanviction.com for immediate help."
                except Exception as e:
                    answer = f"Connection issue. Please email support@scanviction.com — we typically respond within 24 hours."
            st.session_state.support_chat.append({"role":"assistant","content":answer})
            st.rerun()

    st.markdown("<br>",unsafe_allow_html=True)
    if st.button("🗑 Clear Chat",key="clear_support"):
        st.session_state.support_chat=[{"role":"assistant","content":"Chat cleared. How can I help you?"}]
        st.rerun()

    # FAQ quick links
    st.markdown("<br>",unsafe_allow_html=True)
    st.markdown('<div class="sec-hd">Common Questions</div>',unsafe_allow_html=True)
    faqs=[
        ("How do I upgrade to Premium?","Go to Pricing in the top nav, select Premium Monthly or Annual, and click the subscribe button. Payment is processed securely via Stripe."),
        ("What are the composite signal categories?","Scanviction has 23 proprietary categories — including 3 bear/short setups — that combine multiple signals at once (e.g. RSI + short interest + insider buying + money flow) to surface setups not visible through standard TA. Each stock is assigned its single best-fit category."),
        ("Is this financial advice?","No. Scanviction provides algorithmic, educational signals only. Nothing constitutes financial advice. Always consult a licensed financial advisor."),
        ("How do I cancel my subscription?","Go to Settings → Subscription → Open Billing Portal. You can cancel anytime with no questions asked."),
        ("Can I get a refund?","Yes — contact support@scanviction.com within 30 days of your subscription start date."),
    ]
    for q,a in faqs:
        with st.expander(q):
            st.markdown(f'<div style="font-size:13px;color:#374f6e;line-height:1.75;">{a}</div>',unsafe_allow_html=True)

    st.markdown('</div>',unsafe_allow_html=True)
    render_footer()

# ─────────────────────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────────────────────

# ── 1. Handle Stripe payment returns (URL params) ──
handle_payment_return()

# ── 1b. Warm up the scored universe in the background so the first Discover
#        click is fast (the worker keeps it near-live thereafter). ──
try:
    # Capture the FMP key on the main thread (the worker can't read secrets),
    # so background discovery can run market-wide when a key is configured.
    _FMP_KEY_CAPTURED = get_fmp_key()
    # Same for Polygon: when set, the universe worker builds the whole-market
    # scan (thousands of tickers) instead of the curated yfinance/FMP set.
    _POLYGON_KEY_CAPTURED = get_polygon_key()
    try: _TG_TOKEN_CAPTURED = st.secrets.get("TELEGRAM_BOT_TOKEN", "") or ""
    except Exception: _TG_TOKEN_CAPTURED = ""
    # Email config for the worker thread (see _alert_email — st.secrets reads are
    # only reliable here on the main thread).
    try: _RESEND_KEY_CAPTURED = st.secrets.get("RESEND_API_KEY", "") or ""
    except Exception: _RESEND_KEY_CAPTURED = ""
    try: _EMAIL_FROM_CAPTURED = st.secrets.get("EMAIL_FROM", "") or ""
    except Exception: _EMAIL_FROM_CAPTURED = ""
    try: _APP_URL_CAPTURED = st.secrets.get("APP_URL", "") or ""
    except Exception: _APP_URL_CAPTURED = ""
    ensure_universe_worker()
except Exception:
    pass

# ── 2. Execute Stripe redirect if checkout session was just created ──
if st.session_state.get("_redirect_url"):
    url = st.session_state.get("_redirect_url")
    sel_plan = st.session_state.get("sel_plan","premium")
    plan_display = {"premium":"Premium Monthly — $19/mo","annual":"Annual Plan — $149/yr","free":"Free"}.get(sel_plan,"Premium Monthly")
    render_topbar("pricing")
    st.markdown(f'<div style="font-size:12px;color:#374f6e;padding:0 0 16px;"><span style="color:#4a5e7a;">Pricing</span> <span style="color:#2a3a52;"> › </span> <span style="color:#e2e8f0;font-weight:600;">Secure Checkout</span></div>',unsafe_allow_html=True)
    lc,rc = st.columns([3,2],gap="large")
    with lc:
        st.markdown(f"""
        <div style="background:#0d1525;border:1px solid rgba(99,102,241,0.3);border-radius:14px;padding:24px;margin-bottom:14px;">
            <div style="font-size:10px;font-weight:700;color:{BLUE};letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Your Selected Plan</div>
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <div style="font-size:18px;font-weight:800;color:#e2e8f0;">{plan_display}</div>
                <div style="font-size:11px;color:#4ade80;font-weight:700;">● Cancel anytime</div>
            </div>
        </div>
        <div style="background:#080b14;border:1px solid {BORDER};border-radius:10px;padding:16px 18px;margin-bottom:16px;">
            <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">What you get immediately:</div>
            <div style="font-size:13px;color:#374f6e;line-height:2.2;">✅&nbsp; All 23 composite signal categories (incl. bear/short)<br>✅&nbsp; Advanced Brief filters + short-squeeze scanner<br>✅&nbsp; Signal charts &amp; conviction breakdowns<br>✅&nbsp; Plain-English insights<br>✅&nbsp; Unlimited watchlist &amp; price alerts</div>
        </div>
        """, unsafe_allow_html=True)
        # Native link button navigates reliably. A raw <a target="_top"> inside st.markdown renders
        # but often does NOT navigate on click in Streamlit — that's the "nothing happens" symptom.
        st.link_button("🔒  Complete Secure Checkout on Stripe →", url, use_container_width=True, type="primary")
        st.markdown('<div style="text-align:center;margin-top:10px;font-size:11px;color:#2a3a52;">Powered by <strong style="color:#6775ba;">Stripe</strong> · 256-bit SSL · PCI compliant · Card details never touch our servers</div>', unsafe_allow_html=True)
        st.markdown("<br>",unsafe_allow_html=True)
        if st.button("← Change Plan",key="cancel_ck",use_container_width=True):
            st.session_state.pop("_redirect_url",None); nav("pricing")
    with rc:
        st.markdown(f"""
        <div style="background:#080b14;border:1px solid {BORDER};border-radius:14px;padding:22px;">
            <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-bottom:12px;">🔒 Payment Security</div>
            <div style="font-size:12px;color:#374f6e;line-height:2.2;">
            🛡️&nbsp; 256-bit SSL encryption<br>💳&nbsp; Zero card data on our servers<br>🔄&nbsp; Cancel anytime, no questions<br>📧&nbsp; Instant access after payment<br>💰&nbsp; 30-day refund policy<br>🏦&nbsp; Powered by Stripe
            </div>
        </div>
        <div style="background:#0d1525;border:1px solid rgba(34,197,94,0.2);border-radius:10px;padding:18px;margin-top:12px;">
            <div style="font-size:12px;font-weight:700;color:{GREEN};margin-bottom:8px;">After Payment ✓</div>
            <div style="font-size:12px;color:#374f6e;line-height:2.2;">1. Account upgrades instantly<br>2. All premium categories unlock<br>3. Set up watchlist &amp; alerts<br>4. Filter your Daily Brief<br>5. Configure email digests</div>
        </div>
        <div style="margin-top:12px;text-align:center;font-size:12px;color:#2a3a52;">Questions? <span style="color:#a5b4fc;font-weight:600;">support@scanviction.com</span></div>
        """, unsafe_allow_html=True)
    st.stop()

# ── 3. Payment notifications ──
if st.session_state.get("_push_registered"):
    st.session_state.pop("_push_registered", None)
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#04200d,#0d1525);
                border:1px solid rgba(34,197,94,0.4);border-radius:14px;
                padding:20px 24px;margin-bottom:18px;">
        <div style="display:flex;align-items:center;gap:14px;">
            <div style="font-size:32px;">🔔</div>
            <div>
                <div style="font-size:16px;font-weight:800;color:#4ade80;margin-bottom:4px;">
                    Push Notifications Enabled!
                </div>
                <div style="font-size:13px;color:#374f6e;line-height:1.6;">
                    You'll receive instant push alerts when Scanviction detects new opportunities on stocks you follow.
                    <br><strong style="color:#e2e8f0;">Pro tip:</strong> On mobile, tap the share icon and "Add to Home Screen" to install Scanviction as an app.
                </div>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

if st.session_state.get("_pay_success"):
    plan = st.session_state.pop("_pay_success")
    plan_name = "Annual Plan" if plan=="annual" else "Premium"
    # Rich success banner
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#04200d,#0d1525);
                border:1px solid rgba(34,197,94,0.4);border-radius:14px;
                padding:28px 32px;margin-bottom:20px;text-align:center;">
        <div style="font-size:48px;margin-bottom:12px;">🎉</div>
        <div style="font-size:24px;font-weight:800;color:#4ade80;margin-bottom:8px;">
            Welcome to Scanviction {plan_name}!
        </div>
        <div style="font-size:14px;color:#374f6e;margin-bottom:20px;line-height:1.7;">
            Your account has been upgraded. You now have access to all {plan_name} features.<br>
            Start exploring all 23 composite categories, the short-squeeze scanner, and the full Brief filters.
        </div>
        <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;">
            <span style="background:rgba(34,197,94,0.1);color:#4ade80;font-size:12px;font-weight:700;
                         padding:6px 16px;border-radius:20px;border:1px solid rgba(34,197,94,0.3);">
                ✅ All 23 categories unlocked
            </span>
            <span style="background:rgba(34,197,94,0.1);color:#4ade80;font-size:12px;font-weight:700;
                         padding:6px 16px;border-radius:20px;border:1px solid rgba(34,197,94,0.3);">
                ✅ Squeeze Scanner active
            </span>
            <span style="background:rgba(34,197,94,0.1);color:#4ade80;font-size:12px;font-weight:700;
                         padding:6px 16px;border-radius:20px;border:1px solid rgba(34,197,94,0.3);">
                ✅ Advanced Brief filters enabled
            </span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    # Quick action buttons
    _qa1, _qa2, _qa3 = st.columns(3, gap="small")
    with _qa1:
        if st.button("🎯 Explore Premium Categories", key="ps_disc", type="primary", use_container_width=True):
            nav("discover")
    with _qa2:
        if st.button("🔔 Open your Daily Brief", key="ps_bi", use_container_width=True):
            nav("signals")
    with _qa3:
        if st.button("🔔 Set Up Alerts", key="ps_alerts", use_container_width=True):
            nav("settings")

if st.session_state.get("_pay_error"):
    err = st.session_state.pop("_pay_error")
    st.markdown(f"""
    <div style="background:#200404;border:1px solid rgba(239,68,68,0.3);border-radius:10px;
                padding:16px 20px;margin-bottom:16px;">
        <div style="font-size:14px;font-weight:700;color:#f87171;margin-bottom:6px;">
            ❌ Payment Error
        </div>
        <div style="font-size:13px;color:#374f6e;margin-bottom:8px;">{err}</div>
        <div style="font-size:12px;color:#2a3a52;">
            Need help? Email <span style="color:#a5b4fc;font-weight:600;">support@scanviction.com</span>
            and we'll sort it out within 24 hours.
        </div>
    </div>
    """, unsafe_allow_html=True)

if st.session_state.get("_pay_cancelled"):
    st.session_state.pop("_pay_cancelled")
    st.markdown("""
    <div style="background:#0d1525;border:1px solid rgba(245,158,11,0.3);border-radius:10px;
                padding:16px 20px;margin-bottom:16px;">
        <div style="font-size:14px;font-weight:700;color:#f59e0b;margin-bottom:4px;">
            ⚠️ Checkout Cancelled
        </div>
        <div style="font-size:13px;color:#374f6e;">
            No charge was made. Your account remains on the free plan.
            Ready to upgrade? Choose a plan below — it only takes 2 minutes.
        </div>
    </div>
    """, unsafe_allow_html=True)

# ── 3b. Welcome notifications ──
if st.session_state.get("_logged_out"):
    st.session_state.pop("_logged_out")
    st.toast("👋 You've been logged out. See you next time!", icon="✅")
if st.session_state.get("_signup_success"):
    name = st.session_state.pop("_signup_success")
    st.toast(f"🎉 Account created! Welcome to Scanviction, {name}!", icon="🚀")
if st.session_state.get("_login_welcome"):
    name = st.session_state.pop("_login_welcome")
    st.toast(f"👋 Welcome back, {name}!", icon="✅")

# ── 4. Complete pending checkout after login ──
if is_authed() and st.session_state.get("_pending_checkout"):
    plan = st.session_state.pop("_pending_checkout")
    url, err = create_checkout_session(plan, st.session_state.user["email"])
    if url: st.session_state["_redirect_url"] = url; st.rerun()
    else:   st.error(f"Checkout error: {err}")

page=st.session_state.get("page","landing")

# ── SINGLE ACCESS CHECKPOINT ──
# All gating flows through can_access() + PAGE_ACCESS (defined near the role
# helpers). This replaces the old scattered checks (separate auth_required /
# premium_required / admin_required sets plus per-page inline gates) so there
# is exactly one place that decides access, and direct-URL access is blocked
# identically to nav clicks.
_FOOTER_STATE["done"] = False   # reset the once-per-run footer guard
_need = PAGE_ACCESS.get(page)
if _need is not None and not can_access(page):
    if not is_authed():
        # Not logged in → remember destination, show login.
        st.session_state["_intended_page"] = page
        page_login()
    elif _need == "admin":
        render_topbar(page)
        st.markdown('<div class="page-wrap">', unsafe_allow_html=True)
        st.markdown("""<div style="background:#200404;border:1px solid rgba(239,68,68,0.3);border-radius:14px;
                    padding:32px;text-align:center;margin:60px auto;max-width:520px;">
            <div style="font-size:42px;margin-bottom:14px;">🛡️</div>
            <div style="font-size:20px;font-weight:800;color:#f87171;margin-bottom:8px;">Admin Access Required</div>
            <div style="font-size:13px;color:#374f6e;line-height:1.7;">This page is restricted to admin users only.
            Contact support if you believe you should have access.</div>
        </div>""", unsafe_allow_html=True)
        if st.button("← Return to Dashboard", key="adm_deny_back", use_container_width=True):
            nav("dashboard")
        st.markdown('</div>', unsafe_allow_html=True)
    else:  # premium gate
        _t, _d = ("⭐ Premium Feature", "Upgrade to unlock this feature.")
        render_topbar(page)
        st.markdown('<div class="page-wrap">', unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:22px;font-weight:800;color:#e2e8f0;margin-bottom:8px;">{_t}</div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:13px;color:#374f6e;margin-bottom:20px;">{_d}</div>', unsafe_allow_html=True)
        render_lock(_t)
        st.markdown('</div>', unsafe_allow_html=True)
else:
    _scroll_to_top_on_change()   # land at the top when the page/category changes
    if page=="landing":      page_landing()
    elif page=="features":     page_features()
    elif page=="login":        page_login()
    elif page=="signup":       page_signup()
    elif page=="verify_email": page_verify_email()
    elif page=="forgot_pw":    page_forgot()
    elif page=="pricing":      page_pricing()
    elif page=="contact":      page_contact()
    elif page=="terms":        page_terms()
    elif page=="privacy":      page_privacy()
    elif page=="dashboard":    page_dashboard()
    elif page=="discover":     page_discover()
    elif page=="watchlist":    page_watchlist()
    # Refine (and the BI dashboard it had already absorbed) are retired — their filtering
    # now lives in the Brief. Both keys still route so existing bookmarks and saved links
    # land on the surface that took over, rather than on a 404.
    elif page in ("screener", "bi_dashboard"): page_signals()
    elif page=="signals":      page_signals()
    elif page=="performance":  page_performance()
    elif page=="signal_track": page_signal_track()
    elif page=="stock_detail": page_detail()
    elif page=="settings":     page_settings()
    elif page=="admin":        page_admin()
    else: page_landing()

# ── Global footer on EVERY page (disclaimer: not financial advice) ──
# render_footer() is idempotent per run, so pages that call it inline aren't doubled;
# this catch-all guarantees the disclaimer appears on the pages that don't.
try:
    render_footer()
except Exception:
    pass
