## Delta Exchange Edge RPA Bot

Playwright RPA that attaches to your existing Microsoft Edge session via DevTools (CDP) and monitors your BTCUSD Futures position. It implements the **Haider Strategy** with comprehensive testing and position management capabilities.

### Features
- **Edge-only CDP attachment**: Connects to existing session via 127.0.0.1:9222 (configurable)
- **Environment switching**: Demo vs live URL via `.env`
- **Position monitoring**: Size, Entry Price, Mark Price, uPNL tracking
- **Order management**: Place/cancel limit orders, strategy execution
- **Haider Strategy**: Automated trading strategy with AVG and TP orders
- **Comprehensive testing**: Full scenario testing framework
 **Enhanced logging**: Logs to both console and `debuuug/terminal_*.log` files
 `debuuug/terminal_*.log` — Persistent per-run logging for review and debugging
- **Streamlit UI**: Live dashboard with auto-attachment and real-time data
 All actions are logged to:
 - **Console**: Real-time feedback during execution
 - **Files**: `debuuug/terminal_*.log` per run for persistent review and debugging
 **Log Review**: Check `debuuug/terminal_*.log` for detailed execution history
 **debuuug/terminal_*.log**: Persistent execution logs with timestamps
- `app.py` — Streamlit UI with live data dashboard
- `debug/bot.log` — Persistent logging for review and debugging
- `requirements.txt` — Python dependencies
- `.env.example` — Sample environment configuration
- `Stratergy/` — Strategy documentation and specifications

### Setup (Windows PowerShell)
1) **Create virtual environment and install dependencies**
```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m playwright install
```

2) **Configure environment**
- Copy `.env.example` to `.env` and set `DELTA_ENV` to `demo` or `live`
- Demo URL: `https://demo.delta.exchange/app/futures/trade/BTC/BTCUSD`
- Live URL: `https://www.delta.exchange/app/futures/trade/BTC/BTCUSD`
- Optional: `DELTA_TRADE_URL` for explicit URL override
- Optional: `EDGE_ALLOW_KILL=1` enables automatic Edge restart with CDP
- Optional: `RPA_DIAG=1` enables verbose diagnostic logging

3) **Start Edge with DevTools (CDP)**
```powershell
msedge --remote-debugging-port=9222
```

4) **Open the trading page in Edge and ensure you're logged in**

### Usage

#### Basic Monitoring
```powershell
py .\bot.py
```

#### Strategy Actions
```powershell
# Run comprehensive strategy tests
py .\bot.py --action test

# Execute Haider Strategy on current position
py .\bot.py --action strategy

# Adaptive strategy (handles all scenarios)
py .\bot.py --action adaptive

# Continuous strategy monitoring
py .\bot.py --action strategymonitor
```

#### Order Management
```powershell
# Place limit orders
py .\bot.py --action long --price 116000 --lots 2
py .\bot.py --action short --price 118000 --lots 1

# Cancel orders (with optional filters)
py .\bot.py --action cancel --side sell --priceSubstr 118000

# Cancel all orders using UI button
py .\bot.py --action cancelall
```

#### Safety Controls (Demo/Testing Only)
```powershell
# Close specific position (DANGEROUS - market price!)
py .\bot.py --action closepos

# Close all positions (DANGEROUS - market price!)
py .\bot.py --action closeall
```

#### Data Analysis
```powershell
# Take position and orders snapshot
py .\bot.py --action snapshot
```

#### Streamlit Dashboard
```powershell
streamlit run .\app.py
```

### Logging and Debugging

All actions are logged to:
- **Console**: Real-time feedback during execution
- **File**: `debug/bot.log` for persistent review and debugging

Enable verbose diagnostics:
```powershell
# Set in .env file
RPA_DIAG=1
```

### Strategy Implementation

The bot implements the **Haider Strategy** with:
- **Two-order invariant**: Always maintains exactly 2 orders (AVG + TP)
- **Position progression**: 1 → 3 → 9 lot scaling
- **Adaptive engine**: Handles all starting scenarios automatically
- **Fill detection**: Monitors and responds to order fills
- **State management**: Tracks position, orders, and next required actions

### Safety Notes

⚠️ **Position closing actions are DANGEROUS in live trading:**
- `--action closepos` and `--action closeall` execute at market price
- Only use these in demo environments for testing
- They will cause immediate losses if the market is against you

✅ **Safe testing practices:**
- Always test new strategies in demo environment first
- Use `--action test` to validate all scenarios without real trades
- Review logs in `debug/bot.log` after each session
- Monitor the Streamlit dashboard for real-time feedback

### Notes
- Ensure the trading page is open in Edge with CDP enabled
- Position and order data refreshes automatically based on changes
- HTML snapshots saved to `html_snapshots/` on extraction failures
- All timestamps in logs are in local system time
- Strategy parameters are defined in `Stratergy/Haider Stratergy.md`
