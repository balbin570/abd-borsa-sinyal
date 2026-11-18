# Windows home server (IBKR PAPER, read-only)

This starts a **read-only** 5-minute health monitor. It does **not** place IBKR orders, execute the Top5 strategy, log in to IB Gateway, or guarantee unattended 24/7 connectivity.

1. Keep IB Gateway in **Simulated Trading**, with **Read-Only API ON**, local API port **4002**. Never expose port 4002 to the internet.
2. On Windows, install Python 3.13, `ibapi`, `tzdata`, `exchange-calendars`; use the repository directory.
3. Double-click `start_ibkr_home_server.bat`. The health output is saved to `ibkr_bridge/local_runtime/health.json`; logs to `server.log`.
4. To start on Windows login, use **Win+R → shell:startup** and place a shortcut to the BAT there. This starts only after a user signs in; for unattended startup use Windows Task Scheduler with an appropriate user login/session configuration.
5. In Windows Power Options, disable sleep while plugged in; keep monitor sleep separate. Configure Windows updates/restarts deliberately. IB Gateway may require periodic manual login or 2FA.
6. Stop by closing the command window (or Ctrl+C). No real-money order capability is included.

IBKR's $1m paper cash is not the strategy allocation; the independent IBKR strategy risk cap remains $10k. The legacy Top5 ledger remains separate. Automated paper execution is **not enabled**.
