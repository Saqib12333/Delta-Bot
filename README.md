## Delta Exchange Edge RPA Bot

Playwright RPA that attaches to your existing Microsoft Edge session via DevTools (CDP) and monitors your BTCUSD Futures position. It does not open a new browser window; it attaches to the tab you already have open.

### Features
- Edge-only, attaches to existing session via CDP on 127.0.0.1:9222 (configurable)
- Environment switch: demo vs live URL via `.env`
- Monitors and prints: Size, Entry Price, Mark Price, UPNL
- Saves HTML snapshots to `html_snapshots/` on extraction failures for debugging

### Files
- `bot.py` — main RPA/monitor script
- `requirements.txt` — Python deps (Playwright, python-dotenv)
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

3) Start Edge with DevTools (CDP)
You must run Edge with a DevTools port so the bot can attach.
```
msedge --remote-debugging-port=9222
```

4) Run the bot
```
py .\bot.py
```

### Notes
- Ensure the trading page is open in the same Edge instance started with `--remote-debugging-port`.
- If attach fails, the bot prints guidance and saves snapshots under `html_snapshots/`.
- We’ll refine the column mapping next; the environment switch is now in place.
