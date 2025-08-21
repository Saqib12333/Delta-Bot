## Delta Exchange RPA Bot

This repo includes a Playwright-powered RPA bot that opens Delta Exchange, stays logged in using a persistent profile or cookies, and scrapes Open Positions and Open Orders from the BTCUSD Futures page.

### What it does now
- Launches Google Chrome (system-installed) with a persistent user data directory stored locally.
- Attempts login via existing session; if not, loads cookies from `cookies.pkl` as a fallback.
- If still not logged in, prompts you to log in manually and press Enter. Cookies are then saved to `cookies.pkl` for future runs.
- Navigates to: https://www.delta.exchange/app/futures/trade/BTC/BTCUSD
- Scrapes Open Positions and Open Orders and stores a snapshot under `debug/` (HTML, screenshot, and a structured text file).

### Files
- `rpa_delta_bot.py` — the RPA script.
- `requirements.txt` — Python dependency list (Playwright).
- `.gitignore` — ignores virtual envs, artifacts, cookies, and large docs.

### Setup (Windows PowerShell)
1) Create/activate a virtual environment and install deps (activate venv BEFORE installing)
```
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m playwright install --with-deps
```

2) Run the bot (ensure venv is activated)
```
py .\rpa_delta_bot.py
```

3) First run notes
- If you aren’t already logged in through the persistent profile, the script will try `cookies.pkl` if present.
- If login still isn’t detected, the browser is open for manual login. Complete login, then return to the terminal and press Enter.
- Cookies will be saved to `cookies.pkl` in the project root for next time.

4) Browser
- The script uses your system-installed Google Chrome via Playwright's Chrome channel. Ensure Chrome is installed and up to date.

5) Outputs
- `debug/trade_page.html` and `debug/trade_page.png` for troubleshooting UI selectors.
- `debug/btc_usd_positions_orders.txt` containing parsed Positions and Orders.

We’ll extend this bot to implement the Haider Strategy after verifying the data extraction is stable.
