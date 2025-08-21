applyTo: '*/**'
---

# CryptDash Delta RPA – Contributor and Agent Guide

This document is the single source of truth for human developers and AI agents working on this project. It defines goals, environment, architecture, coding standards, workflows, and acceptance criteria.

## Project Goal

Automate the “Haider Strategy” on Delta Exchange 24x7 using an RPA bot built with Playwright. The bot should:
- Stay logged in reliably using a persistent Chrome profile and a cookies fallback.
- Read live state (Open Positions, Open Orders) from the BTCUSD Futures UI.
- Enforce the strategy’s order-placement rules (two-order invariant: one TP opposite side, one AVG same side) with safety caps (MAX_LOTS).
- Be resilient (retries, timeouts, reloads), observable (logs, artifacts), and easy to operate on Windows.

## Current Scope (MVP in repo)

- `bot.py` attaches to Microsoft Edge via CDP, then scrapes Open Positions and Open Orders from:
	https://www.delta.exchange/app/futures/trade/BTC/BTCUSD
- Saves debug artifacts under `debug/` and `html_snapshots/` (HTML snapshots when extraction fails).
 - `app.py` provides a Streamlit dashboard that auto-starts, opens the Delta tab, ensures CDP, and runs scraping in a background thread. It prints INFO logs to terminal and renders data in the UI without any buttons.

Next iterations will wire the scraped data into strategy state and implement order placement/cancellation.

## Environment and Tooling

- OS: Windows
- Shell: PowerShell
- Language: Python 3.10+ recommended
- Browser: Microsoft Edge (attach via CDP)
- Key libs: Playwright (Python), python-dotenv

### Environment (.env)
- Use `.env` at project root. Example keys:
	- `DELTA_ENV`: `demo` or `live`
	- `DELTA_DEMO_URL` / `DELTA_LIVE_URL`
	- `DELTA_TRADE_URL` (optional explicit override)
	- `CDP_PORT` (default `9222`)
	- `EDGE_ALLOW_KILL` (optional `1` to allow auto-restart Edge with CDP)
	- `RPA_DIAG` (optional `1` to enable extra logs and one-time row dumps)
 - The UI app sets `EDGE_ALLOW_KILL=1` by default to ensure CDP is available.

### Browser attach (Edge CDP)
- Start Edge with: `msedge --remote-debugging-port=9222`
- The bot attaches to the existing Edge instance and does not open a new window.
- Ensure the target trading tab is open in the same Edge session.
 - The Streamlit UI also opens the Delta tab if missing and attaches to the same session.

### Setup Checklist (must follow in order)

1. Create and activate a virtual environment BEFORE any installs/tests:
	 - PowerShell
		 - `py -m venv venv`
		 - `.\venv\Scripts\Activate.ps1`
2. Install dependencies:
 	 - `py -m pip install -r requirements.txt`
 	 - `py -m playwright install`
3. Run quick syntax checks before committing changes:
	 - `py -m py_compile .\bot.py`

## Runbook

Manual, one-off run:
- Ensure venv is active
- Configure `.env` (`DELTA_ENV=demo|live`)
- Start Edge with CDP: `msedge --remote-debugging-port=9222`
- `py .\bot.py`

Streamlit dashboard:
- Ensure venv is active, then `streamlit run .\app.py`
- The app auto-opens the Delta tab, ensures CDP, starts Playwright in a background thread, and prints logs to terminal.

Artifacts:
- `html_snapshots/*` for DOM snapshots when extraction fails
- `debug/run.log` for operational logging

## Strategy Contract (from Stratergy/Haider Stratergy.md)

Constants:
- LOT_BTC = 0.001
- SEED_OFFSET = 50 USD
- AVG_STEP = 750 USD
- TP_STEP = 500 USD
- AVG_MULT = 2
- MAX_LOTS = 27

State fields we must derive/maintain:
- `position.side` in {NONE, LONG, SHORT}
- `position.open_lots` in {0, 1, 3, 9, 27}
- `position.avg_price` (BTCUSD)
- `open_orders` list (max length = 2)

Primary flows:
- Idle → Seed: place a seed limit order at mark ± SEED_OFFSET sized 1 lot.
- On Seed Fill → PositionOpen(1): place TP(opposite, qty=open_lots+1) and AVG_ADD(same side, qty=open_lots*AVG_MULT).
- On AVG_ADD Fill: recompute avg/qty; re-arm TP; enforce 2-order invariant.
- On TP Fill (Flip): flip side with 1 lot, cancel other orders, re-arm TP+AVG.
- Enforce MAX_LOTS and keep at most 2 live orders.

## Codebase Overview

- `bot.py`: Entrypoint. Major functions:
	- `extract_position_data(page)`: robust scraping of the Positions row for BTCUSD
	- `extract_open_orders(page)`: clicks the "Open Orders" tab and parses up to two orders (qty, size, limit price, side)
	- `_activate_tab(page, name_regex)`: helper to switch tabs safely
	- `monitor_positions(page, reattach_cb)`: continuous loop, reattaches if the tab closes; refreshes open orders only when position size changes
	- `main()`: attach to Edge CDP reliably, preflight CDP, and start monitoring

- `README.md`: End-user oriented instructions (how to install/run).
- `.gitignore`: ignores env, artifacts, `.pw-user-data/`, `cookies.pkl`, PDFs.
- `Stratergy/Haider Stratergy.md`: Strategy definition and automation status.

## Coding Standards

- Keep public behavior stable; prefer additive changes.
- Logging: Prefer standard logging in the UI (`logging.INFO` to terminal). In the bot, keep logs succinct; guard verbose dumps behind `RPA_DIAG`.
- Selectors: prefer robust strategies (role, headings, contains text). Save HTML/screenshot when selectors fail.
- Error handling: fail fast with artifacts saved; return non-zero exit codes on unrecoverable errors.
- Keep side effects confined to project root (debug/, .pw-user-data/, cookies.pkl).

## Testing and Validation

- Quick checks: `py -m py_compile .\rpa_delta_bot.py`
- Manual smoke test: run the bot, confirm debug artifacts and data rows > 0 when positions/orders exist.
- Add lightweight unit tests for pure functions when introduced (e.g., table parsing helpers with sample HTML).

## Extending to Full Strategy Automation

Planned modules:
1. UI Data Mapper: map scraped table fields to `position` and `open_orders` objects with typed dataclasses.
2. Order API Layer (UI-driven or official API if available):
	 - Place limit orders for seed/avg/tp.
	 - Cancel outstanding orders.
	 - Poll fills and update state.
3. Strategy Engine: deterministic state machine enforcing the two-order invariant and MAX_LOTS.
4. Scheduler/Loop: periodic runs with jitter, error backoff, and auto reload of the page on stale UI.
5. Observability: structured logs, minimal metrics counters, optional file-based state snapshot.

## Operational Guidance

For long-running 24x7 operation on Windows, consider
	- Task Scheduler job that activates the venv and runs the bot.

## Common Issues and Fixes

- Python not found: ensure the `py` launcher is installed and on PATH (ships with Python for Windows).
- Browser not found: install Microsoft Edge; run `py -m playwright install --with-deps`.
- Empty tables: page may be lazy-loaded; ensure `wait_until="networkidle"` and an extra small timeout; see saved HTML for the live structure and adjust selectors.
- Windows Python 3.13 + Streamlit subprocess error: ensure Windows event loop policy is set to Proactor (fallback Selector) before importing Playwright, and inside any worker thread that starts Playwright.

## Acceptance Criteria (for PRs)

- venv-first instructions honored; no global installs required.
- No secrets committed; `.gitignore` respected.
- On a fresh environment, following README steps results in a working bot that reaches the BTCUSD page and saves artifacts under `debug/`.

---
Last updated: 2025-08-22


