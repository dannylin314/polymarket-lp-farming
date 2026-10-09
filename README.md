# Polymarket Liquidity-Rewards Farming

A research-and-execution system for Polymarket's **liquidity-rewards** program: rest
two-sided limit orders near the midpoint of incentivised markets, earn the daily reward
pool pro-rata, and actively manage the resulting inventory so capital keeps working.

The interesting part isn't the idea — it's what it takes to find out whether the idea
actually makes money. This repo is built around an **honest, leak-free simulator** and a
**live order manager with active risk management**, and the headline result is a negative
one, arrived at rigorously:

> Liquidity rewards are real and measurable, but on a retail-sized account they are
> closely matched — and usually beaten — by **adverse selection**: the cost of getting
> one-sided fills on markets that are moving. Naive backtests that mark fills to mid show
> a large profit; once fills are marked honestly, the edge collapses to roughly breakeven
> or negative. The engineering below is what makes that conclusion trustworthy.

> ⚠️ Educational / research project. It places real orders with real funds when run live.
> Not financial advice. Use at your own risk.

---

## How the reward program works

- Rest limit orders within a market's `rewardsMaxSpread` of the midpoint.
- Every minute a random book snapshot scores your orders by **size × closeness-to-mid**
  (a quadratic weight, `((v − k) / v)²`).
- Paid daily at 00:00 UTC, pro-rata: `pool × your_score / total_score`.
- A day only pays if your earnings reach **$1** — below that it pays nothing and does not
  roll over. Markets with mid < 0.10 must be quoted two-sided.

Rewards accrue **only while orders rest unfilled in-band**. A filled order becomes
inventory that earns nothing — so the whole problem is: *stay in-band, and don't get run
over when informed flow arrives.*

---

## Architecture

```
          ┌─────────────┐   incentivised markets, pools, spreads, order books
          │   scan.py    │   (Polymarket Gamma + CLOB, stdlib urllib only)
          └──────┬───────┘
                 │ market universe
      ┌──────────┼───────────────────────────────┐
      ▼          ▼                                ▼
┌──────────┐ ┌──────────┐                   ┌──────────┐
│analyze.py│ │  sim.py   │  honest P&L       │ farm.py  │  live order manager
│ market    │ │ leak-free │  = rewards        │ quote →  │  (two-sided, hedge-guarded)
│ profiling │ │ simulator │  − adverse fills  │ fill →   │  + active management:
└──────────┘ └──────────┘                    │ merge /  │    auto-merge complete sets
                                              │ hedge /  │    auto-hedge naked legs
             ┌──────────┐                     │ flatten  │    auto-flatten toxic legs
             │flowtox.py│  flow-toxicity      └────┬─────┘
             │ filter    │  diagnostics             │
             └──────────┘                           ▼
                                      ┌───────────────────────────┐
                                      │ docker-compose + gluetun   │
                                      │ split-tunnel VPN, always-on │
                                      └───────────────────────────┘
                                                   │
                                            kill.py  (cancel-all kill switch)
```

## Components

| File | Role |
|------|------|
| `lp/scan.py` | Pulls all incentivised markets and their order books; ranks by estimated reward-yield on capital; competition measured as resting **size** near mid, not notional. Pure standard library. |
| `lp/analyze.py` | Characterises the whole incentivised universe by type / pool / spread / min-size / mid / liquidity / horizon to choose farm filters **empirically** rather than by hunch. |
| `lp/sim.py` | The core of the project: a **leak-free simulator**. Rewards are closeness- and uptime-weighted; fills come from real trade prints and are marked to a **forward markout** (median price 2h ahead) minus exit slippage — so informed flow shows up as a loss instead of being hidden by mark-to-mid. Models the $1/day floor, min-size gating, and a capped accrual window. |
| `lp/farm.py` | The live order manager. Selects non-toxic, long-dated, fat-pool markets; quotes two-sided near mid; and **actively manages inventory** each cycle: cancel → auto-merge complete sets → hedge / flatten naked legs → re-quote. A hedge-guard caps one-sided (naked) exposure; a toxicity blocklist keeps it out of informed-flow markets. |
| `lp/flowtox.py` | Flow-toxicity diagnostics (recent trade range / drift / imbalance) used to test whether adverse selection is predictable from public flow. |
| `lp/dormant.py`, `lp/extreme.py` | Research probes for two sub-strategies — uncontested "dormant" pools, and the capital-light extreme-market (95/5) approach. |
| `lp/kill.py` | Kill switch: cancel every resting order on the account (or only the ones the farm placed). |
| `Dockerfile.farm`, `docker-compose.yml` | Always-on deployment. The farm container shares a [gluetun](https://github.com/qdm12/gluetun) VPN container's network namespace, so **only the bot** egresses through the VPN while the host stays on its normal connection (split tunnelling). |

## Engineering highlights

- **Leak-free backtesting.** The first naive simulator showed a large paper profit. Each
  fidelity fix shrank it: marking fills to a forward markout instead of mid, charging exit
  slippage, enforcing the $1/day reward floor, gating on each market's minimum order size,
  and capping a reward-accrual gap bug that was crediting absence as farming. The honest
  net landed near zero. *Finding the leaks was the work.*
- **Active position management.** Rewards stop the instant an order fills, so idle
  inventory is pure drag. The engine recycles capital: merges complete sets back to
  collateral, completes hedges on non-toxic naked legs, and **flattens** naked legs on
  toxic markets (selling out rather than spending scarce capital to rescue a losing
  directional bet).
- **Adverse-selection defence.** A hedge-guard stops one-sided accumulation; a keyword
  toxicity filter excludes informed-flow markets (sports, esports, weather, AI-release
  dates, price-level crypto); market selection favours long-dated, stable-probability
  pools where the mid barely moves.
- **Production deployment.** Dockerised, restart-on-failure, split-tunnel VPN via gluetun,
  gasless complete-set merges via a Builder API key, and a tested cancel-all kill switch.

## What the research concluded

- Rewards are real and scale with deployed capital, but so does adverse selection, and the
  two roughly cancel on a retail account.
- The $1/day floor punishes a thin or single-market book: below $1 for the day you earn
  **nothing**, so breadth matters as much as closeness-to-mid.
- A naive backtest and an honest one can differ by an order of magnitude — the difference
  is entirely in how fills are marked.
- The transferable value here is the methodology (honest simulation, active inventory
  management, toxicity filtering, production plumbing), not a magic-money machine.

---

## Setup

```bash
git clone https://github.com/dannylin314/polymarket-lp-farming.git
cd polymarket-lp-farming
cp .env.example .env         # fill in your own credentials
pip install -r requirements.txt
```

### Analyse the market universe (read-only, no credentials needed)
```bash
python lp/analyze.py
python lp/scan.py
```

### Run the honest simulator (no credentials, no orders)
```bash
python lp/sim.py poll        # accrue a window
python lp/sim.py report      # rewards vs. honestly-marked adverse fills
```

### Run live (places real orders — requires credentials)
```bash
# bare-metal:
./run_pilot.ps1              # or: LP_AUTOEXEC=1 python lp/farm.py --loop
./kill_pilot.ps1             # cancel-all kill switch

# or always-on via Docker with a split-tunnel VPN:
docker compose up -d gluetun
docker compose up -d farm
docker compose logs -f farm
docker compose run --rm farm python kill.py   # kill switch
```

## Tech stack

Python 3.11 · Polymarket CLOB (`polymarket-client` SDK) · SQLite for local state ·
Docker + gluetun (split-tunnel VPN) · standard-library HTTP/JSON throughout the research
tooling (no heavy dependencies).

## Repository layout

```
.
├── lp/
│   ├── scan.py        market + order-book scanner
│   ├── analyze.py     universe characterisation
│   ├── sim.py         leak-free simulator (rewards − adverse fills)
│   ├── farm.py        live order manager + active position management
│   ├── flowtox.py     flow-toxicity diagnostics
│   ├── dormant.py     uncontested-pool probe
│   ├── extreme.py     extreme-market (95/5) probe
│   └── kill.py        cancel-all kill switch
├── Dockerfile.farm
├── docker-compose.yml  always-on farm + gluetun split-tunnel VPN
├── run_pilot.ps1 / kill_pilot.ps1
├── requirements.txt
└── .env.example
```
