# rhbot

A self-hosted, rules-based trading service for **Alpaca**, **Charles Schwab**
and **Robinhood**. It runs unattended, evaluates the rules **you** define, and
places orders through a pluggable broker adapter. Paper trading is the default;
live trading is opt-in.

**This is software, not financial advice.** The included strategies are
templates that demonstrate the framework. Automated trading can lose money fast.
You are responsible for every order this places.

---

## Quick start (paper mode, no accounts needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python run.py
```

Then open the dashboard at **http://127.0.0.1:5001**.

With no credentials it uses **free Yahoo market data (real prices)** and
simulated fills — so paper results are meaningful out of the box.

**Panic button** — halts all trading immediately, even mid-run:

```bash
touch state/STOP
```

---

## What it does each cycle

1. Fetch the latest price + recent bars for every symbol in `config.yaml`
2. Ask that symbol's strategy for a signal (`ENTER_LONG` / `EXIT_LONG` / `HOLD`)
3. Turn an actionable signal into an order
4. Run the order through the **frequency governor** and the **risk manager**
5. If it passes, submit to the broker and record the fill

The strategy only decides *direction*. How often you may trade and how much is
enforced outside it — that separation is what makes an unattended bot safe.

---

## The PDT rule — read this before trading stocks

A **day trade** is buying *and* selling the same security on the *same day*.
If your account is under **$25,000**, US regulations limit you to **3 day trades
per 5 business days**. Exceed it and your account gets flagged and restricted.

This has direct consequences for a "buy every dip, sell every peak" bot:

| | Day-trade limit | Notes |
|---|---|---|
| **Stocks under $25k** | 3 per 5 business days | High-frequency intraday flipping is **not viable** |
| **Stocks, held overnight** | Not a day trade | Swing/daily-bar strategies sidestep the cap |
| **Crypto** | No limit | Exempt from PDT; trades 24/7 |

Three ways to stay compliant, all supported here:
- **Trade crypto** for high frequency, or
- **Use daily bars / overnight holds** for stocks (set `min_seconds_between_trades`
  high, e.g. `86400`), or
- **Run against an Alpaca paper account**, where the cap does not exist at all.

The backtester reports a **`DayTr/wk`** column — keep it **under 3** for stocks.

---

## Brokers — and what "unlimited buys and sells" actually requires

The day-trade cap is a *regulatory* limit on the account, not a limitation of
any one broker's API. No US equities broker will let you around it. So
"unlimited" comes from picking a venue and asset class where the rule doesn't
bind:

| `broker:` | Unlimited round trips? | Notes |
|---|---|---|
| `alpaca` + `mode: paper` | **Yes** | Real matching engine, fake money. No PDT, no settlement. |
| `alpaca` + `mode: live`, crypto | **Yes** | Crypto is PDT-exempt and trades 24/7. |
| `alpaca` + `mode: live`, stocks | Only above $25k | Guard auto-disables when equity clears the threshold. |
| `schwab` (live only) | Only above $25k | Real money, stocks only, whole shares only. |
| `robinhood` crypto | Yes | Official Ed25519 API. Wider spreads than Alpaca. |
| `robinhood` stocks | No | PDT applies, *and* the library is unofficial. |
| `paper` | Yes | Fills simulated locally — no order engine behind it. |

### The recommended setup: test on Alpaca, trade on Schwab

These are independent choices, and the strategies, risk limits and backtests
are identical across both. Only two lines change:

```yaml
# testing                    # real money
broker: alpaca               broker: schwab
mode: paper                  mode: live
```

Alpaca paper gives you somewhere to hammer the strategy with unlimited trades
against a real matching engine. Schwab has no equivalent, so that rehearsal has
to happen somewhere else — which is exactly what this split is for.

### Why Alpaca is the default when its keys are present

- **Official and supported.** Two static headers, no login flow, no MFA
  scraping. `robin_stocks` can break whenever Robinhood changes their site.
- **One key pair covers stocks and crypto**, so a mixed watchlist works in a
  single process — the Robinhood path can't do that live.
- **Paper and live are the same API.** Going live is a one-line config change,
  not a different code path that has never been exercised.
- **Real fills.** The adapter polls the order until it settles and books the
  actual `filled_avg_price`, so the local PnL matches the broker's.

### Setup

1. Sign up at <https://alpaca.markets> and generate **paper** API keys — free,
   no funding required.
2. Put them in `.env`:
   ```
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   ```
3. That's it. `broker: auto` picks Alpaca as soon as the keys exist.

`mode: paper` sends orders to Alpaca's paper host; `mode: live` sends them to
the live host with the same keys' live counterpart. Free stock data comes from
IEX (real-time, single venue); set `alpaca_data_feed: sip` if you pay for the
consolidated tape. Crypto data is full-book either way.

### Two different meanings of "paper"

This trips people up, so it's worth being explicit:

- `broker: paper` — fills are **simulated locally** at the last price plus fixed
  slippage. No order ever leaves the machine.
- `broker: alpaca` + `mode: paper` — orders go to a **real matching engine**
  holding fake money. You get real fill prices, real partial fills, real
  rejects. This is a far better rehearsal, which is why `auto` prefers it.

## Charles Schwab (real money)

`broker: schwab` uses Schwab's official Trader API. It is the live-money path;
there is **no paper account behind this API**, so the config refuses to start
with `mode: paper` rather than quietly placing real orders.

### Setup

1. Register a **Trader API** app at <https://developer.schwab.com> and wait for
   approval. Set the callback URL to `https://127.0.0.1`.
2. Put the credentials in `.env`:
   ```
   SCHWAB_APP_KEY=...
   SCHWAB_APP_SECRET=...
   SCHWAB_CALLBACK_URL=https://127.0.0.1
   ```
3. Authorize:
   ```bash
   python schwab_login.py
   ```
   You log in at schwab.com — the script never sees your password. Schwab
   redirects your browser to a page that **fails to load**; that is expected.
   Paste the full URL from the address bar back into the script.
4. Set `broker: schwab` and `mode: live` in `config.yaml`.

### The weekly ritual

Schwab refresh tokens expire after **7 days** and cannot be renewed
programmatically. When one dies the bot stops trading and says so. Re-authorize
with `python schwab_login.py`. To check how long you have left:

```bash
python schwab_login.py --status
```

The 30-minute access token *is* refreshed automatically, so within a week the
service runs unattended. Tokens are stored in `state/schwab_token.json` with
mode 0600 — treat that file like a password, since it can trade your account
until it expires. It is gitignored.

### Three behavioural differences from Alpaca

- **Whole shares only.** Schwab has no fractional/notional orders on this API,
  so `order_notional` is floored to a share count. `$1000` of a `$309` stock
  buys 3 shares (`$927`), not `3.23`. An order that rounds to zero shares is
  skipped with a warning — if you see those, raise `order_notional` above the
  share price.
- **No crypto.** The config refuses a crypto symbol on this path. Run crypto in
  a separate process with `broker: alpaca`.
- **No hourly candles.** `bar_interval: 1h` is aggregated from 30-minute
  candles, grouped by wall-clock hour so sessions don't bleed into each other.

Market data is the real-time consolidated feed that comes with the brokerage
account — better than Alpaca's free IEX tier.

### Position drift

Once a real broker is involved, it — not `state/portfolio.json` — is the
authority on what you hold. On startup the bot compares the two and logs
`POSITION DRIFT` if they disagree. Exits are sized from the local book, so a
mismatch means the bot will either try to sell what it doesn't have or strand a
real position. Reconcile before trading: either edit the state file, or flatten
at the broker and delete it to start clean.

---

## Backtesting: pick parameters with data, not guesses

```bash
.venv/bin/python sweep.py --symbol AAPL
```

Sweeps three approaches over ~3 years of real daily bars plus real 1-minute
intraday bars, and reports return, trade count, trades/week, day-trades/week,
win rate, and max drawdown. Use your own data with `--csv mydata.csv`.

For an apples-to-apples comparison against buy & hold, deploy full capital:

```bash
.venv/bin/python sweep.py --symbol AAPL --cash 10000 --notional 10000
```

### What the AAPL backtest actually showed

Over 3 years of AAPL daily bars, **buy & hold returned +66.5%**. The best
slope-reversal configuration returned **+63.2% while making 170 trades**, and
most configurations did substantially worse. Every swing configuration
underperformed too.

**Take that seriously**: on this sample, trading actively did not beat doing
nothing, and it added fees, taxes, and drawdown risk. One symbol over one period
is not proof of anything — which is exactly the point. Test your own idea on
your own symbols and be honest about the result before risking money.

---

## Configuration (`config.yaml`)

| Key | Meaning |
|---|---|
| `mode` | `paper` (simulated) or `live` (**real orders**) |
| `poll_interval_seconds` | How often the engine evaluates |
| `bar_interval` | Bar size strategies see: `1m`/`5m`/`15m`/`1h`/`1d` |
| `max_bar_age_minutes` | Staleness guard; blank = auto per interval |
| `risk.pdt_guard` | Block new stock entries when day-trade budget is spent |
| `risk.max_day_trades_per_5_days` | Day-trade budget (3 under $25k) |
| `watchlist[].strategy` | Strategy name (see below) |
| `watchlist[].params` | Per-symbol strategy parameters |
| `watchlist[].order_notional` | Dollars per entry |
| `risk.min_seconds_between_trades` | **Frequency governor**, per symbol |
| `risk.max_order_notional` | Reject orders larger than this |
| `risk.max_total_exposure` | Cap on total open position value |
| `risk.max_open_positions` | Cap on simultaneous positions |
| `risk.max_daily_loss` | Halt everything if daily PnL drops below this |
| `risk.kill_switch_file` | Halt if this file exists |

Risk checks are conservative by design: **sells are always allowed** (reducing
risk should never be blocked), **buys** must pass every gate.

### `bar_interval` must match what you backtested

This is the easiest way to silently break the bot. `smooth: 3` means *3 bars* —
3 days on `1d`, 3 minutes on `1m`. Tune parameters with `sweep.py` on daily bars
and then run with `bar_interval: 1m` and the strategy behaves nothing like what
you measured. Keep them in sync.

### The three automatic guards

- **Staleness guard** — refuses to trade when the newest bar is older than
  `max_bar_age_minutes`. Without it, a closed market or a dead feed looks like a
  flat price line, which a strategy will happily misread as a signal.
- **Frequency governor** — `min_seconds_between_trades`, enforced per symbol.
- **PDT guard** — counts day trades in a rolling 5-business-day window (US
  market timezone) and **blocks new stock entries** once the budget is spent.
  It deliberately does **not** block exits: being trapped in a losing position
  is worse than a PDT flag, so exits log a warning and proceed. Crypto is exempt
  and never gated. Day-trade counts survive restarts.

### Data note: daily bars are repaired automatically

Yahoo publishes the current session's daily bar with a **null close** until it
settles, so a naive daily fetch silently lags by a full trading session. The
feed detects this and rebuilds today's bar from 1-minute data. Without the
repair the bot was missing the most recent session entirely — a real +3.5% AAPL
move in testing.

---

## Strategies

| Name | Idea | Fits |
|---|---|---|
| `slope_reversal` | Smooth the price, take its derivative; buy when slope flips −→+ (local bottom), sell when +→− (local top). `smooth` denoises, `min_slope_pct` is a deadband. | Crypto, or daily bars |
| `swing_trend` | Enter on an RSI bounce within an uptrend; exit on overbought or trend break. Built for multi-day holds. | Stocks under $25k |
| `sma_crossover` | Classic fast/slow moving-average cross. | Baseline reference |

### Writing your own

Copy `rhbot/strategy/slope_reversal.py`, implement `evaluate()`, and register it
in `rhbot/strategy/__init__.py`. Keep it **pure** — no network calls, no order
placement. Given the same bars it must return the same signal; that determinism
is what makes it backtestable and safe to run unattended.

---

## Going live (only after paper works)

1. `cp .env.example .env` and fill in credentials (`.env` is gitignored).
2. Set `mode: live` in `config.yaml`.
3. Start with tiny `order_notional` and tight risk limits.

On **Alpaca** that is the whole procedure — same keys' live counterpart, same
code path you already exercised on paper. On **Robinhood**:

- **Crypto** uses Robinhood's **official** API (Ed25519-signed). Generate an API
  key + key pair in Robinhood's crypto API dashboard.
- **Stocks** use the **unofficial** `robin_stocks` library, which needs your
  Robinhood username/password (and TOTP secret if you use app MFA). It is not
  sanctioned by Robinhood, can break without notice, and heavy polling may draw
  rate limits. Keep `poll_interval_seconds` reasonable.

### Alternative: Robinhood Agentic Trading (official MCP)

Robinhood now offers an **official** agentic trading product exposing an MCP
server, which is a sanctioned alternative to `robin_stocks` for stocks:

```bash
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
```

Trades are confined to a **dedicated "Agentic" account**, separate from your main
one — a useful blast radius limit. Opening that account requires a desktop
browser and must be done by you during authentication.

Caveats worth knowing:

- The **PDT rule still applies** to that account. Robinhood's docs don't mention
  it, but it is a self-directed individual account: under $25k you still get 3
  day trades per 5 business days.
- Robinhood states you are **"ultimately responsible for the trades your AI agent
  places,"** and warns agents "can make errors, misinterpret instructions, act on
  incomplete or outdated information."
- That is a good argument for keeping *decisions* deterministic (this repo's
  strategies) even if *execution* goes through an agent.
  See: <https://robinhood.com/us/en/support/articles/agentic-trading-overview/>

Live mode refuses to start without the matching credentials. On the Robinhood
path it supports a watchlist of a single asset class per process (run two
processes for both); Alpaca handles stocks and crypto together in one.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

45 tests cover indicators, portfolio accounting, every risk gate, the kill
switch, the PDT day-trade counter, the staleness guard, daily-bar repair,
strategy signals, config validation, and the backtester.

---

## Layout

```
run.py                    entry point
sweep.py                  backtest / parameter sweep
schwab_login.py           Schwab OAuth helper — run weekly
config.yaml               your settings
rhbot/
  engine.py               the trading loop
  risk.py                 safety rails (the last gate before any order)
  portfolio.py            cash, positions, PnL, persistence
  indicators.py           SMA / EMA / RSI / crossings
  backtest.py             historical replay + scoring
  factory.py              wires feeds and brokers from config
  alpaca_client.py        official Alpaca REST client (data + trading)
  schwab_client.py        official Schwab REST client + OAuth token lifecycle
  rh_crypto_client.py     official Robinhood Crypto REST client
  strategy/               strategy interface + implementations
  brokers/                paper (default), alpaca, schwab, RH stock, RH crypto
  data/                   Alpaca, Schwab, Yahoo (free), RH stock, RH crypto
  dashboard/              read-only Flask monitor
```

---

## Limitations

- **Long-only.** No shorting, options, or margin.
- **Schwab needs manual re-authentication every 7 days.** Refresh tokens cannot
  be renewed programmatically — that is Schwab policy, not a gap here.
- **Schwab trades whole shares only**, so small `order_notional` values on
  expensive stocks round down to nothing. Alpaca supports fractional.
- On the **Robinhood** crypto feed, bar history is built from observed ticks at
  runtime, so slope strategies need a warmup period after each restart. Alpaca
  and Yahoo both serve real historical crypto bars.
- On the **Robinhood** adapters, live fills are recorded at the observed price
  rather than polled from the order status endpoint, so PnL is approximate. The
  Alpaca adapter polls for the real fill price and does not have this problem.
- The dashboard is read-only and binds to localhost. Don't expose it publicly.
