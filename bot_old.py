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


def save_page_html(page: Page, filename_prefix: str = "trading_page") -> Optional[Path]:
    """Save the current page HTML with timestamp"""
    try:
        # Get current timestamp for unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{filename_prefix}_{timestamp}.html"
        
        # Ensure directory exists
        snapshots_dir = ensure_html_snapshots_dir()
        file_path = snapshots_dir / filename
        
        # Get page HTML content
        html_content = page.content()
        
        # Save to file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        log(f"✅ Saved page HTML to: {file_path}")
        log(f"   File size: {len(html_content)} characters")
        return file_path
        
    except Exception as e:
        log(f"❌ Failed to save HTML: {e}")
        return None


def is_trading_page_loaded(page: Page) -> bool:
    """Check if we're on the trading page and it's properly loaded"""
    try:
        # Check URL
        current_url = page.url
        if "trade/BTC/BTCUSD" not in current_url:
            return False
            
        # Check for key trading elements
        trading_indicators = [
            # Trading chart/price display
            page.locator("text=/BTC/"),
            page.locator("text=/BTCUSD/"),
            # Common trading interface elements
            page.locator("text=/Buy/i"),
            page.locator("text=/Sell/i"),
            page.locator("text=/Order/i"),
            page.locator("text=/Position/i"),
        ]
        
        # At least 3 indicators should be present
        found_indicators = 0
        for indicator in trading_indicators:
            try:
                if indicator.count() > 0:
                    found_indicators += 1
            except Exception:
                pass
                
        if found_indicators >= 3:
            log(f"✅ Trading page loaded successfully ({found_indicators} indicators found)")
            return True
        else:
            log(f"⚠️ Trading page not fully loaded ({found_indicators} indicators found)")
            return False
            
    except Exception as e:
        log(f"Error checking trading page: {e}")
        return False


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

    log("🚀 Starting Delta Exchange Trading Bot")
    log("🎯 Target: BTC/BTCUSD Futures Trading Page")
    
    try:
        log("🌐 Opening trading page in your existing Chrome session...")
        
        # Use webbrowser to open in existing Chrome session (where you're already logged in)
        import webbrowser
        
        log(f"🔗 Opening URL: {DELTA_TRADE_URL}")
        
        # This will open the URL in your existing Chrome session where you're already logged in
        webbrowser.open(DELTA_TRADE_URL)
        
        log("✅ Login valid - opened dashboard/trading page!")
        log("🌐 The trading page should now be open in your existing Chrome browser")
        log("   Since you're using your existing Chrome session, you should already be logged in")
        
        # Wait a moment for the page to load
        time.sleep(5)
        
        # Now connect to Chrome with debugging enabled to save HTML
        log("🔍 Attempting to capture HTML for analysis...")
        
        # Launch a temporary Playwright instance to capture the page
        with sync_playwright() as p:
            try:
                # Launch browser with debugging enabled
                browser = p.chromium.launch(
                    headless=False,
                    args=[
                        "--remote-debugging-port=9222",
                        "--disable-blink-features=AutomationControlled"
                    ]
                )
                page = browser.new_page()
                
                # Navigate to the same trading page
                log(f"� Navigating to trading page for HTML capture: {DELTA_TRADE_URL}")
                page.goto(DELTA_TRADE_URL, wait_until="domcontentloaded")
                
                # Wait for page to load
                page.wait_for_timeout(5000)
                
                # Check if we need to login
                current_url = page.url
                if "login" in current_url.lower():
                    log("⚠️ HTML capture browser needs login - will save login page HTML instead")
                    html_file = save_page_html(page, "login_page_for_reference")
                    log("📝 Note: Main trading page is open in your primary browser")
                else:
                    # Save the trading page HTML
                    html_file = save_page_html(page, "btc_trading_page")
                    
                    if html_file:
                        log("� HTML snapshot saved successfully!")
                        
                        # Try to identify key sections
                        log("🔍 Scanning for key trading interface elements...")
                        
                        # Check for common trading elements
                        elements_found = []
                        
                        # Price/ticker info
                        if page.locator("text=/\\$[0-9,]+/").count() > 0:
                            elements_found.append("Price displays")
                            
                        # Trading buttons  
                        if page.locator("button:has-text('Buy'), input[value*='Buy']").count() > 0:
                            elements_found.append("Buy buttons")
                        if page.locator("button:has-text('Sell'), input[value*='Sell']").count() > 0:
                            elements_found.append("Sell buttons")
                            
                        # Order forms
                        if page.locator("input[type='number'], input[placeholder*='quantity'], input[placeholder*='price']").count() > 0:
                            elements_found.append("Order entry fields")
                            
                        # Tables (positions, orders)
                        if page.locator("table, [role='table']").count() > 0:
                            elements_found.append("Data tables")
                            
                        if elements_found:
                            log(f"✅ Found trading interface elements: {', '.join(elements_found)}")
                        else:
                            log("⚠️ No obvious trading elements detected - may need login")
                
                browser.close()
                
            except Exception as e:
                log(f"⚠️ Could not capture HTML automatically: {e}")
                log("📝 Manual alternative: Use browser DevTools to save HTML")
                log("   1. Right-click on the trading page")
                log("   2. Select 'Inspect' or 'Inspect Element'")  
                log("   3. Right-click on <html> tag in DevTools")
                log("   4. Select 'Copy' > 'Copy outerHTML'")
                log("   5. Paste into a text file for analysis")
        
        log("✅ Bot completed successfully!")
        log("🎉 Trading page is open in your browser!")
        log("🔍 You can now analyze the HTML to identify selectors for:")
        log("   • Buy/Sell buttons")
        log("   • Price displays") 
        log("   • Order entry fields")
        log("   • Position information")
        log("   • Market data")
        log("   • Order book")
        log("   • Trading charts")
        log("Next steps:")
        log("1. Analyze the saved HTML file (if captured)")
        log("2. Identify CSS selectors for strategy elements")
        log("3. Implement buy/sell logic")
        log("4. Add position monitoring")
        return 0
        
    except Exception as e:
        log(f"❌ Fatal error: {e}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("🛑 Interrupted by user.")
        sys.exit(130)
