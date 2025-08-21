import time
import threading
from typing import Optional, Dict, Any

import streamlit as st
import os
import asyncio
import logging
import sys

# Force ProactorEventLoop on Windows before importing Playwright (supports subprocess)
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

from playwright.sync_api import sync_playwright, Page
import webbrowser
from dotenv import load_dotenv

# Use existing bot functions without modifying bot.py
from bot import (
    connect_to_edge_existing_tab,
    extract_position_data,
    extract_open_orders,
    is_cdp_available,
    start_edge_with_cdp,
    wait_for_cdp,
    edge_running,
    kill_edge_processes,
    CDP_PORT,
    DELTA_TRADE_URL,
)

load_dotenv()

st.set_page_config(page_title="Delta Bot", page_icon="🚀", layout="wide")

# Configure terminal logging
logger = logging.getLogger("delta_app")
if not logger.handlers:
    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# No UI controls; app auto-starts

PRIMARY = "#0E7CFF"
BG_GRADIENT = (
    "linear-gradient(135deg, rgba(14,124,255,0.12) 0%, rgba(2,6,23,0.85) 40%, "
    "rgba(2,6,23,0.95) 100%)"
)

CUSTOM_CSS = f"""
<style>
    .stApp {{
        background: {BG_GRADIENT};
        color: #E6EDF3;
        font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial, "Apple Color Emoji", "Segoe UI Emoji";
    }}
    .metric-card {{
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 16px;
        padding: 16px 18px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.25);
    }}
    .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }}
    .col-3 {{ grid-column: span 3; }}
    .col-4 {{ grid-column: span 4; }}
    .col-6 {{ grid-column: span 6; }}
    .col-12 {{ grid-column: span 12; }}
    .title {{ font-weight: 700; font-size: 28px; letter-spacing: 0.3px; }}
    .subtitle {{ font-weight: 500; opacity: .8; margin-top: 4px; }}
    .label {{ opacity: .7; font-size: 12px; }}
    .value {{ font-weight: 700; font-size: 20px; }}
    .good {{ color: #16C784; }}
    .bad {{ color: #EA3943; }}
    .pill {{ display:inline-block; padding:4px 10px; border-radius:999px; background: rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.12); }}
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

st.markdown("""
<div class="grid">
    <div class="col-12">
        <div class="title">Delta Bot <span class="pill">Live Monitor</span></div>
        <div class="subtitle">Positions and Open Orders from your active Edge trading tab</div>
    </div>
</div>
""", unsafe_allow_html=True)

status_ph = st.empty()
pos_ph = st.empty()
orders_ph = st.empty()

# Shared app state
STATE: Dict[str, Any] = {
    "status": "starting",
    "error": None,
    "last_pos": {},
    "last_orders": {},
}
STATE_LOCK = threading.Lock()


def open_in_edge(url: str):
    try:
        if os.name == "nt":
            # Use Edge protocol to force opening in Edge on Windows
            os.startfile(f"microsoft-edge:{url}")
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def ensure_edge_cdp_ready() -> bool:
    logger.info("Checking CDP availability on port %s", CDP_PORT)
    if is_cdp_available(CDP_PORT):
        logger.info("CDP is available")
        return True
    # Optional auto-kill-and-relaunch if Edge is running without CDP
    allow_kill = (os.getenv("EDGE_ALLOW_KILL", "0").strip().lower() in ("1", "true", "yes"))
    if edge_running() and allow_kill:
        try:
            logger.warning("Killing existing Edge processes to relaunch with CDP…")
            kill_edge_processes()
            time.sleep(1)
        except Exception:
            pass
        logger.info("Starting Edge with CDP…")
        start_edge_with_cdp(DELTA_TRADE_URL, CDP_PORT)
        if wait_for_cdp(CDP_PORT, 12):
            logger.info("CDP became available after relaunch")
            return True
        return False
    # Try to start Edge with CDP (if Edge already running without CDP, this likely opens a tab but won’t enable CDP)
    logger.info("Attempting to start Edge with CDP without killing…")
    if start_edge_with_cdp(DELTA_TRADE_URL, CDP_PORT):
        ok = wait_for_cdp(CDP_PORT, 8)
        logger.info("CDP availability after start: %s", ok)
        return ok
    return False


def worker():
    # Always try to ensure CDP; allow auto-relaunch by default for simplicity
    os.environ.setdefault("EDGE_ALLOW_KILL", "1")
    with STATE_LOCK:
        STATE["status"] = "connecting"
        STATE["error"] = None
    logger.info("Opening Delta in Edge…")
    open_in_edge(DELTA_TRADE_URL)

    if not ensure_edge_cdp_ready():
        msg = "Edge CDP not available. Start Edge with --remote-debugging-port=9222"
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["error"] = msg
        logger.error(msg)
        return

    # Playwright startup in this background thread with Proactor loop
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            except Exception:
                pass
    logger.info("Starting Playwright…")
    pw = sync_playwright().start()
    try:
        logger.info("Attaching to existing Edge tab…")
        page: Page = connect_to_edge_existing_tab(DELTA_TRADE_URL, reuse_playwright=pw)
    except Exception as e:
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["error"] = f"Attach failed: {e}"
        logger.exception("Attach failed: %s", e)
        try:
            pw.stop()
        except Exception:
            pass
        return

    with STATE_LOCK:
        STATE["status"] = "connected"
    logger.info("Connected. Starting fetch loop…")

    POS_INTERVAL = 10
    ORD_INTERVAL = 30
    last_pos = 0.0
    last_orders = 0.0

    while True:
        now = time.time()
        try:
            if now - last_pos >= POS_INTERVAL:
                logger.info("Fetching position…")
                pos_data = extract_position_data(page) or {}
                with STATE_LOCK:
                    STATE["last_pos"] = pos_data
                last_pos = now
            if now - last_orders >= ORD_INTERVAL:
                logger.info("Fetching open orders…")
                orders_data = extract_open_orders(page) or {}
                with STATE_LOCK:
                    STATE["last_orders"] = orders_data
                last_orders = now
        except Exception as e:
            logger.exception("Fetch loop error: %s", e)
            with STATE_LOCK:
                STATE["error"] = str(e)
                STATE["status"] = "error"
        time.sleep(0.5)


def start_worker_if_needed():
    if "_worker" not in st.session_state or st.session_state._worker is None or not st.session_state._worker.is_alive():
        logger.info("Starting background worker thread…")
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        st.session_state._worker = t


def format_pos_block(data: Dict[str, Any]) -> str:
    if not data:
        return "<div class='metric-card'>No position</div>"
    size = data.get("size", "-")
    entry = data.get("entry_price", "-")
    mark = data.get("mark_price", "-")
    upnl = data.get("upnl", "-")
    color = "good" if isinstance(upnl, str) and (upnl.strip().startswith("+") or "green" in upnl.lower()) else "bad"
    return f"""
    <div class="metric-card">
      <div class="label">Current Position</div>
      <div class="grid">
         <div class="col-3"><div class="label">Size</div><div class="value">{size}</div></div>
         <div class="col-3"><div class="label">Entry Price</div><div class="value">{entry}</div></div>
         <div class="col-3"><div class="label">Mark Price</div><div class="value">{mark}</div></div>
         <div class="col-3"><div class="label">uPNL</div><div class="value {color}">{upnl}</div></div>
      </div>
    </div>
    """


def format_orders_block(data: Dict[str, Any]) -> str:
    orders = (data or {}).get("orders") or []
    if not orders:
        return "<div class='metric-card'>No open orders</div>"
    rows = []
    for o in orders[:2]:
        side = (o.get("side") or "").upper()
        size = o.get("size") or "-"
        price = o.get("price") or "-"
        side_cls = "good" if side.lower() == "long" else "bad"
        rows.append(f"<div class='grid' style='margin-top:8px'>"
                    f"<div class='col-4'><div class='label'>Side</div><div class='value {side_cls}'>{side}</div></div>"
                    f"<div class='col-4'><div class='label'>Size</div><div class='value'>{size}</div></div>"
                    f"<div class='col-4'><div class='label'>Limit Price</div><div class='value'>{price}</div></div>"
                    f"</div>")
    return "<div class='metric-card'><div class='label'>Open Orders</div>" + "".join(rows) + "</div>"


start_worker_if_needed()

# Simple UI update loop: read shared state and render
last_render_pos = None
last_render_orders = None
while True:
    with STATE_LOCK:
        status = STATE.get("status")
        err = STATE.get("error")
        pos = STATE.get("last_pos") or {}
        orders = STATE.get("last_orders") or {}

    if status == "error" and err:
        status_ph.error(err)
    elif status == "connected":
        status_ph.success("Connected. Streaming data…")
    elif status == "connecting":
        status_ph.info("Connecting to Edge and attaching to the trading tab…")
    else:
        status_ph.info("Starting…")

    if pos != last_render_pos:
        pos_ph.markdown(format_pos_block(pos), unsafe_allow_html=True)
        last_render_pos = pos
    if orders != last_render_orders:
        orders_ph.markdown(format_orders_block(orders), unsafe_allow_html=True)
        last_render_orders = orders

    time.sleep(0.5)

# Footer
st.caption("Positions every 10s; orders every 30s. Logs printed to the Streamlit terminal.")
