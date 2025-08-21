import sys
import time
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page


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


def extract_position_data(page: Page) -> Dict[str, Any]:
    """Extract position data from the trading page"""
    try:
        position_data = {
            "size": None,
            "entry_price": None,
            "mark_price": None,
            "upnl": None,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # Try different selectors for position information
        # Look for position size
        size_selectors = [
            "text=/Size.*[0-9]/",
            "[data-testid*='position'] text=/[0-9]+.*BTC/",
            "text=/Position.*[0-9]/",
            ".position-size",
            "[title*='size'], [title*='Size']"
        ]
        
        for selector in size_selectors:
            try:
                element = page.locator(selector).first
                if element.count() > 0:
                    text = element.text_content()
                    if text and any(char.isdigit() for char in text):
                        position_data["size"] = text.strip()
                        break
            except Exception:
                continue
        
        # Look for entry price
        entry_selectors = [
            "text=/Entry.*\\$[0-9,]+/",
            "text=/Avg Price.*\\$[0-9,]+/",
            "[data-testid*='entry'] text=/\\$[0-9,]+/",
            ".entry-price",
            "[title*='entry'], [title*='Entry']"
        ]
        
        for selector in entry_selectors:
            try:
                element = page.locator(selector).first
                if element.count() > 0:
                    text = element.text_content()
                    if text and '$' in text:
                        position_data["entry_price"] = text.strip()
                        break
            except Exception:
                continue
        
        # Look for mark price
        mark_selectors = [
            "text=/Mark.*\\$[0-9,]+/",
            "text=/Mark Price.*\\$[0-9,]+/",
            "[data-testid*='mark'] text=/\\$[0-9,]+/",
            ".mark-price",
            "[title*='mark'], [title*='Mark']"
        ]
        
        for selector in mark_selectors:
            try:
                element = page.locator(selector).first
                if element.count() > 0:
                    text = element.text_content()
                    if text and '$' in text:
                        position_data["mark_price"] = text.strip()
                        break
            except Exception:
                continue
        
        # Look for UPNL (Unrealized PNL)
        upnl_selectors = [
            "text=/UPNL.*[+-]?\\$[0-9,]+/",
            "text=/Unrealized.*[+-]?\\$[0-9,]+/",
            "text=/PnL.*[+-]?\\$[0-9,]+/",
            "[data-testid*='pnl'] text=/[+-]?\\$[0-9,]+/",
            ".unrealized-pnl, .upnl",
            "[title*='unrealized'], [title*='pnl'], [title*='PnL']"
        ]
        
        for selector in upnl_selectors:
            try:
                element = page.locator(selector).first
                if element.count() > 0:
                    text = element.text_content()
                    if text and ('$' in text or '+' in text or '-' in text):
                        position_data["upnl"] = text.strip()
                        break
            except Exception:
                continue
        
        # Try to get current BTC price as mark price if not found
        if not position_data["mark_price"]:
            price_selectors = [
                "text=/\\$[0-9]{4,6}/",  # Look for prices in the $20000+ range
                "[data-testid*='price'] text=/\\$[0-9,]+/",
                ".current-price, .ticker-price"
            ]
            
            for selector in price_selectors:
                try:
                    elements = page.locator(selector)
                    for i in range(min(3, elements.count())):  # Check first 3 matches
                        text = elements.nth(i).text_content()
                        if text and '$' in text:
                            # Extract numeric value to check if it's a reasonable BTC price
                            import re
                            price_match = re.search(r'\$([0-9,]+)', text)
                            if price_match:
                                price_val = int(price_match.group(1).replace(',', ''))
                                if 20000 <= price_val <= 150000:  # Reasonable BTC price range
                                    position_data["mark_price"] = text.strip()
                                    break
                except Exception:
                    continue
                if position_data["mark_price"]:
                    break
        
        return position_data
        
    except Exception as e:
        log(f"Error extracting position data: {e}")
        return {
            "size": None,
            "entry_price": None,
            "mark_price": None,
            "upnl": None,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(e)
        }


def format_position_display(data: Dict[str, Any]) -> str:
    """Format position data for display"""
    timestamp = data.get("timestamp", "Unknown")
    size = data.get("size", "No position")
    entry = data.get("entry_price", "N/A")
    mark = data.get("mark_price", "N/A")
    upnl = data.get("upnl", "N/A")
    
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


def connect_to_edge_browser() -> tuple[Page, Any]:
    """Connect to Edge browser with debugger port"""
    try:
        log("🌐 Attempting to connect to Edge browser...")
        
        # Launch Playwright with Edge browser
        playwright = sync_playwright().start()
        
        # Try to connect to existing Edge instance with debugging enabled
        try:
            browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
            log("✅ Connected to existing Edge browser instance")
        except Exception:
            log("⚠️ No existing Edge debug instance found, launching new one...")
            browser = playwright.chromium.launch(
                channel="msedge",  # Use Microsoft Edge
                headless=False,
                args=[
                    "--remote-debugging-port=9222",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--disable-default-apps"
                ]
            )
            log("✅ Launched new Edge browser instance")
        
        # Get the default context or create new page
        if browser.contexts:
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
        else:
            context = browser.new_context()
            page = context.new_page()
        
        # Navigate to trading page
        log(f"🎯 Navigating to: {DELTA_TRADE_URL}")
        page.goto(DELTA_TRADE_URL, wait_until="domcontentloaded")
        
        # Wait for page to load
        page.wait_for_timeout(3000)
        
        return page, playwright
        
    except Exception as e:
        log(f"❌ Failed to connect to Edge browser: {e}")
        raise


def main() -> int:
    debug_dir = ensure_debug_dir()
    set_log_file(debug_dir / "run.log")

    log("🚀 Starting Delta Exchange Position Monitor")
    log("🎯 Target: BTC/BTCUSD Futures Trading Page")
    log("🌐 Browser: Microsoft Edge")
    
    try:
        log("🌐 Opening trading page in Microsoft Edge...")
        
        # First, open the URL in Edge to ensure you're logged in
        log(f"🔗 Opening URL in default browser (Edge): {DELTA_TRADE_URL}")
        webbrowser.open(DELTA_TRADE_URL)
        
        log("✅ Trading page opened in Edge!")
        log("⏳ Waiting 5 seconds for page to load...")
        time.sleep(5)
        
        # Now connect to Edge with debugging to monitor positions
        log("🔧 Connecting to Edge browser for position monitoring...")
        
        try:
            page, playwright = connect_to_edge_browser()
            
            # Check if we're on the correct page
            current_url = page.url
            if "trade/BTC/BTCUSD" in current_url:
                log("✅ Connected to BTC/BTCUSD trading page")
            else:
                log(f"⚠️ Current page: {current_url}")
                log("🎯 Navigating to BTC/BTCUSD trading page...")
                page.goto(DELTA_TRADE_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            
            # Start monitoring positions
            log("🔍 Starting position monitoring...")
            monitor_positions(page)
            
        except Exception as e:
            log(f"❌ Could not connect to Edge browser for monitoring: {e}")
            log("💡 Manual monitoring instructions:")
            log("   1. Open Edge browser")
            log("   2. Navigate to the trading page")
            log("   3. Check your positions manually")
            log("   4. To enable automatic monitoring:")
            log("      - Start Edge with: msedge --remote-debugging-port=9222")
            log("      - Then run this script again")
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
            if 'playwright' in locals():
                playwright.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("🛑 Interrupted by user.")
        sys.exit(130)
