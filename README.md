## Delta Exchange Edge RPA Bot

Playwright RPA that attaches to your existing Microsoft Edge session via DevTools (CDP) and monitors your BTCUSD Futures position. It does not open a new browser window; it attaches to the tab you already have open.

### Features
- Edge-only, attaches to existing session via CDP on 127.0.0.1:9222 (configurable)
- Environment switch: demo vs live URL via `.env`
- Monitors and prints: Size, Entry Price, Mark Price, uPNL
- Fetches Open Orders (max two for BTCUSD) and refreshes them periodically (30s)
- Saves HTML snapshots to `html_snapshots/` on extraction failures for debugging
- Optional diagnostic logging via `RPA_DIAG=1` (suppresses by default the verbose row dumps and per-tick logs)
- Streamlit UI (`app.py`) that auto-attaches to Edge and displays the same data in a live dashboard; logs print to terminal
 - New: UI-driven order actions — place maker-only limit orders and cancel open orders from the CLI

### Files
- `bot.py` — main RPA/monitor script (attaches to Edge CDP, scrapes Positions and Open Orders)
- `app.py` — Streamlit UI (auto-attaches to Edge CDP in a background worker and renders live data)
- `requirements.txt` — Python deps (Playwright, python-dotenv, streamlit)
- `.env.example` — sample environment file
- `.gitignore` — ignores venv, logs, debug artifacts, env files

### Setup (Windows PowerShell)
1) Create/activate a virtual environment and install deps
```
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m playwright install
```

2) Configure environment
- Copy `.env.example` to `.env` and set `DELTA_ENV` to `demo` or `live`.
- Demo URL: `https://demo.delta.exchange/app/futures/trade/BTC/BTCUSD`
- Live URL: `https://www.delta.exchange/app/futures/trade/BTC/BTCUSD`
- Optional override: set `DELTA_TRADE_URL` explicitly.
- Optional: `EDGE_ALLOW_KILL=1` allows the bot to close an already running non-CDP Edge and relaunch with CDP automatically.
- Optional: `RPA_DIAG=1` enables extra logs (row dumps and per-tick position change logs). Default is off.

3) Start Edge with DevTools (CDP)
You must run Edge with a DevTools port so the bot can attach.
```
msedge --remote-debugging-port=9222
```

4) Run the bot
```
py .\bot.py
```

5) Run the UI (optional)
```
streamlit run .\app.py
```
The UI auto-opens the Delta trading tab (if needed), ensures CDP, then attaches and streams data. Logs are printed to the same terminal.

### Order actions (advanced)
The bot can place/cancel orders through the UI using the existing Edge session. Use these optional flags:

- Place a maker-only limit order (price in USD, lots are 1=0.001 BTC):
```
py .\bot.py --action place --side buy --price 60000 --lots 2
```
- Cancel open orders (optionally filter by side and/or price substring):
```
py .\bot.py --action cancel --side sell --priceSubstr 60000
```
If no `--action` is provided, the bot runs in monitoring mode.

### Notes
- Ensure the trading page is open in the same Edge instance started with `--remote-debugging-port`.
- If attach fails, the bot prints guidance and saves snapshots under `html_snapshots/`.
- Open Orders are updated only when the position size changes; a compact summary is logged.
- Set `RPA_DIAG=1` to show one-time row index dumps and per-tick position change logs for debugging.
- For a live dashboard, run the Streamlit app (`app.py`); it attaches to the same Edge tab and shows the same data with a modern UI.
