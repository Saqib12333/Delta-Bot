---

applyTo: '**'

---

# CryptDash Delta RPA – Contributor and Agent Guide (Updated)

This document is the single source of truth for human developers and AI agents working on this project. It defines goals, environment, architecture, coding standards, workflows, and acceptance criteria.

## Project Goal

Automate the "Haider Strategy" on Delta Exchange 24x7 using an RPA bot built with Playwright. The bot should:
- Stay logged in reliably using a persistent Chrome profile and a cookies fallback.
- Read live state (Open Positions, Open Orders) from the BTCUSD Futures UI.
- Enforce the strategy's order-placement rules (two-order invariant: one TP opposite side, one AVG same side) with safety caps (MAX_LOTS).
- Be resilient (retries, timeouts, reloads), observable (logs, artifacts), and easy to operate on Windows.

## Current Implementation Status (Complete)

### Core Features ✅
- `bot.py` attaches to Microsoft Edge via CDP, scrapes Open Positions and Open Orders from Delta Exchange
- Enhanced logging to both console and `debug/bot.log` file for persistent review
- `app.py` provides a Streamlit dashboard with auto-start, CDP attachment, and background monitoring
- Complete Haider Strategy implementation with adaptive engine handling all scenarios

### Strategy Engine ✅
- **analyze_current_state()**: Comprehensive state analysis determining position lots, open orders, and next actions
- **adaptive_strategy_engine()**: Handles all starting scenarios (no position, position only, position + 1/2 orders)
- **implement_haider_strategy()**: Core strategy placing AVG (750 USD away, same direction) and TP (300 USD away, opposite direction) orders
- **strategy_monitor_loop()**: Continuous monitoring with fill detection and response

### Order Management ✅
- **create_long_order() / create_short_order()**: Tested and working order placement functions
- **cancel_open_orders()**: Selective order cancellation with filters
- **close_all_positions()**: Emergency position closing (DANGEROUS - for testing only)
- **close_position_by_symbol()**: Specific position closing (DANGEROUS - testing only)
- **cancel_all_orders_button()**: UI-based bulk order cancellation

### Testing Framework ✅
- **run_strategy_tests()**: Comprehensive testing of all strategy scenarios
- Simulation-based testing avoiding natural price movement delays
- State transition validation and fill response testing
- CLI integration: `py bot.py --action test`

### CLI Actions Available
- **monitor**: Default position monitoring
- **strategy**: Execute Haider Strategy on current position
- **adaptive**: Adaptive strategy handling all scenarios
- **strategymonitor**: Continuous strategy monitoring with fill detection
- **test**: Run comprehensive strategy tests
- **snapshot**: Take position and orders data snapshot
- **long/short**: Place specific limit orders
- **cancel**: Cancel orders with optional filters
- **closepos**: Close BTCUSD position (DANGEROUS - testing only)
- **closeall**: Close all positions (DANGEROUS - testing only)
- **cancelall**: Cancel all orders using UI button

## Environment and Tooling

- OS: Windows
- Shell: PowerShell
- Language: Python 3.10+ recommended
- Browser: Microsoft Edge (attach via CDP)
- Key libs: Playwright (Python), python-dotenv, logging

### Environment (.env)
- Use `.env` at project root. Example keys:
	- `DELTA_ENV`: `demo` or `live`
	- `DELTA_DEMO_URL` / `DELTA_LIVE_URL`
	- `DELTA_TRADE_URL` (optional explicit override)
	- `CDP_PORT` (default `9222`)
	- `EDGE_ALLOW_KILL` (optional `1` to allow auto-restart Edge with CDP)
	- `RPA_DIAG` (optional `1` to enable extra logs and one-time row dumps)

### Logging System
- **Console logging**: Real-time feedback during execution
- **File logging**: Persistent logs in `debug/bot.log` for review
- **Enhanced format**: Timestamps, log levels, and structured messages
- **Diagnostic mode**: `RPA_DIAG=1` enables verbose debugging

### Browser attach (Edge CDP)
- Start Edge with: `msedge --remote-debugging-port=9222`
- The bot attaches to the existing Edge instance and does not open a new window.
- Ensure the target trading tab is open in the same Edge session.
- The Streamlit UI also opens the Delta tab if missing and attaches to the same session.

### Setup Checklist (must follow in order)

1. Create and activate a virtual environment BEFORE any installs/tests:
	 - PowerShell: `py -m venv venv` then `.\venv\Scripts\Activate.ps1`
2. Install dependencies: `py -m pip install -r requirements.txt` then `py -m playwright install`
3. Run syntax checks: `py -m py_compile .\bot.py`
4. Test core functions: `py bot.py --action test`

## Runbook

### Basic Operations
- **Monitor**: `py .\bot.py` (default action)
- **Strategy Test**: `py .\bot.py --action test`
- **Execute Strategy**: `py .\bot.py --action strategy`
- **Continuous Monitoring**: `py .\bot.py --action strategymonitor`
- **Dashboard**: `streamlit run .\app.py`

### Order Operations (Demo Recommended)
- **Place Orders**: `py .\bot.py --action long --price 116000 --lots 2`
- **Cancel Orders**: `py .\bot.py --action cancel --side sell --priceSubstr 116000`
- **Emergency Close** (DANGEROUS): `py .\bot.py --action closepos`

### Debugging and Analysis
- **Snapshot**: `py .\bot.py --action snapshot`
- **Log Review**: Check `debug/bot.log` for detailed execution history
- **Diagnostic Mode**: Set `RPA_DIAG=1` in `.env` for verbose logging

### Artifacts and Logging
- **debug/bot.log**: Persistent execution logs with timestamps
- **html_snapshots/**: DOM snapshots when extraction fails
- **debug/**: Additional debugging artifacts

## Strategy Contract (Haider Strategy Implementation)

### Constants
- LOT_BTC = 0.001
- SEED_OFFSET = 50 USD (initial entry)
- AVG_STEP = 750 USD (first averaging distance)
- AVG_STEP_2 = 500 USD (second averaging distance)
- TP_STEP_1 = 300 USD (TP after 1 lot)
- TP_STEP_3 = 200 USD (TP after 3 lots)
- TP_STEP_9 = 100 USD (TP after 9 lots)
- AVG_MULT = 2 (lot multiplier for averaging)
- MAX_LOTS = 9 (maximum position size)

### State Management
- **Position tracking**: Lots, direction, average price
- **Order monitoring**: Real-time detection of fills
- **Two-order invariant**: Always maintain exactly 2 orders (AVG + TP)
- **State transitions**: 1→3→9 lot progression with proper TP sizing

### Strategy Flow
1. **Seed Phase**: Initial 1-lot position with AVG and TP orders
2. **Fill Detection**: Monitor which order fills first (AVG vs TP)
3. **AVG Fill Response**: Scale position, recalculate average, re-arm orders
4. **TP Fill Response**: Flip position to opposite side, restart with 1 lot
5. **Continuous Loop**: Maintain two-order invariant throughout

## Codebase Overview

### Core Files
- **bot.py**: Complete strategy implementation with adaptive engine
- **app.py**: Streamlit dashboard with real-time monitoring
- **debug/bot.log**: Persistent logging for troubleshooting and analysis
- **Stratergy/**: Strategy documentation and specifications

### Key Functions
- **analyze_current_state()**: State analysis and next action determination
- **adaptive_strategy_engine()**: Handles all scenario transitions
- **implement_haider_strategy()**: Core strategy execution
- **run_strategy_tests()**: Comprehensive testing framework
- **position/order extraction**: Robust UI scraping with error handling

## Coding Standards

- **Logging**: Use standard logging module, write to both console and file
- **Error Handling**: Fail fast with artifacts, return detailed error information
- **Safety**: DANGEROUS operations clearly marked and restricted to testing
- **Selectors**: Robust UI targeting with fallbacks and error snapshots
- **State Management**: Immutable operations, clear state transitions
- **Testing**: Comprehensive scenario coverage with simulation-based testing

## Testing and Validation

### Automated Testing
- **Syntax Check**: `py -m py_compile .\bot.py`
- **Strategy Tests**: `py .\bot.py --action test`
- **Scenario Coverage**: All starting states and transitions tested
- **Fill Simulation**: Order cancellation to simulate fills

### Manual Validation
- **Demo Environment**: Always test in demo before live
- **Order Verification**: Confirm orders appear in UI after placement
- **State Consistency**: Verify position and order counts match expectations
- **Log Review**: Check `debug/bot.log` for execution details

## Safety and Risk Management

### DANGEROUS Operations (Testing Only)
- **Position Closing**: `closepos` and `closeall` execute at market price
- **Emergency Use**: Only for testing scenarios or emergency exits
- **Demo Restriction**: Never use position closing in live environments with unrealized losses

### Safe Operations
- **Order Management**: Limit orders with price control
- **Strategy Monitoring**: Read-only position analysis
- **Testing Framework**: Simulation-based validation
- **State Analysis**: Non-invasive data extraction

### Best Practices
- **Always demo first**: Test all new strategies in demo environment
- **Log Review**: Regular review of `debug/bot.log` for issues
- **Gradual Scaling**: Start with minimum lot sizes
- **Monitor Continuously**: Use Streamlit dashboard for real-time oversight

## Operational Guidance

### 24x7 Operation
- **Task Scheduler**: Windows Task Scheduler for automated startup
- **Error Recovery**: Automatic page reloads and CDP reconnection
- **Logging Persistence**: All actions logged to file for post-mortem analysis
- **State Persistence**: Strategy state maintained across restarts

### Troubleshooting
- **CDP Issues**: Restart Edge with `--remote-debugging-port=9222`
- **UI Changes**: HTML snapshots saved for selector updates
- **Network Issues**: Automatic retries with exponential backoff
- **Log Analysis**: Detailed execution logs in `debug/bot.log`

## Acceptance Criteria (for PRs)

- **Environment Setup**: venv-first, no global installs
- **Syntax Validation**: Clean compilation with py_compile
- **Testing Coverage**: All strategy scenarios tested and passing
- **Logging Compliance**: All actions logged to file with timestamps
- **Safety Validation**: DANGEROUS operations clearly marked and restricted
- **Documentation**: README and instructions updated for new features
- **Artifact Management**: Debug files properly handled and gitignored

---
Last updated: 2025-08-23
