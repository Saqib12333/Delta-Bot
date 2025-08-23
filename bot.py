import sys
import time
import os
import re
import webbrowser
import subprocess
import shutil
import urllib.request
import urllib.error
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

from playwright.sync_api import sync_playwright, BrowserContext, Page, Browser
from dotenv import load_dotenv
import argparse


PROJECT_ROOT = Path(__file__).resolve().parent
# Load .env configuration early
load_dotenv(PROJECT_ROOT / ".env")

# Setup logging to file (use 'debuuug' per user request)
DEBUG_DIR = PROJECT_ROOT / "debuuug"
DEBUG_DIR.mkdir(exist_ok=True)

# Create timestamped log file for each run
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
TERMINAL_LOG_FILE = DEBUG_DIR / f"terminal_{timestamp}.log"

# Configure logging to capture all terminal output
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',  # Just the raw message for terminal logs
    handlers=[
        logging.FileHandler(TERMINAL_LOG_FILE, mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
# Create html_snapshots directory for saving page content
HTML_SNAPSHOTS_DIR = PROJECT_ROOT / "html_snapshots"
LOG_FILE: Optional[Path] = None

# Env-driven URL selection
DEFAULT_DEMO_URL = os.getenv("DELTA_DEMO_URL", "https://demo.delta.exchange/app/futures/trade/BTC/BTCUSD")
DEFAULT_LIVE_URL = os.getenv("DELTA_LIVE_URL", "https://www.delta.exchange/app/futures/trade/BTC/BTCUSD")
ENV_NAME = (os.getenv("DELTA_ENV", "demo") or "demo").strip().lower()
DELTA_TRADE_URL = os.getenv("DELTA_TRADE_URL") or (DEFAULT_LIVE_URL if ENV_NAME == "live" else DEFAULT_DEMO_URL)
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
RPA_DIAG = (os.getenv("RPA_DIAG", "0").strip().lower() in ("1", "true", "yes"))
ORDERS_REQUIRE_CANCEL = (os.getenv("ORDERS_REQUIRE_CANCEL", "0").strip().lower() in ("1", "true", "yes"))

# Position monitoring settings
POSITIONS_INTERVAL = 10  # seconds
ORDERS_INTERVAL = 30     # seconds
BASE_LOOP_SLEEP = 1      # seconds


def log(msg: str) -> None:
    """Log a message with RPA prefix to both console and timestamped file."""
    line = f"[RPA] {msg}"
    logger.info(line)  # This goes to both console and file


def log_error(msg: str) -> None:
    """Log error messages."""
    line = f"[RPA] ❌ {msg}"
    logger.error(line)


def log_debug(msg: str) -> None:
    """Log debug messages (only when RPA_DIAG is enabled)."""
    if RPA_DIAG:
        line = f"[RPA] 🔧 {msg}"
        logger.debug(line)


def set_log_file(path: Path) -> None:
    global LOG_FILE
    LOG_FILE = path


def ensure_debug_dir() -> Path:
    debug_dir = PROJECT_ROOT / "debuuug"
    debug_dir.mkdir(exist_ok=True)
    return debug_dir


def ensure_html_snapshots_dir() -> Path:
    """Ensure html_snapshots directory exists"""
    HTML_SNAPSHOTS_DIR.mkdir(exist_ok=True)
    return HTML_SNAPSHOTS_DIR


def save_dom_snapshot(page: Page, label: str = "snapshot") -> Optional[Path]:
    """Save the page's HTML to html_snapshots for debugging."""
    try:
        ensure_html_snapshots_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = HTML_SNAPSHOTS_DIR / f"{label}_{ts}.html"
        content = page.content()
        p.write_text(content, encoding="utf-8")
        log(f"🧾 Saved DOM snapshot: {p}")
        return p
    except Exception as e:
        log(f"⚠️ Failed to save DOM snapshot: {e}")
        return None


def is_cdp_available(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_edge_with_cdp(target_url: str, port: int) -> bool:
    """Attempt to start Microsoft Edge with remote debugging. Returns True if process launch didn't raise."""
    candidates = [
        r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    ]
    edge_path = next((p for p in candidates if os.path.exists(p)), None)
    if edge_path is None:
        # Fallback to PATH-resolved msedge
        edge_path = shutil.which("msedge")
    if edge_path is None:
        log("⚠️ Could not locate msedge.exe automatically.")
        return False
    try:
        subprocess.Popen([edge_path, f"--remote-debugging-port={port}", target_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log(f"⚠️ Failed to start Edge with CDP: {e}")
        return False


def edge_running() -> bool:
    try:
        proc = subprocess.run([
            "tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"
        ], capture_output=True, text=True, check=False)
        out = (proc.stdout or "").strip().lower()
        return ("msedge.exe" in out) and ("no tasks" not in out)
    except Exception:
        return False


def kill_edge_processes() -> None:
    try:
        subprocess.run(["taskkill", "/IM", "msedge.exe", "/F", "/T"], capture_output=True, text=True, check=False)
    except Exception:
        pass


def wait_for_cdp(port: int, timeout_s: int = 15) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_cdp_available(port):
            return True
        time.sleep(0.5)
    return False


def extract_position_data(page: Page) -> Dict[str, Any]:
    """Extract position data from the Positions table row for BTCUSD"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result: Dict[str, Any] = {
        "size": None,
        "entry_price": None,
        "mark_price": None,
        "upnl": None,
        "timestamp": ts,
        "side": None,
        "has_position": False,
    }

    try:
        # Ensure Positions tab is active
        _activate_tab(page, r"^Positions$")
        try:
            page.wait_for_timeout(150)
        except Exception:
            pass

        # Find BTCUSD row
        row = page.locator("tr:has(td:has-text('BTCUSD'))").first
        if row.count() == 0:
            row = page.locator("tr:has(:text('BTCUSD'))").first
        if row.count() == 0:
            save_dom_snapshot(page, label="positions_not_found")
            return result

        try:
            row.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass

        # Collect headers if present
        table = row.locator("xpath=ancestor::table[1]")
        headers: List[str] = []
        try:
            ths = table.locator("thead th")
            headers = [t.strip() for t in ths.all_text_contents()] if ths.count() > 0 else []
        except Exception:
            headers = []
        # Build cell list (th+td) and collect helpful attributes
        cells = row.locator("th, td")
        cell_count = 0
        try:
            cell_count = cells.count()
        except Exception:
            cell_count = 0

        cell_texts: List[str] = []
        cell_labels: List[str] = []
        for i in range(cell_count):
            td = cells.nth(i)
            # Text content
            try:
                txt = (td.inner_text(timeout=800) or "").strip()
            except Exception:
                txt = ""
            cell_texts.append(txt)
            # Attribute-derived labels
            labels: List[str] = []
            for attr in ("data-title", "aria-label", "title", "data-column", "data-col"):
                try:
                    v = td.get_attribute(attr)
                    if v:
                        labels.append(v)
                except Exception:
                    pass
            cell_labels.append("|".join(l.lower() for l in labels))

    # One-time diagnostic dump (only in diagnostic mode)
        if RPA_DIAG and not getattr(extract_position_data, "_row_dumped", False):
            log("🧩 Row cells index dump: " + " | ".join([f"[{i}] {t}" for i, t in enumerate(cell_texts)]))
            setattr(extract_position_data, "_row_dumped", True)

        # Direct attribute-based lookup first
        def pick_by_label(names: List[str]) -> Optional[str]:
            keys = [n.lower() for n in names]
            for i, lab in enumerate(cell_labels):
                for k in keys:
                    if k in lab:
                        val = (cell_texts[i] or "").strip()
                        if val:
                            return val
            return None

        result["size"] = result["size"] or pick_by_label(["size"])   
        result["entry_price"] = result["entry_price"] or pick_by_label(["entry price", "avg price"]) 
        result["mark_price"] = result["mark_price"] or pick_by_label(["mark price"]) 
        result["upnl"] = result["upnl"] or pick_by_label(["upnl", "unrealized", "unrealised"]) 

        # Try symbol-relative mapping if symbol text exists in same row
        if any(v is None for v in (result["size"], result["entry_price"], result["mark_price"], result["upnl"])):
            sym_idx = next((i for i, t in enumerate(cell_texts) if re.search(r"\bbtcusd\b", t or "", re.I)), -1)
            if sym_idx != -1:
                def get_rel(offset: int) -> Optional[str]:
                    j = sym_idx + offset
                    if 0 <= j < len(cell_texts):
                        v = (cell_texts[j] or "").strip()
                        return v or None
                    return None
                # Offsets from earlier screenshot
                result["size"] = result["size"] or get_rel(1)
                result["entry_price"] = result["entry_price"] or get_rel(3)
                result["mark_price"] = result["mark_price"] or get_rel(6)
                result["upnl"] = result["upnl"] or get_rel(10)

        # Heuristic for the observed layout where [3] == "Add"
        if any(v is None for v in (result["size"], result["entry_price"], result["mark_price"], result["upnl"])) and len(cell_texts) >= 10:
            token = (cell_texts[3] or "").lower()
            if "add" in token:
                result["size"] = result["size"] or (cell_texts[0] or None)
                result["entry_price"] = result["entry_price"] or (cell_texts[2] or None)
                result["mark_price"] = result["mark_price"] or (cell_texts[5] or None)
                result["upnl"] = result["upnl"] or (cell_texts[9] or None)

        # Header-aligned mapping using Size as anchor
        if any(result[k] is None for k in ("size", "entry_price", "mark_price", "upnl")) and headers:
            header_map: Dict[str, int] = {h.lower(): i for i, h in enumerate(headers)}

            def hidx(keys: List[str]) -> Optional[int]:
                for k in keys:
                    kk = k.lower()
                    for h, i in header_map.items():
                        if kk in h:
                            return i
                return None

            # Find index of Size header and the matching cell index
            size_h = hidx(["size"])
            size_c = None
            # Prefer attribute label match
            for i, lab in enumerate(cell_labels):
                if "size" in lab:
                    size_c = i
                    break
            # Fallback: look for BTC unit in text (e.g., "+0.001 BTC")
            if size_c is None:
                for i, txt in enumerate(cell_texts):
                    if re.search(r"\bbtc\b", txt or "", re.I):
                        size_c = i
                        break

            if size_h is not None and size_c is not None:
                shift = size_c - size_h

                def val_by_header(keys: List[str]) -> Optional[str]:
                    hi = hidx(keys)
                    if hi is None:
                        return None
                    j = hi + shift
                    if 0 <= j < len(cell_texts):
                        v = (cell_texts[j] or "").strip()
                        return v or None
                    return None

                result["size"] = result["size"] or val_by_header(["size"])
                result["entry_price"] = result["entry_price"] or val_by_header(["entry price", "avg price"])
                result["mark_price"] = result["mark_price"] or val_by_header(["mark price"])
                result["upnl"] = result["upnl"] or val_by_header(["upnl", "unrealized", "unrealised"])        

        # Scoped fuzzy fallback
        def first_text_scoped(selectors: List[str]) -> Optional[str]:
            for sel in selectors:
                try:
                    loc = row.locator(sel).first
                    if loc.count() == 0:
                        continue
                    txt = (loc.inner_text(timeout=800) or "").strip()
                    if txt:
                        return txt
                except Exception:
                    continue
            return None
        if result["size"] is None:
            result["size"] = first_text_scoped(["td:has([class*='size'])"])
        if result["entry_price"] is None:
            result["entry_price"] = first_text_scoped(["td:has([class*='entry'])", "td:has([data-title*='Entry'])"])
        if result["mark_price"] is None:
            result["mark_price"] = first_text_scoped(["td:has([class*='mark'])", "td:has([data-title*='Mark'])"])
        if result["upnl"] is None:
            result["upnl"] = first_text_scoped(["td:has-text('UPNL')", "td:has([class*='pnl'])"])

        # Post-processing: derive has_position and side from size text
        size_txt = result.get("size") or ""
        if size_txt:
            # Detect numeric presence in size (e.g., +0.001 BTC or -0.001 BTC)
            if re.search(r"[0-9]", size_txt):
                result["has_position"] = True
            st = size_txt.strip().lower()
            if st.startswith("+"):
                result["side"] = "long"
            elif st.startswith("-"):
                result["side"] = "short"
            elif "long" in st:
                result["side"] = "long"
            elif "short" in st:
                result["side"] = "short"
        return result

    except Exception as e:
        log(f"Error extracting position data: {e}")
        save_dom_snapshot(page, label="extract_error")
        result["error"] = str(e)
        return result


def format_position_display(data: Dict[str, Any]) -> str:
    """Format position data for display"""
    def s(val: Any, fallback: str) -> str:
        return fallback if val is None else str(val)

    timestamp = s(data.get("timestamp"), "Unknown")
    size = s(data.get("size"), "No position")
    entry = s(data.get("entry_price"), "N/A")
    mark = s(data.get("mark_price"), "N/A")
    upnl = s(data.get("upnl"), "N/A")
    
    return f"""
╔══════════════════════════════════════╗
║           POSITION MONITOR           ║
╠══════════════════════════════════════╣
║ Time: {timestamp:<25} ║
║ Size: {size:<25} ║
║ Entry Price: {entry:<20} ║
║ Mark Price: {mark:<21} ║
║ UPNL: {upnl:<26} ║
╚══════════════════════════════════════╝
"""


def _activate_tab(page: Page, name_regex: str) -> bool:
    """Try to activate a tab by accessible role name regex. Returns True if a click was attempted or tab already active."""
    # Priority: explicit class-based tabs per provided HTML
    try:
        if re.search(r"positions", name_regex, re.I):
            cand = page.locator("css=div.tab.open-positions-tab").first
        elif re.search(r"orders", name_regex, re.I):
            cand = page.locator("css=div.tab.open-orders-tab").first
        else:
            cand = None
        if cand and cand.count() > 0:
            try:
                classes = cand.get_attribute("class") or ""
                if "active" not in classes:
                    try:
                        cand.scroll_into_view_if_needed(timeout=800)
                    except Exception:
                        pass
                    cand.click(timeout=1500)
                return True
            except Exception:
                pass
    except Exception:
        pass
    # Role-based fallback
    try:
        tab = page.get_by_role("tab", name=re.compile(name_regex, re.I)).first
        if tab and tab.count() > 0:
            sel = (tab.get_attribute("aria-selected") or "").lower()
            if sel == "false":
                try:
                    tab.scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                tab.click(timeout=1500)
                return True
            return True
    except Exception:
        pass
    # Fallbacks: try generic clickable with exact text
    try:
        # Try a few variants: 'Open Orders', 'Orders', possibly with count like 'Open Orders (2)'
        cand = page.locator("xpath=(//button|//div|//a|//span)[(contains(normalize-space(.), 'Open Orders') or contains(normalize-space(.), 'Orders')) and not(@disabled) and not(contains(@class,'disabled'))]").first
        if cand and cand.count() > 0 and cand.is_visible():
            try:
                cand.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            cand.click(timeout=1500)
            return True
    except Exception:
        pass
    return False


def extract_open_orders(page: Page) -> Dict[str, Any]:
    """Extract up to two open orders for the current instrument (BTCUSD page).

    Returns a dict: { 'orders': [ { 'symbol', 'side', 'price', 'qty', 'type' }, ... ], 'timestamp': ts }
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[str, Any] = {"orders": [], "timestamp": ts}

    try:
        # Switch to Open Orders tab
        clicked = _activate_tab(page, r"^Open\s*Orders$")
        if not clicked:
            # Try sibling fallback near Positions tab
            try:
                pos = page.get_by_text(re.compile(r"^Positions", re.I)).first
                if pos and pos.count() > 0:
                    container = pos.locator("xpath=ancestor::*[self::div or self::nav or self::section][1]")
                    alt = container.locator("xpath=.//*[contains(normalize-space(.), 'Open Orders') or contains(normalize-space(.), 'Orders')]").first
                    if alt and alt.count() > 0 and alt.is_visible():
                        try:
                            alt.scroll_into_view_if_needed(timeout=800)
                        except Exception:
                            pass
                        alt.click(timeout=1500)
                        clicked = True
            except Exception:
                pass

        # Small wait for table render
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

        # Prefer a table anchored under a visible "Open Orders" heading/label (case-insensitive)
        table = None
        try:
            anchor = page.locator("xpath=(//*[self::h1 or self::h2 or self::h3 or self::div or self::span][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'open orders')])[1]")
            if anchor and anchor.count() > 0 and anchor.is_visible():
                cand = anchor.locator("xpath=following::table[1]").first
                if cand and cand.count() > 0 and cand.is_visible():
                    table = cand
        except Exception:
            pass

        if table is None:
            # Fallback: find a likely Open Orders table by headers and row shape
            tables = page.locator("table")
            try:
                tcount = min(tables.count(), 10)
            except Exception:
                tcount = 0
            best_score = -1
            for i in range(tcount):
                t = tables.nth(i)
                if not t.is_visible():
                    continue
                try:
                    ths = t.locator("thead th")
                    headers = [t2.strip() for t2 in ths.all_text_contents()] if ths.count() > 0 else []
                except Exception:
                    headers = []
                hdr_l = ",".join(h.lower() for h in headers)
                score = 0
                if any(k in hdr_l for k in ("qty", "quantity", "size")):
                    score += 2
                if any(k in hdr_l for k in ("price", "limit")):
                    score += 2
                if any(k in hdr_l for k in ("type", "side")):
                    score += 1
                # ensure has at least some data rows
                rows_vis = t.locator("tbody tr")
                rc = rows_vis.count() if rows_vis else 0
                if rc >= 1:
                    score += 1
                # de-prioritize tables that contain only 'Load more'
                try:
                    body_txt = (t.inner_text(timeout=400) or "").lower()
                    if "load more" in body_txt and rc <= 1:
                        score -= 3
                except Exception:
                    pass
                if score > best_score:
                    best_score = score
                    table = t

        if table is None:
            save_dom_snapshot(page, label="open_orders_not_found")
            return out

        # Read headers
        try:
            ths = table.locator("thead th")
            headers = [t.strip() for t in ths.all_text_contents()] if ths.count() > 0 else []
        except Exception:
            headers = []
        header_map: Dict[str, int] = {(h or '').strip().lower(): i for i, h in enumerate(headers)}

        def hidx(keys: List[str]) -> Optional[int]:
            for k in keys:
                kk = (k or '').strip().lower()
                for h, i in header_map.items():
                    if kk and kk in h:
                        return i
            return None

        # Collect rows
        rows = table.locator("tbody tr")
        row_count = rows.count() if rows else 0
        if row_count == 0:
            # Sometimes rows may not be in tbody
            rows = table.locator("tr").filter(has=table.locator("td"))
            row_count = rows.count() if rows else 0

        orders: List[Dict[str, Any]] = []

        def row_has_cancel(r) -> bool:
            try:
                for sel in [
                    "xpath=.//button[contains(normalize-space(.), 'Cancel')]",
                    "xpath=.//*[contains(@aria-label,'Cancel') or contains(@title,'Cancel')][self::button or self::div or self::span]",
                    "css=button.cancel, div.cancel, span.cancel",
                ]:
                    cand = r.locator(sel).first
                    if cand and cand.count() > 0 and cand.is_visible():
                        return True
            except Exception:
                pass
            return False

        for r_i in range(min(row_count, 12)):  # safety cap
            row = rows.nth(r_i)
            # Gather cell texts and labels
            tds = row.locator("th, td")
            n = tds.count()
            cell_texts: List[str] = []
            cell_labels: List[str] = []
            for i in range(n):
                td = tds.nth(i)
                try:
                    txt = (td.inner_text(timeout=800) or '').strip()
                except Exception:
                    txt = ''
                cell_texts.append(txt)
                labels: List[str] = []
                for attr in ("data-title", "aria-label", "title", "data-column", "data-col"):
                    try:
                        v = td.get_attribute(attr)
                        if v:
                            labels.append(v)
                    except Exception:
                        pass
                cell_labels.append("|".join(l.lower() for l in labels))

            # One-time dump for open orders
            if RPA_DIAG and not getattr(extract_open_orders, "_row_dumped", False):
                log("🧾 Open Orders row dump: " + " | ".join([f"[{i}] {t}" for i, t in enumerate(cell_texts)]))
                setattr(extract_open_orders, "_row_dumped", True)

            # Skip non-order utility rows
            joined_lower = " ".join(cell_texts).lower()
            if "load more" in joined_lower or "no open orders" in joined_lower:
                continue
            # Optionally require a visible cancel control to classify as a live open order
            if ORDERS_REQUIRE_CANCEL and not row_has_cancel(row):
                continue

            # Build order record
            def get_by_header(keys: List[str]) -> Optional[str]:
                idx = hidx(keys)
                if idx is not None and 0 <= idx < len(cell_texts):
                    v = (cell_texts[idx] or '').strip()
                    if v:
                        return v
                # try attribute labels
                keyl = [k.lower() for k in keys]
                for j, lab in enumerate(cell_labels):
                    if any(k in lab for k in keyl):
                        v = (cell_texts[j] or '').strip()
                        if v:
                            return v
                return None

            symbol = get_by_header(["symbol", "instrument", "market", "contract"])
            # If symbol isn't explicitly present, infer from row text
            if not symbol:
                joined = " ".join(cell_texts)
                m = re.search(r"btc\s*usd|btcusd", joined, re.I)
                symbol = "BTCUSD" if m else None

            side = get_by_header(["side", "direction"]) or next(
                (t for t in cell_texts if re.search(r"\b(buy|sell|long|short)\b", t, re.I)), None
            )
            # Normalize cell texts for qty/price to collapse newlines and spaces
            def normalize_num_text(t: Optional[str]) -> Optional[str]:
                if not t:
                    return t
                t2 = re.sub(r"\s+", "", t)  # remove all whitespace
                return t2

            qty = get_by_header(["qty", "quantity"]) or get_by_header(["size", "amount"]) or next(
                (t for t in cell_texts if re.search(r"\b(btc|contracts?)\b", t, re.I)), None
            )
            qty = normalize_num_text(qty)

            price = get_by_header(["price", "limit price", "order price"]) or next(
                (t for t in cell_texts if re.search(r"\$|usd", t, re.I)), None
            )
            price = normalize_num_text(price)
            otype = get_by_header(["type", "order type"]) or None

            # Filter to BTCUSD when possible (we're on BTCUSD page)
            if symbol and not re.search(r"btc\s*usd|btcusd", symbol, re.I):
                continue

            # Derive side from qty if needed (sign or numeric value)
            def parse_qty_sign(q: Optional[str]) -> Optional[str]:
                if not q:
                    return None
                if re.search(r"^-", q.strip()):
                    return "short"
                if re.search(r"^\+", q.strip()):
                    return "long"
                # Numeric positive/negative fallback
                try:
                    qn_tmp = float(re.sub(r"[^0-9\-\.]+", "", q))
                    if qn_tmp > 0:
                        return "long"
                    if qn_tmp < 0:
                        return "short"
                except Exception:
                    pass
                return None

            derived_side = parse_qty_sign(qty)
            if not side and derived_side:
                side = derived_side

            # Compute size in BTC if qty looks like a lot count (e.g., -4 => 0.004 BTC)
            size_btc = None
            try:
                # Clean qty to number
                if qty and not re.search(r"btc", qty, re.I):
                    qn_val = float(re.sub(r"[^0-9\-\.]+", "", qty))
                    size_btc = f"{abs(qn_val)/1000:.3f} BTC"
            except Exception:
                size_btc = None

            order = {
                "symbol": symbol or "BTCUSD",
                "side": side,
                "qty": qty,
                "price": price,
                "type": otype,
                "size": size_btc,
            }
            # Only add if it looks like an actual order (has side and qty or price)
            if order["side"] or order["qty"] or order["price"]:
                orders.append(order)

        out["orders"] = orders[:2]
        # If we still think there are orders but the section text says none, clear them
        try:
            table_txt = (table.inner_text(timeout=400) or "").lower()
            if "no open orders" in table_txt and len(out["orders"]) > 0:
                out["orders"] = []
        except Exception:
            pass
        # Diagnostics when none detected
        if RPA_DIAG and len(out["orders"]) == 0:
            try:
                ths = table.locator("thead th")
                headers = [t.strip() for t in ths.all_text_contents()] if ths.count() > 0 else []
            except Exception:
                headers = []
            log(f"🔎 Open Orders: 0 rows parsed. Headers={headers}")
            save_dom_snapshot(page, label="open_orders_zero")
        return out
    except Exception as e:
        log(f"Error extracting open orders: {e}")
        save_dom_snapshot(page, label="open_orders_error")
        out["error"] = str(e)
        return out


def _select_order_side(page: Page, side: str) -> bool:
    """Select Buy|Long or Sell|Short in the order panel. Returns True if a click was attempted or already active.

    side: 'buy'/'long' or 'sell'/'short'
    """
    side_l = (side or "").strip().lower()
    want_buy = side_l in ("buy", "long")
    target_text = "Buy | Long" if want_buy else "Sell | Short"
    
    if RPA_DIAG:
        log(f"🔧 _select_order_side: looking for '{target_text}'")
    
    # Try multiple selectors for side selection
    side_selectors = [
        # Specific class-based selectors
        f"div.style--IHeIe.style--RvHLs:has-text('{target_text}')",
        # Text-based selectors
        f"div:has-text('{target_text}')",
        f"button:has-text('{target_text}')",
        f"span:has-text('{target_text}')",
        # Contains text selectors
        f"[role='button']:has-text('{target_text}')",
        f"[role='tab']:has-text('{target_text}')",
        # XPath fallback
        f"xpath=(//div|//button|//span)[contains(normalize-space(.), '{target_text}')]"
    ]
    
    for selector in side_selectors:
        try:
            if "xpath=" in selector:
                cand = page.locator(selector).first
            else:
                cand = page.locator(selector).first
                
            if cand and cand.count() > 0 and cand.is_visible():
                if RPA_DIAG:
                    log(f"🔧 Found side selector with: {selector}")
                    try:
                        classes = cand.get_attribute("class") or ""
                        log(f"🔧 Side element classes: {classes}")
                    except Exception:
                        pass
                
                try:
                    classes = cand.get_attribute("class") or ""
                    # If it looks already selected, skip clicking but return success
                    if "active" in classes or "selected" in classes:
                        if RPA_DIAG:
                            log(f"🔧 Side already selected (has active/selected class)")
                        return True
                        
                    cand.scroll_into_view_if_needed(timeout=800)
                    cand.click(timeout=1500)
                    if RPA_DIAG:
                        log(f"🔧 Clicked side selector successfully")
                    return True
                except Exception as e:
                    if RPA_DIAG:
                        log(f"🔧 Side selector click failed: {e}")
                    continue
        except Exception as e:
            if RPA_DIAG:
                log(f"🔧 Side selector {selector} failed: {e}")
            continue
    
    if RPA_DIAG:
        log(f"❌ No side selector found for '{target_text}'")
        # Take a diagnostic snapshot
        try:
            snap_filename = f"side_not_found_{target_text.replace(' | ', '_').lower()}_{int(time.time())}.html"
            if os.path.exists("debuuug"):
                snap_path = os.path.join("debuuug", snap_filename)
            else:
                snap_path = snap_filename
            with open(snap_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            log(f"🔍 Saved DOM snapshot: {snap_path}")
        except Exception:
            pass
    
    return True  # Return True even if we couldn't find it, to not block the flow


def _ensure_maker_only(page: Page, enabled: bool = True) -> bool:
    """Toggle 'Maker Only' to desired state. Returns True if state likely matches desired after operation.

    Heuristic: check aria-checked or CSS class containing 'active'/'selected'. If unknown, click once when enabling.
    """
    try:
        cand = page.locator("div.style--yKyoP:has-text('Maker Only')").first
        if not cand or cand.count() == 0:
            # broader fallback by text
            cand = page.locator("xpath=(//div|//button|//span)[contains(normalize-space(.), 'Maker Only')]").first
        if not cand or cand.count() == 0:
            return False

        def is_on() -> Optional[bool]:
            try:
                aria = (cand.get_attribute("aria-pressed") or cand.get_attribute("aria-checked") or "").lower()
                if aria in ("true", "false"):
                    return aria == "true"
            except Exception:
                pass
            try:
                cls = cand.get_attribute("class") or ""
                if any(tok in cls for tok in ("active", "selected", "on")):
                    return True
                if any(tok in cls for tok in ("off", "disabled")):
                    return False
            except Exception:
                pass
            return None

        state = is_on()
        if state is None:
            # If unknown, click once when enabling to bias to ON
            if enabled:
                try:
                    cand.scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                cand.click(timeout=1500)
                return True
            return True  # leave as-is when disabling and unknown
        if state != enabled:
            try:
                cand.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            cand.click(timeout=1500)
            time.sleep(0.1)
            return True
        return True
    except Exception:
        return False


def _select_order_type(page: Page, type_name: str = "Limit") -> bool:
    """Select order type (e.g., 'Limit' or 'Market'). Best-effort with multiple selectors."""
    try:
        # Click order type dropdown/area if needed, then click desired type
        # Try to find a visible control with current type text
        cur = page.locator("xpath=(//button|//div|//span)[contains(normalize-space(.), 'Limit') or contains(normalize-space(.), 'Market')]").first
        try:
            if cur and cur.count() > 0 and cur.is_visible():
                cur.click(timeout=800)
                time.sleep(0.1)
        except Exception:
            pass
        # Now click the desired type in the menu/list
        candidates = [
            f"button:has-text('{type_name}')",
            f"[role='menuitem']:has-text('{type_name}')",
            f"li:has-text('{type_name}')",
            f"div:has-text('{type_name}')",
        ]
        for sel in candidates:
            try:
                opt = page.locator(sel).first
                if opt and opt.count() > 0 and opt.is_visible():
                    opt.click(timeout=800)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def place_market_order(page: Page, side: str, lots: int, wait_s: float = 5.0) -> Dict[str, Any]:
    """Place a Market order via the UI to reduce/close a position.

    side: 'buy'/'long' or 'sell'/'short'
    lots: integer lot count
    """
    try:
        if lots <= 0:
            return {"ok": False, "error": "invalid_lots"}
        # Ensure trade form ready
        if not wait_for_trade_page_ready(page, timeout_s=12):
            return {"ok": False, "error": "trade_form_not_ready"}
        _select_order_side(page, side)
        _select_order_type(page, "Market")
        # Fill only quantity for market
        qty_ok = _fill_order_inputs(page, price="", lots=f"{lots}")
        # Even if price field missing, _fill_order_inputs may return False. Accept as long as qty is set.
        try:
            qty_input = page.locator("input[name='Quantity']").first
            if qty_input.count() == 0:
                # try alternative
                qty_input = page.locator("input[placeholder*='Quantity'], input[placeholder*='Size']").first
            qty_val = qty_input.input_value() if qty_input and qty_input.count() > 0 else ""
            if not re.search(r"\d", qty_val or ""):
                return {"ok": False, "error": "qty_not_filled"}
        except Exception:
            pass
        # Submit
        if not _click_submit(page, side):
            return {"ok": False, "error": "submit_not_clicked"}
        # Confirm if needed
        try:
            for sel in ["button:has-text('Confirm')", "button:has-text('Yes')", "[data-testid*='confirm']"]:
                btn = page.locator(sel).first
                if btn and btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=1000)
                    break
        except Exception:
            pass
        time.sleep(wait_s)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_lots_from_size(size_text: Optional[str]) -> Optional[int]:
    """Convert a size like '+0.001 BTC' to integer lots assuming 1 lot = 0.001 BTC."""
    if not size_text:
        return None
    try:
        m = re.search(r"([+-]?[0-9]*\.?[0-9]+)", size_text)
        if not m:
            return None
        btc = abs(float(m.group(1)))
        lots = int(round(btc / 0.001))
        return max(lots, 0)
    except Exception:
        return None


def close_position_market(page: Page, wait_s: float = 5.0) -> Dict[str, Any]:
    """Close current BTCUSD position using a Market order opposite to position side."""
    try:
        pos = extract_position_data(page)
        size_txt = pos.get("size") or ""
        if not re.search(r"[0-9]", size_txt):
            return {"success": True, "action": "close_position_market", "note": "no_position"}
        lots = _parse_lots_from_size(size_txt) or 1
        side = "sell" if "+" in size_txt else ("buy" if "-" in size_txt else "sell")
        # Cancel open orders first to avoid conflicts
        try:
            cancel_open_orders(page)
        except Exception:
            pass
        res = place_market_order(page, side, lots)
        if not res.get("ok"):
            return {"success": False, "error": res.get("error", "market_order_failed")}
        # Verify position cleared
        time.sleep(wait_s)
        pos2 = extract_position_data(page)
        size2 = pos2.get("size") or ""
        cleared = not re.search(r"[0-9]", size2)
        return {"success": cleared, "action": "close_position_market", "lots": lots}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fill_order_inputs(page: Page, price: str, lots: str) -> bool:
    """Fill limit price and lot size inputs. Returns True on best-effort success."""
    ok = True
    if RPA_DIAG:
        log(f"🔧 _fill_order_inputs: price={price}, lots={lots}")
    
    # First, try to dismiss any overlays or dropdowns that might be interfering
    try:
        # Press Escape to close any open dropdowns
        page.keyboard.press("Escape")
        time.sleep(0.2)
        # Click somewhere neutral to dismiss overlays
        page.locator("body").click(timeout=500)
        time.sleep(0.2)
    except Exception:
        pass
    
    # Try multiple price input selectors
    price_selectors = [
        "input[name='orderPrice']",
        "input[placeholder*='Price']",
        "input[placeholder*='price']", 
        "input[data-testid*='price']",
        "input[type='number'][placeholder*='Price']",
        "input[type='text'][placeholder*='Price']"
    ]
    
    price_filled = False
    for selector in price_selectors:
        try:
            price_in = page.locator(selector).first
            if price_in and price_in.count() > 0 and price_in.is_visible():
                if RPA_DIAG:
                    log(f"🔧 Found price input with selector: {selector}")
                try:
                    # Try force clicking by using force=True to bypass intercepts
                    price_in.click(force=True, timeout=1000)
                    # Multiple clearing strategies
                    try:
                        price_in.fill("")
                    except Exception:
                        pass
                    try:
                        price_in.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass
                    # Type the value
                    price_in.type(str(price), delay=50)
                    # Tab to next field
                    page.keyboard.press("Tab")
                    price_filled = True
                    if RPA_DIAG:
                        log(f"🔧 Price input filled successfully using force click")
                    break
                except Exception as e:
                    if RPA_DIAG:
                        log(f"🔧 Price selector {selector} failed with force: {e}")
                    # Try JavaScript approach as fallback
                    try:
                        page.evaluate(f"document.querySelector('{selector}').value = '{price}'")
                        page.evaluate(f"document.querySelector('{selector}').dispatchEvent(new Event('input', {{bubbles: true}}))")
                        price_filled = True
                        if RPA_DIAG:
                            log(f"🔧 Price input filled using JavaScript")
                        break
                    except Exception as js_e:
                        if RPA_DIAG:
                            log(f"🔧 JavaScript fallback failed: {js_e}")
                        continue
        except Exception as e:
            if RPA_DIAG:
                log(f"🔧 Price selector {selector} failed: {e}")
            continue
    
    if not price_filled:
        if RPA_DIAG:
            log("❌ No price input found")
        ok = False

    # Try multiple quantity input selectors
    qty_selectors = [
        "input[name='Quantity']",
        "input[name='quantity']",
        "input[placeholder*='Quantity']",
        "input[placeholder*='quantity']",
        "input[placeholder*='Size']",
        "input[placeholder*='size']",
        "input[data-testid*='quantity']",
        "input[data-testid*='size']"
    ]
    
    qty_filled = False
    for selector in qty_selectors:
        try:
            qty_in = page.locator(selector).first
            if qty_in and qty_in.count() > 0 and qty_in.is_visible():
                if RPA_DIAG:
                    log(f"🔧 Found quantity input with selector: {selector}")
                try:
                    # Try force clicking by using force=True to bypass intercepts
                    qty_in.click(force=True, timeout=1000)
                    # Multiple clearing strategies
                    try:
                        qty_in.fill("")
                    except Exception:
                        pass
                    try:
                        qty_in.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass
                    # Type the value
                    qty_in.type(str(lots), delay=50)
                    # Tab to next field
                    page.keyboard.press("Tab")
                    qty_filled = True
                    if RPA_DIAG:
                        log(f"🔧 Quantity input filled successfully using force click")
                    break
                except Exception as e:
                    if RPA_DIAG:
                        log(f"🔧 Quantity selector {selector} failed with force: {e}")
                    # Try JavaScript approach as fallback
                    try:
                        page.evaluate(f"document.querySelector('{selector}').value = '{lots}'")
                        page.evaluate(f"document.querySelector('{selector}').dispatchEvent(new Event('input', {{bubbles: true}}))")
                        qty_filled = True
                        if RPA_DIAG:
                            log(f"🔧 Quantity input filled using JavaScript")
                        break
                    except Exception as js_e:
                        if RPA_DIAG:
                            log(f"🔧 JavaScript fallback failed: {js_e}")
                        continue
        except Exception as e:
            if RPA_DIAG:
                log(f"🔧 Quantity selector {selector} failed: {e}")
            continue
    
    if not qty_filled:
        if RPA_DIAG:
            log("❌ No quantity input found")
        ok = False

    return ok


def _click_submit(page: Page, side: str) -> bool:
    """Click the Buy or Sell submit button. Returns True if click attempted."""
    side_l = (side or "").strip().lower()
    want_buy = side_l in ("buy", "long")
    label = "Buy" if want_buy else "Sell"
    
    if RPA_DIAG:
        log(f"🔧 _click_submit: looking for {label} button")
    
    # Try multiple button selectors in order of preference
    button_selectors = [
        # Class-based selectors (most specific)
        f"div.{label.lower()}",
        f"button.{label.lower()}",
        f"div.{label.lower()}-button",
        f"button.{label.lower()}-button",
        # Text-based selectors
        f"button:has-text('{label}')",
        f"div:has-text('{label}')",
        f"[role='button']:has-text('{label}')",
        # Data attribute selectors
        f"[data-testid*='{label.lower()}']",
        f"[data-cy*='{label.lower()}']",
        # Broader selectors
        f"xpath=(//button|//div)[contains(@class, '{label.lower()}') or contains(normalize-space(.), '{label}')]"
    ]
    
    for selector in button_selectors:
        try:
            if "xpath=" in selector:
                btn = page.locator(selector).first
            else:
                btn = page.locator(selector).first
                
            if btn and btn.count() > 0 and btn.is_visible():
                if RPA_DIAG:
                    log(f"🔧 Found {label} button with selector: {selector}")
                    try:
                        btn_text = btn.inner_text(timeout=500)
                        log(f"🔧 Button text: '{btn_text}'")
                    except Exception:
                        pass
                try:
                    btn.scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                btn.click(timeout=2000)
                if RPA_DIAG:
                    log(f"🔧 Clicked {label} button successfully")
                return True
        except Exception as e:
            if RPA_DIAG:
                log(f"🔧 {label} selector {selector} failed: {e}")
            continue
    
    if RPA_DIAG:
        log(f"❌ No {label} button found with any selector")
        # Take a diagnostic snapshot to see what's available
        try:
            snap_filename = f"submit_button_not_found_{label.lower()}_{int(time.time())}.html"
            page.content()
            if os.path.exists("debuuug"):
                snap_path = os.path.join("debuuug", snap_filename)
            else:
                snap_path = snap_filename
            with open(snap_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            log(f"🔍 Saved DOM snapshot: {snap_path}")
        except Exception:
            pass
    
    return False


# Readiness and verification helpers
def wait_for_trade_page_ready(page: Page, timeout_s: float = 12.0) -> bool:
    """Wait until the limit order form appears (price & quantity inputs or side toggles)."""
    if RPA_DIAG:
        log(f"🔧 wait_for_trade_page_ready: timeout={timeout_s}s")
    
    deadline = time.time() + max(1.0, timeout_s)
    while time.time() < deadline:
        try:
            # Check for order price and quantity inputs
            price_input = page.locator("input[name='orderPrice']").first
            qty_input = page.locator("input[name='Quantity']").first
            if price_input.count() > 0 and qty_input.count() > 0:
                if RPA_DIAG:
                    log(f"🔧 Trade page ready: found orderPrice and Quantity inputs")
                return True
        except Exception:
            pass
        
        try:
            # Check for side toggles as alternative
            side_toggle = page.locator("xpath=(//div|//button|//span)[contains(normalize-space(.), 'Buy | Long') or contains(normalize-space(.), 'Sell | Short')]").first
            if side_toggle.count() > 0:
                if RPA_DIAG:
                    log(f"🔧 Trade page ready: found side toggle")
                return True
        except Exception:
            pass
        
        # Try alternative input selectors
        try:
            alt_price = page.locator("input[placeholder*='Price'], input[placeholder*='price']").first
            alt_qty = page.locator("input[placeholder*='Quantity'], input[placeholder*='quantity'], input[placeholder*='Size'], input[placeholder*='size']").first
            if alt_price.count() > 0 and alt_qty.count() > 0:
                if RPA_DIAG:
                    log(f"🔧 Trade page ready: found alternative price/qty inputs")
                return True
        except Exception:
            pass
            
        time.sleep(0.3)
    
    if RPA_DIAG:
        log(f"❌ Trade page not ready after {timeout_s}s")
        # Take diagnostic snapshot
        try:
            snap_filename = f"trade_page_not_ready_{int(time.time())}.html"
            if os.path.exists("debuuug"):
                snap_path = os.path.join("debuuug", snap_filename)
            else:
                snap_path = snap_filename
            with open(snap_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            log(f"🔍 Saved DOM snapshot: {snap_path}")
        except Exception:
            pass
    
    return False


def open_orders_ready(page: Page, timeout_s: float = 8.0) -> bool:
    """Ensure the Open Orders table or empty-state text is visible."""
    try:
        _activate_tab(page, r"^Open\s*Orders$")
    except Exception:
        pass
    deadline = time.time() + max(1.0, timeout_s)
    while time.time() < deadline:
        try:
            # Either a table with rows or an empty-state text counts as ready
            tables = page.locator("table")
            if tables.count() > 0:
                # some instances render instantly with no rows
                return True
        except Exception:
            pass
        try:
            if page.get_by_text(re.compile(r"no\s+open\s+orders", re.I)).first.count() > 0:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _orders_signature(orders: List[Dict[str, Any]]) -> List[str]:
    """Create a simple signature list for open orders to detect changes."""
    sigs: List[str] = []
    for o in orders or []:
        sig = f"{(o.get('side') or '').lower()}|{o.get('price') or ''}|{o.get('qty') or ''}|{o.get('symbol') or ''}"
        sigs.append(sig)
    sigs.sort()
    return sigs


def place_limit_order(page: Page, side: str, price: float, lots: int, maker_only: bool = True, wait_s: float = 10.0) -> Dict[str, Any]:
    """Place a maker-only limit order via the UI and verify by matching side/price in Open Orders afterwards.

    Returns dict: { 'ok': bool, 'after': [...], 'error': optional }
    """
    try:
        # Ensure trade form is ready
        ready = wait_for_trade_page_ready(page, timeout_s=15)
        if not ready:
            return {"ok": False, "after": [], "error": "trade_form_not_ready"}
        
        # First, ensure we're in "Limit" order mode (not Stop Limit, Trailing Stop, etc.)
        if RPA_DIAG:
            log(f"🔧 Ensuring Limit order type is selected")
        try:
            # Look for order type selector and click "Limit"
            limit_options = [
                "button:has-text('Limit')",
                "[data-testid*='Limit']:not([data-testid*='Stop'])",
                "li:has-text('Limit')",
                ".order-type button:has-text('Limit')",
                "[role='menuitem']:has-text('Limit')"
            ]
            for selector in limit_options:
                try:
                    limit_btn = page.locator(selector).first
                    if limit_btn and limit_btn.count() > 0 and limit_btn.is_visible():
                        limit_btn.click(timeout=1000)
                        if RPA_DIAG:
                            log(f"🔧 Selected Limit order type")
                        time.sleep(0.3)
                        break
                except Exception:
                    continue
        except Exception:
            pass
        
        # Ensure we are on the order form (Positions tab is fine; the form is side panel)
        _select_order_side(page, side)
        if maker_only:
            _ensure_maker_only(page, True)
        # Fill inputs
        inputs_ok = _fill_order_inputs(page, f"{price}", f"{lots}")
        if not inputs_ok:
            return {"ok": False, "after": [], "error": "inputs_not_filled"}
        
        # Verify the inputs actually contain our values
        if RPA_DIAG:
            log(f"🔧 Verifying input values...")
            try:
                price_input = page.locator("input[name='orderPrice']").first
                if price_input.count() > 0:
                    actual_price = price_input.input_value()
                    log(f"🔧 Price input value: '{actual_price}' (expected: {price})")
            except Exception as e:
                log(f"🔧 Could not read price input: {e}")
            
            try:
                qty_input = page.locator("input[name='Quantity']").first
                if qty_input.count() > 0:
                    actual_qty = qty_input.input_value()
                    log(f"🔧 Quantity input value: '{actual_qty}' (expected: {lots})")
            except Exception as e:
                log(f"🔧 Could not read quantity input: {e}")
                
        # Submit
        clicked = _click_submit(page, side)
        if not clicked:
            return {"ok": False, "after": [], "error": "submit_button_not_found"}
        
        # Check for any confirmation dialogs or error messages
        if RPA_DIAG:
            log(f"🔧 Checking for dialogs or error messages after submit...")
        time.sleep(0.5)
        
        # Look for confirmation dialogs and click "Confirm" if present
        try:
            confirm_selectors = [
                "button:has-text('Confirm')",
                "button:has-text('Yes')",
                "button:has-text('OK')",
                "[data-testid*='confirm']",
                ".confirm-button",
                ".modal button:has-text('Confirm')"
            ]
            for selector in confirm_selectors:
                try:
                    confirm_btn = page.locator(selector).first
                    if confirm_btn and confirm_btn.count() > 0 and confirm_btn.is_visible():
                        confirm_btn.click(timeout=1000)
                        if RPA_DIAG:
                            log(f"🔧 Clicked confirmation dialog")
                        time.sleep(0.3)
                        break
                except Exception:
                    continue
        except Exception:
            pass
        
        # Look for error messages
        try:
            error_selectors = [
                ".error-message",
                ".alert-error",
                "[role='alert']",
                ".notification.error",
                ".toast.error",
                "*:has-text('Error')",
                "*:has-text('Invalid')",
                "*:has-text('Failed')"
            ]
            for selector in error_selectors:
                try:
                    error_elem = page.locator(selector).first
                    if error_elem and error_elem.count() > 0 and error_elem.is_visible():
                        error_text = error_elem.inner_text(timeout=500)
                        if RPA_DIAG:
                            log(f"🔧 ⚠️ Found error message: {error_text}")
                except Exception:
                    continue
        except Exception:
            pass
        
        # Wait longer for order to process
        time.sleep(1.0)
        open_orders_ready(page, timeout_s=8)
        # Poll for an order that matches side and price
        target_side = ("long" if side.lower() in ("buy", "long") else "short")
        def _norm_num(s: str) -> str:
            return re.sub(r"[^0-9\.-]", "", s or "")
        target_price_norm = _norm_num(str(price))
        try:
            target_price_val = float(target_price_norm)
        except Exception:
            target_price_val = None
        
        if RPA_DIAG:
            log(f"🔧 Verification: looking for side='{target_side}' price={price} (norm={target_price_norm})")
        
        deadline = time.time() + max(2.0, wait_s)
        after = []
        while time.time() < deadline:
            try:
                info = extract_open_orders(page)
                after = info.get("orders", [])
                
                if RPA_DIAG:
                    log(f"🔧 Verification: found {len(after)} orders in Open Orders")
                    for i, o in enumerate(after):
                        log(f"🔧   [{i}] side='{o.get('side')}' price='{o.get('price')}' qty='{o.get('qty')}'")
                
                # match by side text and price - BOTH must match
                found = False
                for o in after:
                    s = (o.get("side") or "").strip().lower()
                    p = (o.get("price") or "")
                    p_norm = _norm_num(p)
                    
                    # First check if side matches
                    side_matches = target_side in s
                    price_matches = False
                    
                    if side_matches:
                        # Check price match - must be exact or within tolerance
                        if target_price_norm and p_norm:
                            if target_price_norm in p_norm or p_norm in target_price_norm:
                                price_matches = True
                            else:
                                try:
                                    pv = float(p_norm)
                                    if target_price_val is not None and abs(pv - target_price_val) <= 2.0:
                                        price_matches = True
                                except Exception:
                                    pass
                    
                    if side_matches and price_matches:
                        if RPA_DIAG:
                            log(f"🔧 ✅ Found matching order: side='{s}' price='{p}' (matches target)")
                        found = True
                        break
                    elif side_matches and RPA_DIAG:
                        log(f"🔧 ❌ Side matches but price doesn't: side='{s}' price='{p}' vs target={price}")
                
                if found:
                    return {"ok": True, "after": after}
            except Exception as e:
                if RPA_DIAG:
                    log(f"🔧 Verification error: {e}")
            time.sleep(0.5)
        # If not matched, return what we saw and log if diagnostic mode
        if RPA_DIAG:
            try:
                for i, o in enumerate(after, 1):
                    log(f"🔎 After[{i}] side={o.get('side')} price={o.get('price')} qty={o.get('qty')} size={o.get('size')}")
            except Exception:
                pass
        return {"ok": False, "after": after, "error": "order_not_visible_in_open_orders"}
    except Exception as e:
        return {"ok": False, "after": [], "error": str(e)}


def cancel_open_orders(page: Page, side: Optional[str] = None, price_substr: Optional[str] = None, max_to_cancel: Optional[int] = None, wait_s: float = 3.0) -> Dict[str, Any]:
    """Cancel open orders optionally filtered by side ('buy'/'sell'/'long'/'short') and/or price substring.

    Returns: { 'ok': bool, 'before': [...], 'after': [...], 'cancelled': int }
    """
    side_l: Optional[str] = (side or None)
    if side_l:
        side_l = side_l.strip().lower()
        if side_l in ("long", "buy"):
            side_l = "buy"
        elif side_l in ("short", "sell"):
            side_l = "sell"
    # Snapshot before
    before_info = extract_open_orders(page)
    before = before_info.get("orders", [])
    base_sig = _orders_signature(before)

    cancelled = 0
    try:
        # Ensure on Open Orders tab
        _activate_tab(page, r"^Open\s*Orders$")
        page.wait_for_timeout(200)

        # Locate the best table again
        tables = page.locator("table")
        tcount = min(tables.count(), 10) if tables else 0
        target_table = None
        best = -1
        for i in range(tcount):
            t = tables.nth(i)
            if not t.is_visible():
                continue
            try:
                hdr = (t.locator("thead").inner_text(timeout=400) or "").lower()
            except Exception:
                hdr = ""
            score = 0
            if any(k in hdr for k in ("qty", "quantity", "size")):
                score += 2
            if any(k in hdr for k in ("price", "limit")):
                score += 2
            if any(k in hdr for k in ("type", "side")):
                score += 1
            if score > best:
                best = score
                target_table = t
        if not target_table:
            return {"ok": False, "before": before, "after": before, "cancelled": 0, "error": "orders_table_not_found"}

        rows = target_table.locator("tbody tr")
        rc = rows.count() if rows else 0

        for r_i in range(rc):
            if max_to_cancel is not None and cancelled >= max_to_cancel:
                break
            row = rows.nth(r_i)
            try:
                row_txt = (row.inner_text(timeout=600) or "").strip()
            except Exception:
                row_txt = ""
            
            if RPA_DIAG:
                log(f"🔧 Cancel: checking row {r_i}: '{row_txt[:100]}...'")
            
            # Side filter
            if side_l:
                has_buy = re.search(r"\b(buy|long)\b", row_txt, re.I) is not None
                has_sell = re.search(r"\b(sell|short)\b", row_txt, re.I) is not None
                
                # Also check for negative quantity (indicates short) or positive (indicates long)
                # Handle both "-1" and "-\n1" patterns and look specifically for the quantity field
                if "-" in row_txt and "1" in row_txt:
                    # Look for patterns that suggest negative quantity
                    if re.search(r"-\s*\n?\s*1", row_txt) or re.search(r"^\s*-\s*1", row_txt, re.M):
                        has_sell = True  # This looks like a -1 quantity = short position
                        if RPA_DIAG:
                            log(f"🔧 Cancel: detected short from negative quantity pattern in row")
                
                # Also look for positive standalone numbers that could be quantities (avoid prices/dates)
                standalone_nums = re.findall(r"(?:^|\n)\s*([+-]?\d{1,3})\s*(?:\n|$)", row_txt, re.M)
                for num_str in standalone_nums:
                    try:
                        num_val = int(num_str.strip())
                        if num_val > 0 and num_val < 1000:  # Likely a quantity, not a price
                            has_buy = True
                            if RPA_DIAG:
                                log(f"🔧 Cancel: detected long from standalone positive qty: {num_val}")
                        elif num_val < 0:
                            has_sell = True
                            if RPA_DIAG:
                                log(f"🔧 Cancel: detected short from standalone negative qty: {num_val}")
                    except ValueError:
                        continue
                
                if RPA_DIAG:
                    log(f"🔧 Cancel: row {r_i} side detection - has_buy={has_buy}, has_sell={has_sell}, looking_for={side_l}")
                
                if side_l == "buy" and not has_buy:
                    if RPA_DIAG:
                        log(f"🔧 Cancel: row {r_i} skipped - looking for buy/long but not found")
                    continue
                if side_l == "sell" and not has_sell:
                    if RPA_DIAG:
                        log(f"🔧 Cancel: row {r_i} skipped - looking for sell/short but not found")
                    continue
            
            # Price filter
            if price_substr and (price_substr not in row_txt):
                if RPA_DIAG:
                    log(f"🔧 Cancel: row {r_i} skipped - price substr '{price_substr}' not found")
                continue

            if RPA_DIAG:
                log(f"🔧 Cancel: row {r_i} matches filters, looking for cancel button")

            # Find cancel control within the row
            cancel_btn = None
            # Use the specific data-testid for cancel buttons
            cancel_selectors = [
                "button[data-testid='HoldingsCancelButton']",
                "[data-testid='HoldingsCancelButton']",
                "xpath=.//button[@data-testid='HoldingsCancelButton']",
                "xpath=.//button[contains(normalize-space(.), '✕')]",
                "xpath=.//div[contains(normalize-space(.), '✕')]",
                "xpath=.//span[contains(normalize-space(.), '✕')]",
                "xpath=.//*[contains(normalize-space(.), '✕')]",
                "xpath=.//button[contains(normalize-space(.), 'Cancel')]",
                "xpath=.//*[contains(@aria-label,'Cancel') or contains(@title,'Cancel')][self::button or self::div or self::span]",
                "css=button.cancel, div.cancel, span.cancel",
            ]
            
            for sel in cancel_selectors:
                try:
                    cand = row.locator(sel).first
                    if cand and cand.count() > 0 and cand.is_visible():
                        cancel_btn = cand
                        if RPA_DIAG:
                            log(f"🔧 Cancel: found button with selector: {sel}")
                        break
                except Exception as e:
                    if RPA_DIAG:
                        log(f"🔧 Cancel: selector {sel} failed: {e}")
                    continue
                    
            if not cancel_btn:
                # As a last resort, try last clickable in the row
                try:
                    candidates = row.locator("xpath=.//button|.//a|.//div[@role='button']")
                    if candidates.count() > 0:
                        cancel_btn = candidates.nth(candidates.count() - 1)
                        if RPA_DIAG:
                            log(f"🔧 Cancel: using last clickable element as fallback")
                except Exception:
                    pass
                    
            if cancel_btn:
                if RPA_DIAG:
                    log(f"🔧 Cancel: attempting to click cancel button for row {r_i}")
                try:
                    cancel_btn.scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                try:
                    cancel_btn.click(timeout=1500)
                    cancelled += 1
                    if RPA_DIAG:
                        log(f"🔧 Cancel: successfully cancelled order {r_i}")
                    time.sleep(0.2)
                except Exception as e:
                    if RPA_DIAG:
                        log(f"🔧 Cancel: click failed for row {r_i}: {e}")
                    continue
            else:
                if RPA_DIAG:
                    log(f"🔧 Cancel: no cancel button found for row {r_i}")
                    # Save a diagnostic snapshot of this row
                    try:
                        row_html = row.inner_html(timeout=500)
                        snap_filename = f"cancel_no_button_row_{r_i}_{int(time.time())}.html"
                        if os.path.exists("debuuug"):
                            snap_path = os.path.join("debuuug", snap_filename)
                        else:
                            snap_path = snap_filename
                        with open(snap_path, "w", encoding="utf-8") as f:
                            f.write(f"<!-- Row {r_i} text: {row_txt} -->\n{row_html}")
                        log(f"🔍 Saved row HTML: {snap_path}")
                    except Exception:
                        pass

        # Verify change
        if RPA_DIAG:
            log(f"🔧 Cancel: verifying changes (cancelled={cancelled}, wait_s={wait_s})")
        deadline = time.time() + max(1.0, wait_s)
        after = before
        verification_attempts = 0
        while time.time() < deadline:
            verification_attempts += 1
            try:
                if RPA_DIAG:
                    log(f"🔧 Cancel: verification attempt {verification_attempts}")
                info2 = extract_open_orders(page)
                after = info2.get("orders", [])
                new_sig = _orders_signature(after)
                if new_sig != base_sig:
                    if RPA_DIAG:
                        log(f"🔧 Cancel: orders changed! before={len(before)} after={len(after)}")
                    break
                elif RPA_DIAG:
                    log(f"🔧 Cancel: orders unchanged (still {len(after)} orders)")
            except Exception as e:
                if RPA_DIAG:
                    log(f"🔧 Cancel: verification error: {e}")
                # Don't break, just continue trying
            time.sleep(0.5)
        
        if RPA_DIAG:
            log(f"🔧 Cancel: verification completed after {verification_attempts} attempts")
        
        ok = _orders_signature(after) != base_sig or cancelled > 0
        # Return to Positions
        try:
            _activate_tab(page, r"^Positions$")
        except Exception:
            pass
        return {"ok": ok, "before": before, "after": after, "cancelled": cancelled}
    except Exception as e:
        return {"ok": False, "before": before, "after": before, "cancelled": cancelled, "error": str(e)}


# --- Simple wrappers requested for testing ---
def create_long_order(page: Page, price: float, lots: int, maker_only: bool = True) -> Dict[str, Any]:
    """Create a Buy/Long limit order with the given price and lot count."""
    return place_limit_order(page, side="buy", price=price, lots=lots, maker_only=maker_only)


def create_short_order(page: Page, price: float, lots: int, maker_only: bool = True) -> Dict[str, Any]:
    """Create a Sell/Short limit order with the given price and lot count."""
    return place_limit_order(page, side="sell", price=price, lots=lots, maker_only=maker_only)


def analyze_strategy_state(page: Page) -> Dict[str, Any]:
    """Analyze current position and orders to determine strategy state.
    
    Returns: {
        'position': {...},
        'orders': [...],
        'state': 'seed_setup|seed_active|averaging|ready_for_flip',
        'missing_orders': [...],
        'next_action': 'place_initial|place_missing|monitor|error'
    }
    """
    result = {
        'position': None,
        'orders': [],
        'state': 'unknown',
        'missing_orders': [],
        'next_action': 'error',
        'errors': []
    }
    
    try:
        # Extract current position
        pos_info = extract_position_data(page)
        result['position'] = pos_info
        
        # Extract current orders
        orders_info = extract_open_orders(page)
        result['orders'] = orders_info.get('orders', [])
        
        if RPA_DIAG:
            log(f"🔍 State Analysis: position={pos_info}, orders={len(result['orders'])}")
        
        # Determine strategy state based on position and orders
        has_position = pos_info and pos_info.get('size') and '0.001' in pos_info.get('size', '')
        order_count = len(result['orders'])
        
        if not has_position and order_count == 0:
            # No position, no orders - need to create initial position
            result['state'] = 'no_position'
            result['next_action'] = 'create_initial_position'
        
        elif has_position and order_count == 0:
            # Position exists, no orders - seed setup needed
            result['state'] = 'seed_setup'
            result['next_action'] = 'place_initial'
            result['missing_orders'] = ['avg', 'tp']
        
        elif has_position and order_count == 1:
            # Position + 1 order - missing one order
            result['state'] = 'partial_setup'
            result['next_action'] = 'place_missing'
            
            # Determine which order is missing
            existing_order = result['orders'][0]
            order_side = existing_order.get('side', '').lower()
            position_direction = _infer_position_direction_from_position(pos_info)
            
            if position_direction in order_side:
                # Existing order is same direction (AVG) - missing TP
                result['missing_orders'] = ['tp']
            else:
                # Existing order is opposite direction (TP) - missing AVG
                result['missing_orders'] = ['avg']
        
        elif has_position and order_count == 2:
            # Position + 2 orders - fully set up, ready to monitor
            result['state'] = 'seed_active'
            result['next_action'] = 'monitor'
        
        else:
            # Unexpected state
            result['state'] = 'unexpected'
            result['next_action'] = 'error'
            result['errors'].append(f"Unexpected state: position={bool(has_position)}, orders={order_count}")
        
        return result
        
    except Exception as e:
        result['errors'].append(f"State analysis error: {str(e)}")
        return result


def _infer_position_direction_from_position(pos_info: Dict) -> str:
    """Infer position direction (long/short) from position data."""
    if not pos_info:
        return "unknown"
    
    position_size = pos_info.get('size', '')
    position_side = pos_info.get('side', 'NONE')
    
    if "+" in position_size or position_side.lower() in ("long", "buy"):
        return "long"
    elif "-" in position_size or position_side.lower() in ("short", "sell"):
        return "short"
    else:
        return "unknown"


def calculate_strategy_prices(position_info: Dict, position_lots: int = 1) -> Dict[str, float]:
    """Calculate AVG and TP prices based on current position.
    
    Args:
        position_info: Position data from extract_position_data
        position_lots: Current position size in lots (1, 3, 9, etc.)
    
    Returns: {
        'avg_price': float,
        'tp_price': float,
        'avg_lots': int,
        'tp_lots': int
    }
    """
    # Extract average price
    avg_price_str = position_info.get('avg_price', '') or ''
    
    # Try to parse avg price or estimate from position data
    try:
        avg_price = float(re.sub(r"[^0-9\.-]", "", avg_price_str))
    except (ValueError, TypeError):
        # Estimate from position data if avg_price not available
        all_pos_text = str(position_info)
        price_matches = re.findall(r"\b(\d+\.?\d*)\b", all_pos_text)
        potential_prices = [float(m) for m in price_matches if 10000 <= float(m) <= 200000]
        avg_price = potential_prices[0] if potential_prices else 117000
    
    # Determine position direction
    is_long = _infer_position_direction_from_position(position_info) == "long"
    
    # Calculate distances based on position size (strategy progression)
    if position_lots == 1:
        # Seed phase: 1 lot
        tp_distance = 300
        avg_distance = 750
        avg_lots = 2
        tp_lots = 2
    elif position_lots == 3:
        # First averaging: 3 lots
        tp_distance = 200
        avg_distance = 500  # From first avg price, not current avg
        avg_lots = 6
        tp_lots = 4
    elif position_lots >= 9:
        # Final averaging: 9+ lots
        tp_distance = 100
        avg_distance = 300
        avg_lots = position_lots * 2
        tp_lots = position_lots + 1
    else:
        # Default case
        tp_distance = 300
        avg_distance = 750
        avg_lots = 2
        tp_lots = 2
    
    # Calculate actual prices
    if is_long:
        avg_price_target = avg_price - avg_distance  # Long: average down
        tp_price_target = avg_price + tp_distance    # Long: take profit up
    else:
        avg_price_target = avg_price + avg_distance  # Short: average up
        tp_price_target = avg_price - tp_distance    # Short: take profit down
    
    return {
        'avg_price': avg_price_target,
        'tp_price': tp_price_target,
        'avg_lots': avg_lots,
        'tp_lots': tp_lots,
        'current_avg': avg_price,
        'position_direction': 'long' if is_long else 'short'
    }


def detect_order_fill(before_orders: List[Dict], after_orders: List[Dict]) -> Dict[str, Any]:
    """Detect which order was filled by comparing before/after order lists.
    
    Returns: {
        'filled': bool,
        'fill_type': 'avg'|'tp'|'unknown',
        'filled_order': {...},
        'remaining_orders': [...]
    }
    """
    result = {
        'filled': False,
        'fill_type': 'unknown',
        'filled_order': None,
        'remaining_orders': after_orders
    }
    
    if len(before_orders) <= len(after_orders):
        return result  # No fill detected
    
    # Find which order is missing
    before_sigs = {f"{o.get('side')}_{o.get('price')}_{o.get('qty')}" for o in before_orders}
    after_sigs = {f"{o.get('side')}_{o.get('price')}_{o.get('qty')}" for o in after_orders}
    
    missing_sigs = before_sigs - after_sigs
    
    if len(missing_sigs) == 1:
        # Find the filled order
        missing_sig = list(missing_sigs)[0]
        for order in before_orders:
            order_sig = f"{order.get('side')}_{order.get('price')}_{order.get('qty')}"
            if order_sig == missing_sig:
                result['filled'] = True
                result['filled_order'] = order
                
                # Determine fill type based on order characteristics
                order_side = order.get('side', '').lower()
                
                # This is a simplified heuristic - in practice you'd need more context
                # AVG orders are typically larger quantities, TP orders are profit-taking
                if 'long' in order_side or 'short' in order_side:
                    # Could be either - need position context to determine
                    result['fill_type'] = 'detected'  # Will be refined by caller
                
                break
    
    return result


def handle_order_fill(page: Page, fill_info: Dict, current_position: Dict) -> Dict[str, Any]:
    """Handle order fill by placing appropriate replacement orders.
    
    Args:
        page: Playwright page
        fill_info: Result from detect_order_fill
        current_position: Current position info
    
    Returns: Strategy execution result
    """
    result = {
        'success': False,
        'actions_taken': [],
        'errors': []
    }
    
    try:
        filled_order = fill_info.get('filled_order', {})
        order_side = filled_order.get('side', '').lower()
        order_price = filled_order.get('price', '')
        order_qty = filled_order.get('qty', '')
        
        position_direction = _infer_position_direction_from_position(current_position)
        
        # Determine if this was AVG or TP fill
        is_avg_fill = position_direction in order_side
        is_tp_fill = not is_avg_fill
        
        if RPA_DIAG:
            fill_type = 'AVG' if is_avg_fill else 'TP'
            log(f"🔄 Handling {fill_type} fill: {order_side} {order_qty} @ {order_price}")
        
        if is_avg_fill:
            # AVG order filled - position grew
            result['actions_taken'].append(f"AVG fill detected: {order_side} {order_qty} @ {order_price}")
            
            # Cancel remaining TP order (price no longer valid)
            remaining_orders = fill_info.get('remaining_orders', [])
            if remaining_orders:
                cancel_result = cancel_open_orders(page, max_to_cancel=len(remaining_orders))
                result['actions_taken'].append(f"Cancelled {cancel_result.get('cancelled', 0)} remaining orders")
            
            # Calculate new position size (estimate)
            # This is simplified - in practice you'd track this more precisely
            estimated_new_lots = 3  # 1 original + 2 from AVG
            
            # Place new TP and AVG orders for grown position
            strategy_prices = calculate_strategy_prices(current_position, estimated_new_lots)
            
            # Place new TP order
            tp_result = place_limit_order(page, 
                                        'short' if position_direction == 'long' else 'long',
                                        strategy_prices['tp_price'], 
                                        strategy_prices['tp_lots'])
            if tp_result.get('ok'):
                result['actions_taken'].append(f"Placed new TP: {strategy_prices['tp_lots']} @ {strategy_prices['tp_price']}")
            
            # Place new AVG order
            avg_result = place_limit_order(page, 
                                         position_direction,
                                         strategy_prices['avg_price'], 
                                         strategy_prices['avg_lots'])
            if avg_result.get('ok'):
                result['actions_taken'].append(f"Placed new AVG: {strategy_prices['avg_lots']} @ {strategy_prices['avg_price']}")
        
        elif is_tp_fill:
            # TP order filled - position flipped
            result['actions_taken'].append(f"TP fill detected: {order_side} {order_qty} @ {order_price}")
            
            # Cancel remaining AVG order (no longer relevant)
            remaining_orders = fill_info.get('remaining_orders', [])
            if remaining_orders:
                cancel_result = cancel_open_orders(page, max_to_cancel=len(remaining_orders))
                result['actions_taken'].append(f"Cancelled {cancel_result.get('cancelled', 0)} remaining orders")
            
            # Position should now be flipped - implement new seed strategy
            # Wait a moment for position to update, then re-implement strategy
            time.sleep(1.0)
            
            strategy_result = implement_haider_strategy(page)
            if strategy_result.get('success'):
                result['actions_taken'].append("Re-implemented strategy for flipped position")
            else:
                result['errors'].extend(strategy_result.get('errors', []))
        
        result['success'] = len(result['errors']) == 0
        return result
        
    except Exception as e:
        result['errors'].append(f"Fill handling error: {str(e)}")
        return result


def adaptive_strategy_engine(page: Page) -> Dict[str, Any]:
    """Main strategy engine that analyzes current state and takes appropriate action.
    
    This function can handle any starting state and continue the strategy appropriately.
    """
    result = {
        'success': False,
        'state_analysis': None,
        'actions_taken': [],
        'errors': []
    }
    
    try:
        if RPA_DIAG:
            log("🎯 Starting Adaptive Strategy Engine")
        
        # 1. Analyze current state
        state = analyze_strategy_state(page)
        result['state_analysis'] = state
        
        if state.get('errors'):
            result['errors'].extend(state['errors'])
            return result
        
        current_state = state.get('state')
        next_action = state.get('next_action')
        
        if RPA_DIAG:
            log(f"🎯 Current state: {current_state}, Next action: {next_action}")
        
        # 2. Take appropriate action based on state
        if next_action == 'create_initial_position':
            result['errors'].append("No position found. Please create an initial 1-lot position manually.")
            return result
        
        elif next_action == 'place_initial':
            # Implement full strategy (AVG + TP orders)
            strategy_result = implement_haider_strategy(page)
            result['actions_taken'].append("Implemented initial Haider Strategy")
            if not strategy_result.get('success'):
                result['errors'].extend(strategy_result.get('errors', []))
        
        elif next_action == 'place_missing':
            # Place missing order(s)
            missing_orders = state.get('missing_orders', [])
            position = state.get('position')
            
            strategy_prices = calculate_strategy_prices(position)
            position_direction = strategy_prices['position_direction']
            
            for missing_type in missing_orders:
                if missing_type == 'avg':
                    # Place missing AVG order
                    avg_result = place_limit_order(page, position_direction, 
                                                 strategy_prices['avg_price'], 
                                                 strategy_prices['avg_lots'])
                    if avg_result.get('ok'):
                        result['actions_taken'].append(f"Placed missing AVG: {strategy_prices['avg_lots']} @ {strategy_prices['avg_price']}")
                
                elif missing_type == 'tp':
                    # Place missing TP order
                    tp_side = 'short' if position_direction == 'long' else 'long'
                    tp_result = place_limit_order(page, tp_side, 
                                                strategy_prices['tp_price'], 
                                                strategy_prices['tp_lots'])
                    if tp_result.get('ok'):
                        result['actions_taken'].append(f"Placed missing TP: {strategy_prices['tp_lots']} @ {strategy_prices['tp_price']}")
        
        elif next_action == 'monitor':
            # Strategy is fully set up - ready for monitoring
            result['actions_taken'].append("Strategy fully set up - ready for monitoring")
        
        else:
            result['errors'].append(f"Unknown next action: {next_action}")
            return result
        
        result['success'] = len(result['errors']) == 0
        return result
        
    except Exception as e:
        result['errors'].append(f"Strategy engine error: {str(e)}")
        return result


def strategy_monitor_loop(page: Page, reattach_cb: Optional[Callable] = None) -> None:
    """Continuous monitoring loop for strategy execution.
    
    Monitors for order fills and responds according to Haider Strategy rules.
    """
    if RPA_DIAG:
        log("🔄 Starting Strategy Monitor Loop")
    
    # Initial setup
    setup_result = adaptive_strategy_engine(page)
    if not setup_result.get('success'):
        log(f"❌ Strategy setup failed: {setup_result.get('errors')}")
        return
    
    log(f"✅ Strategy initialized: {setup_result.get('actions_taken')}")
    
    # Monitoring loop
    last_orders = []
    iteration = 0
    
    while True:
        try:
            iteration += 1
            if RPA_DIAG and iteration % 10 == 0:
                log(f"🔄 Monitor iteration {iteration}")
            
            # Get current orders
            current_orders_info = extract_open_orders(page)
            current_orders = current_orders_info.get('orders', [])
            
            # Check for order fills
            if last_orders and len(current_orders) < len(last_orders):
                fill_info = detect_order_fill(last_orders, current_orders)
                
                if fill_info.get('filled'):
                    log(f"🔄 Order fill detected!")
                    
                    # Get updated position
                    current_position = extract_position_data(page)
                    
                    # Handle the fill
                    handle_result = handle_order_fill(page, fill_info, current_position)
                    
                    if handle_result.get('success'):
                        log(f"✅ Fill handled: {handle_result.get('actions_taken')}")
                    else:
                        log(f"❌ Fill handling failed: {handle_result.get('errors')}")
            
            # Store current orders for next iteration
            last_orders = current_orders
            
            # Sleep before next check
            time.sleep(10)  # Check every 10 seconds
            
        except KeyboardInterrupt:
            log("🛑 Strategy monitoring stopped by user")
            break
        except Exception as e:
            log(f"❌ Monitor error: {e}")
            if reattach_cb:
                try:
                    page = reattach_cb()
                    if RPA_DIAG:
                        log("🔄 Reattached to page")
                except Exception:
                    log("❌ Reattach failed")
                    break
            time.sleep(5)  # Wait before retry
    """Implement the Haider Strategy based on current position state.
    
    Expected starting state: 1 open position of 1 lot
    Action: Place 2 orders:
    1. AVG order: 2 lots in SAME direction, 750 USD away
    2. TP order: 2 lots in OPPOSITE direction, 300 USD away
    
    Returns: Dict with strategy execution results
    """
    strategy_result = {
        "success": False,
        "position": None,
        "orders_placed": [],
        "errors": []
    }
    
    try:
        if RPA_DIAG:
            log("🎯 Starting Haider Strategy Implementation")
        
        # 1. Get current position
        pos_info = extract_position_data(page)
        if not pos_info:
            strategy_result["errors"].append("No position found")
            return strategy_result
        
        strategy_result["position"] = pos_info
        
        # Extract position details
        position_side = pos_info.get("side", "NONE")
        position_size = pos_info.get("size", "")
        avg_price_str = pos_info.get("avg_price", "")
        
        if RPA_DIAG:
            log(f"🎯 Current Position: side={position_side}, size={position_size}, avg_price={avg_price_str}")
        
        # Parse average price
        try:
            avg_price = float(re.sub(r"[^0-9\.-]", "", avg_price_str or ""))
        except ValueError:
            # If avg_price is N/A, try to infer from position data or use current market price
            if RPA_DIAG:
                log(f"🎯 avg_price not available ({avg_price_str}), will use position data to estimate")
            
            # Look for a numeric value in the position data that could be the avg price
            # From the raw position data, try to find a price-like number
            all_pos_text = str(pos_info)
            price_matches = re.findall(r"\b(\d+\.?\d*)\b", all_pos_text)
            potential_prices = []
            for match in price_matches:
                try:
                    val = float(match)
                    # BTC prices are typically between 10,000 and 200,000 USD
                    if 10000 <= val <= 200000:
                        potential_prices.append(val)
                except ValueError:
                    continue
            
            if potential_prices:
                avg_price = potential_prices[0]  # Use the first reasonable price found
                if RPA_DIAG:
                    log(f"🎯 Using estimated avg_price: {avg_price}")
            else:
                # Fallback: use a reasonable current price estimate (around 117k based on snapshot)
                avg_price = 117000
                if RPA_DIAG:
                    log(f"🎯 Using fallback avg_price: {avg_price}")
        
        if avg_price <= 0:
            strategy_result["errors"].append(f"Invalid avg_price: {avg_price}")
            return strategy_result
        
        # Determine position direction
        if position_side == "NONE" and not position_size:
            strategy_result["errors"].append("No open position found")
            return strategy_result
        
        # Infer direction from size (handle both "+0.001 BTC" and text-based sides)
        is_long_position = False
        if "+" in position_size or position_side.lower() in ("long", "buy"):
            is_long_position = True
        elif "-" in position_size or position_side.lower() in ("short", "sell"):
            is_long_position = False
        else:
            # If we have a position size but unclear direction, try to infer from other data
            if position_size and "0.001" in position_size:  # We have a position
                # Default to long if we can't determine (most positions are long in demo)
                is_long_position = True
                if RPA_DIAG:
                    log(f"🎯 Could not determine direction clearly, defaulting to LONG based on size: {position_size}")
            else:
                strategy_result["errors"].append(f"Could not determine position direction from side={position_side}, size={position_size}")
                return strategy_result
        
        direction = "LONG" if is_long_position else "SHORT"
        if RPA_DIAG:
            log(f"🎯 Position Direction: {direction} at avg_price={avg_price}")
        
        # 2. Calculate order prices
        # AVG order: same direction, 750 USD away
        if is_long_position:
            avg_price_target = avg_price - 750  # Long position: average down
            tp_price_target = avg_price + 300   # Long position: take profit up
            avg_side = "long"
            tp_side = "short"
        else:
            avg_price_target = avg_price + 750  # Short position: average up  
            tp_price_target = avg_price - 300   # Short position: take profit down
            avg_side = "short"
            tp_side = "long"
        
        if RPA_DIAG:
            log(f"🎯 AVG Order: {avg_side} 2 lots @ {avg_price_target}")
            log(f"🎯 TP Order: {tp_side} 2 lots @ {tp_price_target}")
        
        # 3. Place AVG order (same direction, 2 lots)
        if RPA_DIAG:
            log(f"🎯 Placing AVG order: {avg_side} 2 lots @ {avg_price_target}")
        
        avg_result = place_limit_order(page, avg_side, avg_price_target, 2, maker_only=True)
        if avg_result.get("ok"):
            strategy_result["orders_placed"].append({
                "type": "AVG",
                "side": avg_side,
                "price": avg_price_target,
                "lots": 2,
                "result": "success"
            })
            if RPA_DIAG:
                log(f"✅ AVG order placed successfully")
        else:
            error_msg = f"AVG order failed: {avg_result.get('error', 'unknown')}"
            strategy_result["errors"].append(error_msg)
            if RPA_DIAG:
                log(f"❌ {error_msg}")
        
        # 4. Place TP order (opposite direction, 2 lots)
        if RPA_DIAG:
            log(f"🎯 Placing TP order: {tp_side} 2 lots @ {tp_price_target}")
        
        tp_result = place_limit_order(page, tp_side, tp_price_target, 2, maker_only=True)
        if tp_result.get("ok"):
            strategy_result["orders_placed"].append({
                "type": "TP",
                "side": tp_side,
                "price": tp_price_target,
                "lots": 2,
                "result": "success"
            })
            if RPA_DIAG:
                log(f"✅ TP order placed successfully")
        else:
            error_msg = f"TP order failed: {tp_result.get('error', 'unknown')}"
            strategy_result["errors"].append(error_msg)
            if RPA_DIAG:
                log(f"❌ {error_msg}")
        
        # 5. Check overall success
        if len(strategy_result["orders_placed"]) == 2:
            strategy_result["success"] = True
            if RPA_DIAG:
                log(f"🎯 ✅ Haider Strategy implemented successfully! Placed {len(strategy_result['orders_placed'])} orders")
        else:
            if RPA_DIAG:
                log(f"🎯 ❌ Haider Strategy partially failed. Placed {len(strategy_result['orders_placed'])}/2 orders")
        
        return strategy_result
        
    except Exception as e:
        strategy_result["errors"].append(f"Strategy implementation error: {str(e)}")
        if RPA_DIAG:
            log(f"🎯 ❌ Strategy error: {e}")
        return strategy_result


def analyze_current_state(page: Page) -> Dict[str, Any]:
    """Analyze current position and orders to determine strategy state and next action."""
    position_data = extract_position_data(page)
    orders_data = extract_open_orders(page)
    
    # Parse position 
    position_lots = 0
    position_side = "none"
    position_avg_price = 0.0
    
    if position_data.get("success") and position_data.get("position"):
        pos = position_data["position"]
        position_lots = abs(float(pos.get("size", "0")))
        position_side = "long" if float(pos.get("size", "0")) > 0 else "short"
        position_avg_price = float(pos.get("avg_price", "0"))
    
    # Parse orders
    open_orders = orders_data.get("orders", [])
    
    # Determine state
    if position_lots == 0:
        state = "no_position"
        next_action = "seed_placement"
    elif len(open_orders) == 0:
        state = "position_only"
        next_action = "initialize_strategy"
    elif len(open_orders) == 1:
        state = "single_order"
        next_action = "complete_order_pair"
    elif len(open_orders) == 2:
        state = "monitoring"
        next_action = "monitor_fills"
    else:
        state = "excess_orders"
        next_action = "cleanup_orders"
    
    return {
        "position_lots": position_lots,
        "position_side": position_side,
        "position_avg_price": position_avg_price,
        "open_orders": open_orders,
        "state": state,
        "next_action": next_action,
        "mark_price": position_data.get("mark_price", 0.0)
    }


def calculate_strategy_prices(position_info: Dict[str, Any]) -> Dict[str, float]:
    """Calculate TP and AVG prices based on current position."""
    lots = position_info.get("position_lots", 0)
    side = position_info.get("position_side", "")
    avg_price = position_info.get("position_avg_price", 0.0)
    
    if lots == 1:
        # Seed stage: TP at ±300, AVG at ±750
        tp_offset = 300
        avg_offset = 750
        tp_lots = 2.0
        avg_lots = 2.0
    elif lots == 3:
        # First averaging stage: TP at ±200, AVG at ±500
        tp_offset = 200
        avg_offset = 500  
        tp_lots = 4.0
        avg_lots = 6.0
    elif lots == 9:
        # Second averaging stage: TP only at ±100
        tp_offset = 100
        avg_offset = 0  # No more averaging
        tp_lots = 10.0
        avg_lots = 0.0
    else:
        # Default fallback
        tp_offset = 300
        avg_offset = 750
        tp_lots = 2.0
        avg_lots = 2.0
    
    if side == "long":
        tp_price = avg_price + tp_offset  # Short TP above
        avg_price_target = avg_price - avg_offset  # Long AVG below
        tp_side = "short"
        avg_side = "long"
    else:  # short
        tp_price = avg_price - tp_offset  # Long TP below
        avg_price_target = avg_price + avg_offset  # Short AVG above
        tp_side = "long" 
        avg_side = "short"
    
    return {
        "tp_price": tp_price,
        "tp_side": tp_side,
        "tp_lots": tp_lots,
        "avg_price": avg_price_target,
        "avg_side": avg_side,
        "avg_lots": avg_lots,
        "position_direction": side
    }


def implement_haider_strategy(page: Page) -> Dict[str, Any]:
    """Implement the Haider Strategy by placing AVG and TP orders based on current position."""
    log("📈 Implementing Haider Strategy...")
    
    # Get current position data
    position_data = extract_position_data(page)
    if not position_data.get("success") or not position_data.get("position"):
        return {"success": False, "error": "No position found"}
    
    position = position_data["position"]
    size = float(position.get("size", "0"))
    avg_price = float(position.get("avg_price", "0"))
    
    if size == 0:
        return {"success": False, "error": "No open position"}
    
    # Determine position direction and lots
    is_long = size > 0
    lots = abs(size)
    direction = "long" if is_long else "short"
    
    log(f"📊 Current position: {direction.upper()} {lots} lots @ ${avg_price:,.2f}")
    
    # Calculate strategy prices based on position size
    position_info = {
        "position_lots": lots,
        "position_side": direction,
        "position_avg_price": avg_price
    }
    
    strategy_prices = calculate_strategy_prices(position_info)
    
    # Place orders
    orders_placed = []
    
    try:
        # Place TP order (opposite direction)
        if strategy_prices['tp_lots'] > 0:
            log(f"🎯 Placing TP order: {strategy_prices['tp_side']} {int(strategy_prices['tp_lots'])} lots @ ${strategy_prices['tp_price']:,.2f}")
            tp_result = place_limit_order(page, strategy_prices['tp_side'], 
                                        strategy_prices['tp_price'], 
                                        int(strategy_prices['tp_lots']))
            if tp_result.get("success"):
                orders_placed.append(f"TP: {strategy_prices['tp_side']} {int(strategy_prices['tp_lots'])} lots @ {strategy_prices['tp_price']:,.2f}")
        
        # Place AVG order (same direction) - only if lots < 9
        if strategy_prices['avg_lots'] > 0:
            log(f"📈 Placing AVG order: {strategy_prices['avg_side']} {int(strategy_prices['avg_lots'])} lots @ ${strategy_prices['avg_price']:,.2f}")
            avg_result = place_limit_order(page, strategy_prices['avg_side'],
                                         strategy_prices['avg_price'],
                                         int(strategy_prices['avg_lots']))
            if avg_result.get("success"):
                orders_placed.append(f"AVG: {strategy_prices['avg_side']} {int(strategy_prices['avg_lots'])} lots @ {strategy_prices['avg_price']:,.2f}")
        
        if orders_placed:
            log(f"✅ Strategy implemented successfully! Placed {len(orders_placed)} orders")
            for order in orders_placed:
                log(f"  - {order}")
            return {
                "success": True, 
                "orders_placed": len(orders_placed),
                "details": orders_placed
            }
        else:
            return {"success": False, "error": "No orders were placed"}
            
    except Exception as e:
        log(f"❌ Error implementing strategy: {e}")
        return {"success": False, "error": str(e)}


def run_strategy_tests(page: Page) -> None:
    """Run comprehensive strategy tests by systematically testing different scenarios."""
    log("🧪 Starting Comprehensive Strategy Testing")
    log("=" * 60)
    
    # Test 1: Current State Analysis
    log("🔍 TEST 1: Current State Analysis")
    current_state = analyze_current_state(page)
    log(f"✅ Position: {current_state['position_lots']} lots, Orders: {len(current_state['open_orders'])}, State: {current_state['state']}")
    
    # Test 2: Clear orders to test clean scenarios
    log("\n🧹 TEST 2: Testing Clean State Scenarios")
    original_orders = current_state['open_orders'].copy()
    
    if len(original_orders) > 0:
        log("Clearing existing orders for clean testing...")
        clear_result = cancel_open_orders(page, wait_s=2.0)
        log(f"✅ Cleared {clear_result.get('cancelled', 0)} orders")
        time.sleep(1)
    
    # Test 3: Adaptive Strategy from Clean State
    log("\n🎯 TEST 3: Adaptive Strategy from Clean State")
    clean_result = adaptive_strategy_engine(page)
    log(f"✅ Clean state result: {clean_result.get('action', 'none')} - Success: {clean_result.get('success', False)}")
    time.sleep(2)
    
    # Test 4: Verify Strategy Implementation
    log("\n📊 TEST 4: Strategy Implementation Verification")
    post_clean_state = analyze_current_state(page)
    log(f"✅ After clean strategy - Orders: {len(post_clean_state['open_orders'])}, State: {post_clean_state['state']}")
    
    # Test 5: Simulate Fill by Cancelling One Order
    if len(post_clean_state['open_orders']) >= 2:
        log("\n🎲 TEST 5: Simulating Order Fill")
        test_order = post_clean_state['open_orders'][0]
        test_price = test_order.get('price', '')[:5]
        log(f"Cancelling order at {test_price} to simulate fill...")
        
        cancel_result = cancel_open_orders(page, price_substr=test_price, wait_s=2.0)
        log(f"✅ Cancelled {cancel_result.get('cancelled', 0)} orders")
        time.sleep(1)
        
        # Test response to simulated fill
        fill_response = adaptive_strategy_engine(page)
        log(f"✅ Fill response: {fill_response.get('action', 'none')} - Success: {fill_response.get('success', False)}")
    
    # Test 6: Final State
    log("\n🏁 TEST 6: Final State Verification")
    final_state = analyze_current_state(page)
    log(f"✅ Final state - Position: {final_state['position_lots']} lots, Orders: {len(final_state['open_orders'])}, State: {final_state['state']}")
    
    log("\n" + "=" * 60)
    log("🧪 Testing Complete!")


def close_all_positions(page: Page, wait_s: float = 3.0) -> Dict[str, Any]:
    """Close all open positions using the 'Close All Positions' button.
    
    WARNING: This will close ALL positions immediately at market price.
    Only use in demo/testing scenarios!
    """
    try:
        log("🚨 CLOSING ALL POSITIONS - This is dangerous in live trading!")
        
        # Look for the Close All Positions button
        close_all_selector = 'button[data-testid="close-all-positions"]'
        
        if page.locator(close_all_selector).count() == 0:
            log("❌ Close All Positions button not found")
            return {"success": False, "error": "Button not found"}
        
        # Click the button
        page.locator(close_all_selector).click()
        log("🔧 Clicked 'Close All Positions' button")
        
        # Wait for any confirmation dialog and handle it
        page.wait_for_timeout(1000)  # Wait for potential dialog
        
        # Look for confirmation dialog and confirm if needed
        confirm_selectors = [
            'button:has-text("Confirm")',
            'button:has-text("Yes")', 
            'button:has-text("Close")',
            'button[data-testid="confirm-button"]'
        ]
        
        for selector in confirm_selectors:
            if page.locator(selector).count() > 0:
                page.locator(selector).click()
                log(f"🔧 Confirmed action with {selector}")
                break
        
        # Wait for the action to complete
        time.sleep(wait_s)
        
        log("✅ Close All Positions action completed")
        return {"success": True, "action": "close_all_positions"}
        
    except Exception as e:
        log(f"❌ Error closing all positions: {e}")
        return {"success": False, "error": str(e)}


def close_position_by_symbol(page: Page, symbol: str = "BTCUSD", wait_s: float = 2.0) -> Dict[str, Any]:
    """Close a specific position by clicking the X button in the position row.
    
    WARNING: This closes the position at market price!
    """
    try:
        log(f"🚨 CLOSING POSITION for {symbol}")
        # Ensure Positions tab is active
        try:
            _activate_tab(page, r"^Positions$")
            page.wait_for_timeout(300)
        except Exception:
            pass

        # Find the BTCUSD row first
        row = page.locator("tr:has(td:has-text('BTCUSD'))").first
        if row.count() == 0:
            row = page.locator("tr:has(:text('BTCUSD'))").first
        if row.count() == 0:
            # Fallback to whole-page search if row not found
            row = None

        # Define close button selectors
        close_selectors = [
            "button:has-text('Close')",
            "button:has(svg)",
            "[data-testid*='close']",
            "svg[data-palette='CrossIcon']",
            "xpath=.//button[contains(@aria-label,'Close') or contains(@title,'Close')]",
        ]

        def try_click_close(scope) -> bool:
            for selector in close_selectors:
                try:
                    cand = scope.locator(selector).first if scope else page.locator(selector).first
                    if cand and cand.count() > 0 and cand.is_visible():
                        try:
                            cand.scroll_into_view_if_needed(timeout=800)
                        except Exception:
                            pass
                        cand.click(timeout=1500)
                        log(f"🔧 Clicked close button: {selector}")
                        return True
                except Exception:
                    continue
            return False

        clicked = False
        if row and row.count() > 0:
            clicked = try_click_close(row)
        if not clicked:
            clicked = try_click_close(page)
        if not clicked:
            log("❌ No close button found in position row/page")
            return {"success": False, "error": "Close button not found"}

        # Wait for confirmation dialog and confirm
        page.wait_for_timeout(1000)
        confirm_selectors = [
            "button:has-text('Confirm')",
            "button:has-text('Yes')",
            "button:has-text('Close Position')",
        ]
        for confirm_sel in confirm_selectors:
            try:
                btn = page.locator(confirm_sel).first
                if btn and btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=1200)
                    log("🔧 Confirmed position close")
                    break
            except Exception:
                continue

        time.sleep(wait_s)
        log(f"✅ Position close action completed for {symbol}")
        return {"success": True, "action": "close_position", "symbol": symbol}

    except Exception as e:
        log(f"❌ Error closing position: {e}")
        return {"success": False, "error": str(e)}


def cancel_all_orders_button(page: Page, wait_s: float = 2.0) -> Dict[str, Any]:
    """Cancel all orders using the 'Cancel All Orders' button."""
    try:
        log("🧹 Cancelling all orders using Cancel All Orders button")
        
        # Look for Cancel All Orders button
        cancel_all_selectors = [
            'button:has-text("Cancel All Orders")',
            'span:has-text("Cancel All Orders")',
            '[data-testid*="cancel-all"]'
        ]
        
        for selector in cancel_all_selectors:
            if page.locator(selector).count() > 0:
                page.locator(selector).click()
                log(f"🔧 Clicked Cancel All Orders button: {selector}")
                
                # Wait for confirmation
                page.wait_for_timeout(1000)
                
                # Handle confirmation dialog
                confirm_selectors = [
                    'button:has-text("Confirm")',
                    'button:has-text("Yes")',
                    'button:has-text("Cancel Orders")'
                ]
                
                for confirm_sel in confirm_selectors:
                    if page.locator(confirm_sel).count() > 0:
                        page.locator(confirm_sel).click()
                        log("🔧 Confirmed order cancellation")
                        break
                
                time.sleep(wait_s)
                log("✅ Cancel All Orders action completed")
                return {"success": True, "action": "cancel_all_orders"}
        
        log("❌ Cancel All Orders button not found")
        return {"success": False, "error": "Button not found"}
        
    except Exception as e:
        log(f"❌ Error cancelling all orders: {e}")
        return {"success": False, "error": str(e)}


def cancel_orders(page: Page, side: Optional[str] = None, price_substr: Optional[str] = None, max_to_cancel: Optional[int] = None) -> Dict[str, Any]:
    """Cancel open orders optionally filtered by side and/or price substring."""
    return cancel_open_orders(page, side=side, price_substr=price_substr, max_to_cancel=max_to_cancel)


def _infer_position_side(size_text: Optional[str]) -> Optional[str]:
    """Infer 'long' or 'short' from a position size string (e.g., '+0.001 BTC', '-0.001 BTC')."""
    if not size_text:
        return None
    s = size_text.strip().lower()
    if s.startswith("+"):
        return "long"
    if s.startswith("-"):
        return "short"
    if "long" in s:
        return "long"
    if "short" in s:
        return "short"
    return None


def watch_seed_phase(page: Page, timeout_s: float = 300.0) -> Dict[str, Any]:
    """Monitor open orders during seed phase and detect which fills first.

    Assumptions at start: 1 open position of 1 lot and exactly 2 open orders.
    Returns: { 'result': 'tp'|'avg'|'timeout'|'error', 'position_side': str, 'initial': [...], 'final': [...] }
    """
    try:
        # Capture initial state
        pos = extract_position_data(page)
        pos_side = _infer_position_side(pos.get("size")) or "unknown"
        oinfo = extract_open_orders(page)
        initial = oinfo.get("orders", [])
        if len(initial) < 1:
            return {"result": "error", "error": "no_open_orders", "position_side": pos_side, "initial": initial, "final": initial}
        base_sig = _orders_signature(initial)
        # Side mapping for initial orders
        def norm_side(x: Optional[str]) -> Optional[str]:
            if not x:
                return None
            x = x.strip().lower()
            if x in ("buy", "long"):
                return "long"
            if x in ("sell", "short"):
                return "short"
            return None

        # Loop until one order disappears or timeout
        deadline = time.time() + max(5.0, timeout_s)
        final = initial
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                now_orders = extract_open_orders(page).get("orders", [])
            except Exception:
                continue
            sig = _orders_signature(now_orders)
            if sig != base_sig:
                final = now_orders
                # Identify which side disappeared
                init_sides = sorted([norm_side(o.get("side")) or "?" for o in initial])
                now_sides = sorted([norm_side(o.get("side")) or "?" for o in now_orders])
                # Find the missing side label
                missing: Optional[str] = None
                tmp = now_sides.copy()
                for s in init_sides:
                   if s in tmp:
                       tmp.remove(s)
                   else:
                       missing = s
                       break
                if missing is None:
                    # fallback: use length change
                    missing = "long" if init_sides.count("long") > now_sides.count("long") else "short"
                # If the missing side equals position side → AVG filled; else TP filled
                if pos_side != "unknown" and missing in ("long", "short"):
                    result = "avg" if missing == pos_side else "tp"
                else:
                    result = "avg" if missing == "long" else "tp"  # best-effort
                return {"result": result, "position_side": pos_side, "initial": initial, "final": final}
        # Timeout
        return {"result": "timeout", "position_side": pos_side, "initial": initial, "final": final}
    except Exception as e:
        return {"result": "error", "error": str(e), "position_side": None, "initial": [], "final": []}


def monitor_positions(page: Page, reattach_cb=None) -> None:
    """Continuously monitor position data"""
    log("🔍 Starting position monitoring...")
    log("📊 Monitoring: Size, Entry Price, Mark Price, UPNL")
    log(f"⏱️ Position interval: {POSITIONS_INTERVAL}s, Orders interval: {ORDERS_INTERVAL}s")
    log("🛑 Press Ctrl+C to stop monitoring")
    
    # Initial wait to allow the page to finish rendering
    try:
        log("⏳ Waiting 10 seconds for page to fully render…")
        time.sleep(10)
    except Exception:
        pass

    last_display = ""
    last_position_size = None
    cached_open_orders: Optional[Dict[str, Any]] = None
    last_pos_ts = 0.0
    last_orders_ts = 0.0
    
    try:
        while True:
            try:
                now = time.time()

                # Positions task
                if now - last_pos_ts >= POSITIONS_INTERVAL:
                    try:
                        data = extract_position_data(page)
                        # Format for display
                        display = format_position_display(data)
                        if display != last_display:
                            print(display)
                            last_display = display
                        # Track size for optional immediate orders refresh
                        current_size = data.get("size")
                        if current_size != last_position_size:
                            last_position_size = current_size
                            # Also trigger orders refresh on size change
                            last_orders_ts = 0.0
                    except Exception as pos_err:
                        log(f"❌ Positions fetch error: {pos_err}")
                    finally:
                        last_pos_ts = now

                # Open Orders task
                if now - last_orders_ts >= ORDERS_INTERVAL:
                    try:
                        orders_info = extract_open_orders(page)
                        cached_open_orders = orders_info
                        orders = orders_info.get("orders", []) if orders_info else []
                        if orders:
                            log("📬 Open Orders updated:")
                            for i, o in enumerate(orders, 1):
                                log(f"  {i}) {o.get('side','?')} Size={o.get('size','?')} Limit Price={o.get('price','?')}")
                        else:
                            log("📭 No open orders detected for BTCUSD")
                        # Return to Positions after reading orders
                        try:
                            _activate_tab(page, r"^Positions$")
                            page.wait_for_timeout(150)
                        except Exception:
                            pass
                    except Exception as oo_err:
                        log(f"❌ Failed to refresh open orders: {oo_err}")
                    finally:
                        last_orders_ts = now

                # Base loop sleep
                time.sleep(BASE_LOOP_SLEEP)
                
            except KeyboardInterrupt:
                log("🛑 Position monitoring stopped by user")
                break
            except Exception as e:
                msg = str(e)
                log(f"❌ Error during monitoring: {msg}")
                # Try to reattach if the page/context/browser closed
                if reattach_cb and any(s in msg.lower() for s in ["has been closed", "target closed", "websocket closed"]):
                    try:
                        log("♻️ Attempting to reattach to the Edge trading tab…")
                        new_page = reattach_cb()
                        if new_page:
                            page = new_page
                            log("✅ Reattached successfully.")
                    except Exception as re_err:
                        log(f"❌ Reattach failed: {re_err}")
                time.sleep(BASE_LOOP_SLEEP)  # Continue monitoring despite errors
                
    except Exception as e:
        log(f"❌ Fatal monitoring error: {e}")


def connect_to_edge_existing_tab(target_url: str, timeout_s: int = 20, reuse_playwright=None):
    """Attach to existing Edge via CDP and return page for the matching tab.

    IMPORTANT: Edge must be running with --remote-debugging-port=9222.
    This function will NOT launch a new Edge window.
    """
    log(f"🌐 Attaching to existing Edge (CDP) at http://127.0.0.1:{CDP_PORT} …")
    playwright = reuse_playwright or sync_playwright().start()
    try:
        browser: Browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        log("✅ Connected to Edge via CDP")

        def find_page() -> Optional[Page]:
            candidates: List[Page] = []
            for ctx in browser.contexts:
                for p in ctx.pages:
                    url = (p.url or "").lower()
                    if ("demo.delta.exchange" in url) and ("/app/futures/trade/" in url):
                        candidates.append(p)
            if not candidates:
                # fallback: any trade page
                for ctx in browser.contexts:
                    for p in ctx.pages:
                        url = (p.url or "").lower()
                        if target_url.lower() in url or "/app/futures/trade/" in url:
                            candidates.append(p)
            if not candidates:
                return None
            # score candidates: prefer non-login URLs and contexts with delta cookies
            def score(p: Page) -> int:
                s = 0
                url = (p.url or "").lower()
                if "demo.delta.exchange" in url:
                    s += 2
                if "/app/futures/trade/" in url:
                    s += 3
                if "login" in url:
                    s -= 5
                try:
                    cookies = p.context.cookies()
                    delta_cookies = [c for c in cookies if "delta.exchange" in (c.get("domain") or "")]
                    s += min(len(delta_cookies), 5)
                except Exception:
                    pass
                return s
            candidates.sort(key=score, reverse=True)
            return candidates[0]

        # Wait briefly for the user-opened tab to appear
        deadline = time.time() + timeout_s
        page = find_page()
        while page is None and time.time() < deadline:
            time.sleep(0.5)
            page = find_page()

        if page is None:
            raise RuntimeError(
                "Trading tab not found in existing Edge session. Make sure the URL is open in the logged-in Edge window."
            )

        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        return page
    except Exception as e:
        log(f"❌ Could not attach to existing Edge. Ensure Edge is started with --remote-debugging-port={CDP_PORT}.")
        log(f"Tip: Close Edge completely, then run: msedge --remote-debugging-port={CDP_PORT}")
        raise


def main() -> int:
    debug_dir = ensure_debug_dir()
    set_log_file(debug_dir / "run.log")

    log("🚀 Starting Delta Exchange Position Monitor")
    log("🎯 Target: BTC/BTCUSD Futures Trading Page")
    log("🌐 Browser: Microsoft Edge")
    
    playwright = None
    try:
        # 0) Ensure Edge CDP is available; if not, optionally restart Edge with CDP
        if not is_cdp_available(CDP_PORT):
            allow_kill = (os.getenv("EDGE_ALLOW_KILL", "0").strip().lower() in ("1", "true", "yes"))
            if edge_running() and allow_kill:
                log(f"🧪 No CDP on 127.0.0.1:{CDP_PORT}. Closing Edge to relaunch with CDP…")
                kill_edge_processes()
                time.sleep(1)
                start_edge_with_cdp(DELTA_TRADE_URL, CDP_PORT)
                if not wait_for_cdp(CDP_PORT, 12):
                    log(f"❌ CDP still not available on 127.0.0.1:{CDP_PORT} after relaunch.")
                    log(f"Try manually: msedge --remote-debugging-port={CDP_PORT}")
                    return 1
            else:
                log(f"🧪 No CDP detected on 127.0.0.1:{CDP_PORT}. Attempting to start Edge with CDP…")
                start_edge_with_cdp(DELTA_TRADE_URL, CDP_PORT)
                if not wait_for_cdp(CDP_PORT, 8):
                    log(f"❌ CDP still not available on 127.0.0.1:{CDP_PORT}.")
                    if not allow_kill and edge_running():
                        log("Edge appears to be running without CDP. Set EDGE_ALLOW_KILL=1 in .env to let the bot close Edge and relaunch automatically, or run:")
                        log(f"  msedge --remote-debugging-port={CDP_PORT}")
                    else:
                        log(f"Try manually: msedge --remote-debugging-port={CDP_PORT}")
                    return 1

        # 1) Open the target URL in Edge (if not already open) using default browser as a hint
        log(f"🔗 Opening URL in default browser (Edge): {DELTA_TRADE_URL}")
        try:
            webbrowser.open(DELTA_TRADE_URL)
        except Exception:
            pass
        log("⏳ Waiting a moment for the tab to appear…")
        time.sleep(3)

        # 2) Attach ONLY to existing Edge (no new window)
        try:
            # Start Playwright once and reuse between reattachments
            playwright = sync_playwright().start()
            page = connect_to_edge_existing_tab(DELTA_TRADE_URL, reuse_playwright=playwright)
            log("✅ Attached to the existing Edge trading tab")

            # Optional one-shot actions via CLI
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--action", choices=["monitor", "place", "cancel", "seedwatch", "long", "short", "snapshot", "strategy", "adaptive", "strategymonitor", "test", "closeall", "closepos", "cancelall"], default="monitor")
            parser.add_argument("--side", choices=["buy", "sell", "long", "short"], required=False)
            parser.add_argument("--price", type=float, required=False)
            parser.add_argument("--lots", type=int, required=False)
            parser.add_argument("--priceSubstr", type=str, required=False)
            try:
                args, _ = parser.parse_known_args(sys.argv[1:])
            except SystemExit:
                args = argparse.Namespace(action="monitor", side=None, price=None, lots=None, priceSubstr=None)

            if args.action == "place":
                if args.side and args.price is not None and args.lots is not None:
                    log(f"🧪 Placing {args.side} limit order: price={args.price}, lots={args.lots}, maker-only=True")
                    res = place_limit_order(page, args.side, args.price, args.lots, maker_only=True)
                    log(f"Result: ok={res.get('ok')} before={len(res.get('before', []))} after={len(res.get('after', []))}")
                else:
                    log("❌ Missing --side/--price/--lots for action=place")
            elif args.action == "long":
                if args.price is not None and args.lots is not None:
                    log(f"🧪 Placing LONG: price={args.price}, lots={args.lots}")
                    res = create_long_order(page, args.price, args.lots, maker_only=True)
                    log(f"Result: ok={res.get('ok')} after={len(res.get('after', []))}")
                else:
                    log("❌ Missing --price/--lots for action=long")
            elif args.action == "short":
                if args.price is not None and args.lots is not None:
                    log(f"🧪 Placing SHORT: price={args.price}, lots={args.lots}")
                    res = create_short_order(page, args.price, args.lots, maker_only=True)
                    log(f"Result: ok={res.get('ok')} after={len(res.get('after', []))}")
                else:
                    log("❌ Missing --price/--lots for action=short")
            elif args.action == "cancel":
                log(f"🧪 Cancelling open orders filter: side={args.side or '*'}, priceSubstr={args.priceSubstr or '*'}")
                res = cancel_open_orders(page, side=args.side, price_substr=args.priceSubstr)
                log(f"Result: ok={res.get('ok')} cancelled={res.get('cancelled', 0)} before={len(res.get('before', []))} after={len(res.get('after', []))}")
            elif args.action == "seedwatch":
                log("🔎 Watching seed phase to detect first fill (TP vs AVG)…")
                res = watch_seed_phase(page, timeout_s=600)
                log(f"Result: {res.get('result')} position_side={res.get('position_side')} initial={len(res.get('initial', []))} final={len(res.get('final', []))}")
            elif args.action == "snapshot":
                log("📸 Taking current state snapshot...")
                # Extract positions
                try:
                    pos_info = extract_position_data(page)
                    if pos_info:
                        log(f"🏦 Position: side={pos_info.get('side', 'NONE')} size={pos_info.get('size', '0')} avg_price={pos_info.get('avg_price', 'N/A')}")
                    else:
                        log("🏦 Position: No open position found")
                except Exception as e:
                    log(f"❌ Error extracting position: {e}")
                
                # Extract open orders
                try:
                    orders_info = extract_open_orders(page)
                    orders = orders_info.get("orders", [])
                    if orders:
                        log(f"📋 Open Orders ({len(orders)}):")
                        for i, order in enumerate(orders, 1):
                            side = order.get('side', 'unknown')
                            price = order.get('price', 'unknown')
                            qty = order.get('qty', 'unknown')
                            log(f"  [{i}] {side} {qty} @ {price}")
                    else:
                        log("📋 Open Orders: None")
                except Exception as e:
                    log(f"❌ Error extracting orders: {e}")
            elif args.action == "strategy":
                log("🎯 Implementing Haider Strategy...")
                result = implement_haider_strategy(page)
                if result["success"]:
                    log(f"✅ Strategy implemented successfully! Placed {len(result['orders_placed'])} orders")
                    for order in result["orders_placed"]:
                        log(f"  - {order['type']}: {order['side']} {order['lots']} lots @ {order['price']}")
                else:
                    log(f"❌ Strategy failed. Errors: {result['errors']}")
                    if result["orders_placed"]:
                        log(f"Partial success: {len(result['orders_placed'])} orders placed")
            elif args.action == "adaptive":
                log("🎯 Running Adaptive Strategy Engine...")
                result = adaptive_strategy_engine(page)
                if result["success"]:
                    log(f"✅ Adaptive strategy completed: {result['actions_taken']}")
                else:
                    log(f"❌ Adaptive strategy failed: {result['errors']}")
                if result["state_analysis"]:
                    state = result["state_analysis"]
                    log(f"📊 State: {state.get('state')}, Orders: {len(state.get('orders', []))}, Next: {state.get('next_action')}")
            elif args.action == "strategymonitor":
                log("🔄 Starting Strategy Monitor (continuous)...")
                reattach = lambda: connect_to_edge_existing_tab(DELTA_TRADE_URL, reuse_playwright=playwright)
                strategy_monitor_loop(page, reattach_cb=reattach)
            elif args.action == "test":
                log("🧪 Running Comprehensive Strategy Tests...")
                run_strategy_tests(page)
            elif args.action == "closeall":
                log("🚨 CLOSING ALL POSITIONS (DANGEROUS!)")
                result = close_all_positions(page)
                log(f"Result: {result}")
            elif args.action == "closepos":
                log("🚨 CLOSING BTCUSD POSITION (DANGEROUS!)")
                result = close_position_by_symbol(page, "BTCUSD")
                log(f"Result: {result}")
                # If UI close failed or position persists, try market close fallback
                try:
                    pos_after = extract_position_data(page)
                    size_txt = pos_after.get("size") or ""
                    still_open = re.search(r"[0-9]", size_txt) is not None
                except Exception:
                    still_open = False
                if (not result.get("success")) or still_open:
                    log("🧯 Row close did not clear position; attempting market close fallback…")
                    mres = close_position_market(page)
                    log(f"Fallback result: {mres}")
            elif args.action == "cancelall":
                log("🧹 CANCELLING ALL ORDERS")
                result = cancel_all_orders_button(page)
                log(f"Result: {result}")
            else:
                # Start monitoring positions
                reattach = lambda: connect_to_edge_existing_tab(DELTA_TRADE_URL, reuse_playwright=playwright)
                monitor_positions(page, reattach_cb=reattach)
        except Exception as e:
            log(f"❌ Attach/monitor error: {e}")
            log("If Edge isn't in CDP mode, close Edge and start it like this:")
            log(f"  msedge --remote-debugging-port={CDP_PORT}")
            return 1
        
        log("✅ Position monitoring completed!")
        return 0
        
    except KeyboardInterrupt:
        log("🛑 Interrupted by user.")
        return 0
    except Exception as e:
        log(f"❌ Fatal error: {e}")
        return 1
    finally:
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("🛑 Interrupted by user.")
        sys.exit(130)
