import sys
import time
import os
import re
import webbrowser
import subprocess
import shutil
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page, Browser
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
# Load .env configuration early
load_dotenv(PROJECT_ROOT / ".env")
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

# Position monitoring settings
POSITIONS_INTERVAL = 10  # seconds
ORDERS_INTERVAL = 30     # seconds
BASE_LOOP_SLEEP = 1      # seconds


def log(msg: str) -> None:
    line = f"[RPA] {msg}"
    print(line)
    try:
        if LOG_FILE is not None:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def set_log_file(path: Path) -> None:
    global LOG_FILE
    LOG_FILE = path


def ensure_debug_dir() -> Path:
    debug_dir = PROJECT_ROOT / "debug"
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
    out: Dict[str, Any] = { "orders": [], "timestamp": ts }

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

        # small wait for table render
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

        # Find a likely Open Orders table by headers and row shape
        tables = page.locator("table")
        table = None
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
            for key in ("qty", "quantity", "size"):
                if key in hdr_l:
                    score += 2
            for key in ("price", "limit"):
                if key in hdr_l:
                    score += 2
            for key in ("type", "side"):
                if key in hdr_l:
                    score += 1
            # ensure has at least 1-2 data rows
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
        headers: List[str] = []
        try:
            ths = table.locator("thead th")
            headers = [t.strip() for t in ths.all_text_contents()] if ths.count() > 0 else []
        except Exception:
            headers = []
        header_map: Dict[str, int] = { (h or '').strip().lower(): i for i, h in enumerate(headers) }

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
        for r_i in range(min(row_count, 10)):  # safety cap
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
            qn_val = None
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
        return out
    except Exception as e:
        log(f"Error extracting open orders: {e}")
        save_dom_snapshot(page, label="open_orders_error")
        out["error"] = str(e)
        return out


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
