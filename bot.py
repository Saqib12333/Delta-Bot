import sys
import time
import os
import re
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page, Browser


PROJECT_ROOT = Path(__file__).resolve().parent
# Create html_snapshots directory for saving page content
HTML_SNAPSHOTS_DIR = PROJECT_ROOT / "html_snapshots"
LOG_FILE: Optional[Path] = None

# Direct trading page URL (assuming we're already logged in)
DELTA_TRADE_URL = "https://demo.delta.exchange/app/futures/trade/BTC/BTCUSD"

# Position monitoring settings
MONITORING_INTERVAL = 5  # Check positions every 5 seconds


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
        # Optional: ensure Positions tab is active (non-intrusive)
        try:
            tab = page.get_by_role("tab", name=re.compile("^Positions", re.I)).first
            if tab and tab.count() > 0:
                sel = (tab.get_attribute("aria-selected") or "").lower()
                if sel == "false":
                    tab.click(timeout=1000)
        except Exception:
            pass

        # Find the row that contains our symbol in any cell
        row = page.locator("tr:has(td:has-text('BTCUSD'))").first
        if row.count() == 0:
            # Try alternative: span/div based cells
            row = page.locator("tr:has(:text('BTCUSD'))").first

        if row.count() == 0:
            # Couldn't find row; save snapshot to debug
            save_dom_snapshot(page, label="positions_not_found")
            return result

        # Scroll row into view for virtualized tables
        try:
            row.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass

    # Identify the header cells from the same table
        table = row.locator("xpath=ancestor::table[1]")
        headers = []
        try:
            header_locs = table.locator("thead th")
            headers = [h.strip() for h in header_locs.all_text_contents()] if header_locs.count() > 0 else []
        except Exception:
            headers = []

        # Build header index mapping
        header_map: Dict[str, int] = {}
        for idx, h in enumerate(headers):
            hl = h.lower()
            header_map[hl] = idx

        def col_index_for(names: List[str]) -> Optional[int]:
            if not headers:
                return None
            for n in names:
                nlow = n.lower()
                for hl, i in header_map.items():
                    if nlow in hl:
                        return i
            return None

        # Helper: get td by attribute matching label (data-title/aria-label/title)
        def cell_by_label(labels: List[str]) -> Optional[str]:
            try:
                tds = row.locator("td")
                count = tds.count()
                for i in range(count):
                    td = tds.nth(i)
                    attrs = []
                    for attr in ("data-title", "aria-label", "title", "data-column", "data-col"):
                        try:
                            v = td.get_attribute(attr)
                            if v:
                                attrs.append(v)
                        except Exception:
                            pass
                    joined = "|".join(a.lower() for a in attrs)
                    for lbl in labels:
                        if lbl.lower() in joined:
                            txt = td.inner_text(timeout=1000).strip()
                            return txt if txt else None
            except Exception:
                return None
            return None

        # First try attribute-based matching
        result["size"] = result["size"] or cell_by_label(["Size"])
        result["entry_price"] = result["entry_price"] or cell_by_label(["Entry Price", "Avg Price"])
        result["mark_price"] = result["mark_price"] or cell_by_label(["Mark Price"]) 
        result["upnl"] = result["upnl"] or cell_by_label(["UPNL", "Unrealized PnL", "Unrealised PnL"]) 

        # If attributes could not be used, fall back to header index + offset alignment
        if any(v is None for v in (result["size"], result["entry_price"], result["mark_price"], result["upnl"])):
            size_idx = col_index_for(["size"])
            entry_idx = col_index_for(["entry price", "avg price"])
            mark_idx = col_index_for(["mark price"])
            upnl_idx = col_index_for(["upnl", "unrealized", "unrealised"])

            def cell_text_with_offset(col_idx: Optional[int]) -> Optional[str]:
                if col_idx is None:
                    return None
                try:
                    tds = row.locator("td")
                    td_count = tds.count()
                    hdr_count = len(headers)
                    offset = max(td_count - hdr_count, 0)
                    nth = col_idx + 1 + offset
                    if nth < 1 or nth > td_count:
                        return None
                    cell = tds.nth(nth - 1)
                    txt = cell.inner_text(timeout=1000).strip()
                    return txt if txt else None
                except Exception:
                    return None

            result["size"] = result["size"] or cell_text_with_offset(size_idx)
            result["entry_price"] = result["entry_price"] or cell_text_with_offset(entry_idx)
            result["mark_price"] = result["mark_price"] or cell_text_with_offset(mark_idx)
            result["upnl"] = result["upnl"] or cell_text_with_offset(upnl_idx)

        # If attribute/header strategies fail or for extra safety, align relative to the Symbol cell
        try:
            tds = row.locator("td")
            td_count = tds.count()
            td_texts: List[str] = []
            for i in range(td_count):
                try:
                    td_texts.append((tds.nth(i).inner_text(timeout=800) or "").strip())
                except Exception:
                    td_texts.append("")

            # Find index of the symbol cell that contains BTCUSD
            symbol_idx = -1
            for i, txt in enumerate(td_texts):
                if "btcusd" in txt.lower():
                    symbol_idx = i
                    break

            def get_td(idx: int) -> Optional[str]:
                if 0 <= idx < td_count:
                    val = td_texts[idx].strip()
                    return val if val else None
                return None

            if symbol_idx != -1:
                # Based on observed layout after Symbol:
                # Size(+1), Notional(+2), Entry Price(+3), TP/SL(+4), Index Price(+5), Mark Price(+6), Est. Liq.(+7), Margin(+8), Auto Top-up(+9), UPNL(+10)
                size_rel = get_td(symbol_idx + 1)
                entry_rel = get_td(symbol_idx + 3)
                mark_rel = get_td(symbol_idx + 6)
                upnl_rel = get_td(symbol_idx + 10)

                # Only overwrite if missing or clearly mismapped
                if result["size"] is None or (result["size"] and result["size"].isdigit()):
                    result["size"] = size_rel or result["size"]
                result["entry_price"] = entry_rel or result["entry_price"]
                result["mark_price"] = mark_rel or result["mark_price"]
                result["upnl"] = upnl_rel or result["upnl"]
        except Exception:
            pass

        # Fallbacks if table headers unavailable (use scoped search within the row only)
        def first_text_scoped(selectors: List[str]) -> Optional[str]:
            for sel in selectors:
                try:
                    loc = row.locator(sel).first
                    if loc.count() == 0:
                        continue
                    txt = loc.inner_text(timeout=800)
                    if txt:
                        return txt.strip()
                except Exception:
                    continue
            return None

        if result["size"] is None:
            result["size"] = first_text_scoped([
                "td:has-text('BTC')",
                "td:has-text('contracts')",
                "td:has([class*='size'])",
            ])

        if result["entry_price"] is None:
            result["entry_price"] = first_text_scoped([
                "td:has-text('$')",
                "td:has([class*='entry'])",
            ])

        if result["mark_price"] is None:
            result["mark_price"] = first_text_scoped([
                "td:has-text('Mark')",
                "td:has([class*='mark'])",
            ])

        if result["upnl"] is None:
            result["upnl"] = first_text_scoped([
                "td:has-text('UPNL')",
                "td:has([class*='pnl'])",
            ])

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


def monitor_positions(page: Page) -> None:
    """Continuously monitor position data"""
    log("🔍 Starting position monitoring...")
    log("📊 Monitoring: Size, Entry Price, Mark Price, UPNL")
    log(f"⏱️ Update interval: {MONITORING_INTERVAL} seconds")
    log("🛑 Press Ctrl+C to stop monitoring")
    
    # Initial wait to allow the page to finish rendering
    try:
        log("⏳ Waiting 10 seconds for page to fully render…")
        time.sleep(10)
    except Exception:
        pass

    last_display = ""
    
    try:
        while True:
            try:
                # Extract current position data
                data = extract_position_data(page)
                
                # Format for display
                display = format_position_display(data)
                
                # Only print if data has changed or every 60 seconds
                if display != last_display:
                    print(display)
                    last_display = display
                    
                    # Log position changes
                    if any([data.get("size"), data.get("entry_price"), data.get("upnl")]):
                        log(f"📊 Position Update - Size: {data.get('size', 'N/A')}, "
                            f"Entry: {data.get('entry_price', 'N/A')}, "
                            f"Mark: {data.get('mark_price', 'N/A')}, "
                            f"UPNL: {data.get('upnl', 'N/A')}")
                
                # Wait before next check
                time.sleep(MONITORING_INTERVAL)
                
            except KeyboardInterrupt:
                log("🛑 Position monitoring stopped by user")
                break
            except Exception as e:
                log(f"❌ Error during monitoring: {e}")
                time.sleep(MONITORING_INTERVAL)  # Continue monitoring despite errors
                
    except Exception as e:
        log(f"❌ Fatal monitoring error: {e}")


def connect_to_edge_existing_tab(target_url: str, timeout_s: int = 20):
    """Attach to existing Edge via CDP and return (page, playwright) for the matching tab.

    IMPORTANT: Edge must be running with --remote-debugging-port=9222.
    This function will NOT launch a new Edge window.
    """
    log("🌐 Attaching to existing Edge (CDP) at http://127.0.0.1:9222 …")
    playwright = sync_playwright().start()
    try:
        browser: Browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
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

        return page, playwright
    except Exception as e:
        log("❌ Could not attach to existing Edge. Ensure Edge is started with --remote-debugging-port=9222.")
        log("Tip: Close Edge completely, then run: msedge --remote-debugging-port=9222")
        raise


def main() -> int:
    debug_dir = ensure_debug_dir()
    set_log_file(debug_dir / "run.log")

    log("🚀 Starting Delta Exchange Position Monitor")
    log("🎯 Target: BTC/BTCUSD Futures Trading Page")
    log("🌐 Browser: Microsoft Edge")
    
    playwright = None
    try:
        # 1) Open only in your current Edge session (does not launch separate Edge in code)
        log(f"🔗 Opening URL in default browser (Edge): {DELTA_TRADE_URL}")
        webbrowser.open(DELTA_TRADE_URL)
        log("⏳ Waiting a moment for the tab to appear…")
        time.sleep(3)

        # 2) Attach ONLY to existing Edge (no new window)
        try:
            page, playwright = connect_to_edge_existing_tab(DELTA_TRADE_URL)
            log("✅ Attached to the existing Edge trading tab")

            # Start monitoring positions
            monitor_positions(page)
        except Exception as e:
            log(f"❌ Attach/monitor error: {e}")
            log("If Edge isn't in CDP mode, close Edge and start it like this:")
            log("  msedge --remote-debugging-port=9222")
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
