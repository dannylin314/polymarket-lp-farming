"""Live LP-farm order manager — harvest Polymarket's maker-reward subsidy.

Rests TWO-SIDED limit orders near mid, inside a market's rewardsMaxSpread, on a
whitelist of DURABLE low-competition incentivized markets (see spec / memory
project_lp_reward_farming_2026-08-30). The edge is the subsidy, NOT prediction —
we quote symmetric and take no directional view.

  python farm.py                 - ONE cycle: select -> quote -> (place) -> log
  python farm.py --report        - show pilot_lp.db state (quotes/orders/fills)
  python farm.py --loop           - re-quote every LP_INTERVAL_S seconds

DRY-RUN is the default (LP_AUTOEXEC unset/0): it computes and logs the exact
two-sided quotes it WOULD post, against live books, with no client and no creds.
Validate selection + quoting math here before a VPN is up. Set LP_AUTOEXEC=1 to
go live (needs VPN — UK /order is geoblocked — plus the POLY_* creds below).

Env:
  LP_AUTOEXEC=0|1        default 0 (dry-run)
  LP_MARKETS=substr;...  whitelist question substrings (default: durable-3)
  LP_K_CENTS=1.0         quote offset from mid, capped at maxSpread-0.5c
  LP_MIN_POOL=40         skip farms with pool < this
  LP_MAX_COMP=400        skip farms with qualifying competition > this
  LP_MIN_HOURS=24        skip farms resolving within this many hours (adverse-fill guard)
  LP_MAX_INV_USD=75      per-market net-inventory cap; pull the increasing side past it
  LP_DAILY_STOP_USD=21   halt+cancel if net (rewards - fill PnL) <= -this
  LP_DEFAULT_SIZE=20     per-side size when rewardsMinSize is 0
  LP_INTERVAL_S=600      --loop re-quote cadence
  POLY_PRIVATE_KEY / POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE /
  POLY_FUNDER_ADDRESS / POLY_SIGNATURE_TYPE(=2)   (LIVE only)
"""
import os, sys, time, json, math, sqlite3, argparse, datetime
from pathlib import Path

import scan   # reuse g(), fetch_incentivized(), reward_rate(), book(), _ascii()

DB = Path(__file__).resolve().parent / "pilot_lp.db"

# Durable low-competition farms validated 9/1 (31 snaps). Ornn is the safest
# first target (stable index, low comp, no informed-flow problem); the weather
# lows are sole-maker -> you eat all informed flow alone, so add them only after
# Ornn proves net-positive. See spec §3.
DURABLE_DEFAULT = "Ornn B200 Index;lowest temperature in Hong Kong;lowest temperature in Shanghai"
# toxicity guard (9/14): never quote these even if whitelisted — informed-flow markets that
# carried ~all the adverse fill P&L in the sim ("highest score" AI-benchmark/esports score races;
# temp/crypto = volatile near-resolution). Mirrors sim.py EXCLUDE.
TOXIC = (
    # crypto / index / price levels — volatile, near-resolution, informed
    "highest score", "bitcoin", "ethereum", " sol ", "solana", "up or down",
    "higher or lower", " xrp ", "dogecoin", "index", " btc", " eth",
    # weather — short-dated, informed obs flow (NYC precip legs bled −$14 live 9/28)
    "temperature", "precipitation", "inches of", "rainfall", " rain", "snow",
    "degrees", "weather", "high temp", "low temp",
    # esports / gaming tournaments — informed live-event flow (BNK FEARX Demacia Cup bled)
    "esports", "hitpoint", "demacia", "league of legends", " lol ", "valorant",
    " dota", "counter-strike", "counter strike", " cs2", "csgo", "overwatch",
    "rocket league", "call of duty", "riot games", " worlds",
    # sports — event-driven, informed (Sal Stewart ROY, Eddie Howe coach, Nate Sheppard D1 rush bled)
    "head coach", "next coach", "rookie of the year", "spread:", "o/u", "over/under",
    "moneyline", "relegat", "vs.", " vs ", " beat ", "premier league", "champions league",
    "super bowl", "world cup", " nba", " nfl", " mlb", " nhl", " ufc", "grand prix",
    " d1 ", "ncaa", "heisman", "college football", "rushing", "touchdown", "quarterback",
    "passing yards", "receiving yards", "mvp", "cy young", "all-star", "playoff",
    # entertainment / awards / reality / film-casting — catalyst-driven, thin, drifty
    # (Ezra Frech DWTS bled; "live-action Miles Morales actor" naked-drifted −$14 on 9/30)
    "dancing with the stars", "dwts", "oscar", "grammy", "emmy", "eurovision",
    "bachelor", "survivor", "reality", " award", "box office",
    "live-action", "live action", " movie", " film", " actor", "recast", "casting", "spider-man",
    # tech / AI catalysts — release/announce/IPO = event-driven, informed (Anthropic/OpenAI/Gemini bled)
    "ipo", "release", "released by", "announce", "debut", "launch", " model ",
    "gpt", "claude", "gemini", "grok", "llama", "openai", "anthropic",
)


def is_toxic(title):
    """True if a market title hits the informed-flow TOXIC blocklist. Single source of truth for
    the selection guard AND the naked-leg manager: a naked leg on a toxic market auto-FLATTENS
    (sell out) rather than auto-hedges (complete), because completing spends scarce capital to
    rescue a losing informed-flow bet (e.g. the Miles Morales entertainment leg that went −$14)."""
    ql = (title or "").lower()
    return any(t in ql for t in TOXIC)


def env_f(k, d):
    try: return float(os.environ.get(k, str(d)))
    except Exception: return d

def env_i(k, d):
    try: return int(os.environ.get(k, str(d)))
    except Exception: return d


class Cfg:
    def __init__(self):
        self.live       = os.environ.get("LP_AUTOEXEC", "0").strip() in ("1", "true", "yes", "on")
        self.markets    = [s.strip().lower() for s in os.environ.get("LP_MARKETS", DURABLE_DEFAULT).split(";") if s.strip()]
        self.k_cents    = env_f("LP_K_CENTS", 1.0)
        self.min_pool   = env_f("LP_MIN_POOL", 40)
        self.max_comp   = env_f("LP_MAX_COMP", 400)
        self.min_hours  = env_f("LP_MIN_HOURS", 24)
        self.min_days   = env_f("LP_MIN_DAYS", 20)   # dynamic-stable mode: long-dated floor
        self.max_inv    = env_f("LP_MAX_INV_USD", 75)
        self.max_imbalance = env_f("LP_MAX_IMBALANCE_USD", 2.0)  # HEDGE GUARD: max $ one outcome may lead the other before we stop quoting that side
        self.hedge_max_price = env_f("LP_HEDGE_MAX_PRICE", 0.85)  # don't COMPLETE a hedge when the missing side's mid > this (near-decided market -> completing buys the expensive winner and locks a big loss)
        self.mid_lo     = env_f("LP_MID_LO", 0.15)   # only farm BALANCED markets: skip mid outside [lo,hi]
        self.mid_hi     = env_f("LP_MID_HI", 0.85)   # (near-decided extremes fill one-sided as they resolve)
        self.min_liq    = env_f("LP_MIN_LIQ", 500)   # liquidity floor: thin books fill one-sided + cost to exit
        self.daily_stop = env_f("LP_DAILY_STOP_USD", 21)
        self.def_size   = env_f("LP_DEFAULT_SIZE", 20)
        self.interval   = env_i("LP_INTERVAL_S", 600)


# ── DB ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS quotes (ts INTEGER, market TEXT, token TEXT,
                   side TEXT, price REAL, size REAL, mid REAL, maxspr REAL, in_band INTEGER,
                   pool REAL, comp REAL, mode TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS orders (ts INTEGER, market TEXT, token TEXT,
                   side TEXT, price REAL, size REAL, poly_oid TEXT, status TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fills (ts INTEGER, market TEXT, token TEXT,
                   side TEXT, price REAL, size REAL, mid_at_fill REAL, poly_oid TEXT)""")
    con.commit(); con.close()


# ── selection ───────────────────────────────────────────────────────────
def hours_to_end(m):
    ed = m.get("endDate") or ""
    try:
        end = datetime.datetime.fromisoformat(ed.replace("Z", "+00:00"))
        return (end - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return 1e9   # no end date known -> treat as far away


def select_farms(cfg):
    """Return whitelisted, durable, safe-to-quote incentivized markets with book+params."""
    out = []
    booked = 0
    book_budget = env_i("LP_BOOK_BUDGET", 12)   # cap /book calls/cycle (throttle-safe)
    # FRESH-REWARD TILT + low-liq first: newly-incentivized markets ('new') are less crowded before
    # others pile in (better reward share), so book them first; then low-liquidity (low-comp) markets
    # before the crowded macro pools. (gamma `competitive` field is ~0.9-1.0 on ALL markets = useless
    # as a filter, per the 9/29 analysis; our order-book comp<=max_comp is the real competition gate.)
    cands = sorted(scan.fetch_incentivized(),
                   key=lambda m: (not bool(m.get("new")), float(m.get("liquidity") or 0)))
    for m in cands:
        q = scan._ascii(m.get("question", ""))
        ql = q.lower()
        if is_toxic(q):                        # toxicity guard: skip informed-flow markets
            continue
        if float(m.get("liquidity") or 0) < cfg.min_liq:   # thin books fill one-sided + cost to exit
            continue
        if cfg.markets:                        # whitelist mode: only named markets
            if not any(sub in ql for sub in cfg.markets):
                continue
        else:                                  # DYNAMIC stable mode (matches validated sim):
            if hours_to_end(m) < cfg.min_days * 24:   # long-dated only (low adverse)
                continue
        pool = scan.reward_rate(m)
        if pool < cfg.min_pool:
            continue
        htr = hours_to_end(m)
        if htr < cfg.min_hours:
            continue          # adverse-fill guard: never farm into a near resolution
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        if len(toks) < 2:
            continue          # need YES(token0) + NO(token1) for two-sided bid-YES/bid-NO
        max_spr = float(m.get("rewardsMaxSpread") or 3.0) / 100.0
        req_min = float(m.get("rewardsMinSize") or 0)
        if req_min > cfg.def_size:
            continue          # min-size gate: only farm markets we qualify for AT our size (never size up)
        min_sz  = cfg.def_size
        tick    = float(m.get("orderPriceMinTickSize") or 0.01)
        if booked >= book_budget:
            break                              # book budget spent this cycle (throttle guard)
        bids, asks = scan.book(toks[0])
        booked += 1
        time.sleep(1.0)                        # gentle spacing; book() retries once on fail
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        if not (cfg.mid_lo <= mid <= cfg.mid_hi):
            continue          # only BALANCED markets — near-decided extremes fill one-sided as they resolve
        qb = sum(sz for p, sz in bids if p >= mid - max_spr)
        qa = sum(sz for p, sz in asks if p <= mid + max_spr)
        comp = qb + qa
        if comp > cfg.max_comp:
            continue
        out.append(dict(q=q, token0=toks[0], token1=toks[1], mid=mid, max_spr=max_spr,
                        min_sz=min_sz, tick=tick, pool=pool, comp=comp,
                        neg_risk=bool(m.get("negRisk")), hours=htr))
    return out


def desired_quotes(f, cfg):
    """Two-sided reward quote WITHOUT inventory: BID YES (buy token0) below YES-mid AND
    BID NO (buy token1) below NO-mid. Two buy legs on opposite outcomes are genuinely
    two-sided in the unified book (a NO bid = a YES ask), and need no held inventory.
    (A buy+sell on the same token would need YES inventory to back the sell — the cold-start
    failure the first live cycle hit.)"""
    k = min(f["max_spr"] - 0.005, cfg.k_cents / 100.0)   # stay 0.5c inside the reward edge
    k = max(k, f["tick"])                                 # at least one tick off mid
    tick = f["tick"]
    def snap_price(p):
        p = round(round(p / tick) * tick, 4)
        return min(max(p, tick), 1 - tick)
    sz = round(f["min_sz"], 2)
    yes_mid = f["mid"]
    no_mid = 1.0 - yes_mid
    legs = [
        ("YES", f["token0"], yes_mid, snap_price(yes_mid - k)),
        ("NO",  f["token1"], no_mid,  snap_price(no_mid - k)),
    ]
    quotes = []
    for outcome, token, omid, price in legs:
        in_band = (omid - price) <= f["max_spr"] + 1e-9 and price < omid
        quotes.append(dict(side="BUY", outcome=outcome, token=token,
                           price=price, size=sz, in_band=in_band))
    return quotes


# ── inventory / stop ────────────────────────────────────────────────────
def token_inventory_usd(token):
    """USD cost basis currently held in one outcome token (all fills are buys)."""
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT size, price FROM fills WHERE token=?", (token,)).fetchall()
    con.close()
    return sum(s * p for s, p in rows)


# ── live client ─────────────────────────────────────────────────────────
def build_client():
    # Official unified SDK (polymarket-client / github Polymarket/py-sdk). SecureClient auto-
    # resolves wallet_type and signs BOTH buys and sells correctly — the low-level py_clob_client_v2
    # had an unfixed sell-signing bug (invalid signature) for Gnosis-Safe accounts. Verified live 9/14.
    from polymarket import SecureClient
    # Gasless merges/redeems need a Builder (or Relayer) API key; without it
    # merge_positions() fails "Gasless transactions require a Builder API Key".
    # Creds live in .env (POLY_BUILDER_*); pass them so auto-merge can execute.
    api_key = None
    bk, bs, bp = (os.environ.get("POLY_BUILDER_API_KEY"),
                  os.environ.get("POLY_BUILDER_SECRET"),
                  os.environ.get("POLY_BUILDER_PASSPHRASE"))
    if bk and bs and bp:
        try:
            from polymarket.auth import BuilderApiKey
            api_key = BuilderApiKey(key=bk, secret=bs, passphrase=bp)
        except Exception as e:
            print(f"  [warn] could not build BuilderApiKey ({e}); merges will fail")
    return SecureClient.create(
        private_key=os.environ.get("POLY_PRIVATE_KEY", ""),
        wallet=os.environ.get("POLY_FUNDER_ADDRESS", ""),
        api_key=api_key,
    )


def our_open_order_ids():
    con = sqlite3.connect(DB)
    ids = [r[0] for r in con.execute(
        "SELECT poly_oid FROM orders WHERE status='placed' AND poly_oid IS NOT NULL").fetchall()]
    con.close()
    return ids


def tick_str(t):
    """py_clob_client keys ROUNDING_CONFIG by string tick literals, not floats."""
    for s in ("0.1", "0.01", "0.001", "0.0001"):
        if abs(float(t) - float(s)) < 1e-9:
            return s
    return "0.01"


def place_live(client, f, q, cfg):
    # SecureClient.place_limit_order handles wallet-type signing, tick/neg_risk internally.
    # post_only=True → maker-only: reject if it would cross/take (LP quotes never take).
    try:
        r = client.place_limit_order(
            token_id=q["token"], price=q["price"], size=q["size"],
            side=("BUY" if q["side"] == "BUY" else "SELL"), post_only=True)
    except Exception as e:
        return None, str(e)[:160]
    if getattr(r, "ok", False):
        return getattr(r, "order_id", None), None
    reason = getattr(r, "reason", None) or getattr(r, "error", None) or getattr(r, "message", None) or r
    return None, str(reason)[:160]


def merge_complete_sets(cfg, client=None):
    """AUTO-MERGE: for every market where we hold a balanced (complete-set) portion, merge it back to
    USDC (amount='max' = largest balanced amount) -> reclaims $1/pair and frees capital to recycle into
    new resting quotes. Closes the rest -> both-fill -> merge -> re-quote loop. Returns the client
    (built lazily) so the caller can reuse it. Dry-run only prints."""
    from collections import defaultdict
    F = os.environ.get("POLY_FUNDER_ADDRESS", "")
    raw = scan.g(f"https://data-api.polymarket.com/positions?user={F}")
    if not isinstance(raw, list):
        return client
    mk = defaultdict(dict)
    for p in raw:
        try: mk[p["conditionId"]][int(p.get("outcomeIndex", 0))] = p
        except Exception: continue
    for cond, s in mk.items():
        y = s.get(0); n = s.get(1)
        ysz = float(y["size"]) if y else 0.0
        nsz = float(n["size"]) if n else 0.0
        pair = min(ysz, nsz)
        mergeable = (y and y.get("mergeable")) or (n and n.get("mergeable"))
        if pair >= 1 and mergeable:
            title = scan._ascii((y or n).get("title", "?"))[:44]
            print(f"  [merge] {title}: {pair:.0f} complete pairs -> reclaim ~${pair:.2f}")
            if cfg.live:
                if client is None:
                    client = build_client()
                try:
                    h = client.merge_positions(condition_id=cond, amount="max")
                    r = h.wait() if hasattr(h, "wait") else h
                    print(f"     -> merged ({getattr(r, 'status', r)})")
                except Exception as e:
                    print(f"     -> merge failed: {str(e)[:140]}")
    return client


# ── cycle ───────────────────────────────────────────────────────────────
def cycle(cfg):
    init_db()
    farms = select_farms(cfg)
    now = int(time.time())
    mode = "LIVE" if cfg.live else "DRY"

    # daily stop check (rewards not yet integrated in pilot; guard on fill PnL proxy)
    # (kept simple: halt if any market's net inventory loss already exceeds the stop)
    con = sqlite3.connect(DB)
    print(f"=== LP farm cycle [{mode}]  {len(farms)} farm(s) selected ===")
    print("%-40s %5s %6s %6s | %-4s %6s %5s %4s" % (
        "market", "mid", "pool$", "comp$", "side", "price", "size", "band"))
    print("-" * 92)

    client = None
    if cfg.live:
        canc = our_open_order_ids()
        if canc:
            try:
                build_client().cancel_orders(order_ids=canc)   # SecureClient: keyword-only arg
            except Exception as e:
                print(f"  [warn] cancel_orders failed: {e}")
            con.execute("UPDATE orders SET status='canceled' WHERE status='placed'")
            con.commit()
        client = merge_complete_sets(cfg, client)   # AUTO-MERGE completed sets -> reclaim capital
        hedge_positions(cfg)                         # AUTO-HEDGE naked legs -> complete them (maker bid on lagging
                                                     # side, skips near-decided) so they become mergeable next cycle
                                                     # and capital recycles instead of locking up unearning

    for f in farms:
        for q in desired_quotes(f, cfg):
            inv = token_inventory_usd(q["token"])
            adds = q["size"] * q["price"]
            # per-token inventory guard: skip a leg that would push that outcome's held USD past the cap
            over_cap = (inv + adds) > cfg.max_inv
            # HEDGE GUARD: never add to the side already AHEAD of the other outcome. If YES keeps
            # filling and NO doesn't, YES inventory leads -> we stop quoting YES and only quote NO
            # until it catches up. Caps net directional (naked) exposure to ~one order per market
            # instead of the unbounded one-sided accumulation that went naked-long 50 Anthropic-YES.
            other_token = f["token1"] if q["outcome"] == "YES" else f["token0"]
            inv_other = token_inventory_usd(other_token)
            naked = (inv - inv_other) > cfg.max_imbalance
            blocked = over_cap or naked
            reason = " *SKIP-inv" if over_cap else (" *SKIP-naked" if naked else "")
            con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, f["q"], q["token"], q["side"] + ":" + q["outcome"], q["price"], q["size"],
                         f["mid"], f["max_spr"] * 100, int(q["in_band"]), f["pool"], f["comp"], mode))
            print("%-38s %5.3f %6.0f %6.0f | bid %-3s %6.3f %5.0f %4s%s" % (
                f["q"][:38], f["mid"], f["pool"], f["comp"], q["outcome"], q["price"],
                q["size"], "Y" if q["in_band"] else "N", reason))
            if blocked or not q["in_band"]:
                continue
            if cfg.live:
                if client is None:
                    client = build_client()
                oid, err = place_live(client, f, q, cfg)
                status = "placed" if oid else "failed"
                con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                            (now, f["q"], q["token"], q["side"] + ":" + q["outcome"], q["price"], q["size"], oid, status))
                if err:
                    print(f"      [place failed] {err}")
    con.commit(); con.close()

    if not farms:
        print("  (no farms passed selection — check whitelist / min-hours / pool)")
    print(f"\n  mode={mode}"
          + ("" if cfg.live else "  — DRY-RUN: quotes logged, nothing posted. Set LP_AUTOEXEC=1 (+VPN+creds) to go live."))


def report():
    init_db()
    con = sqlite3.connect(DB)
    print("=== pilot_lp.db ===")
    for tbl in ("quotes", "orders", "fills"):
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {n} rows")
    print("\nlatest quotes:")
    print("%-40s %-4s %6s %5s %4s %-4s" % ("market", "side", "price", "size", "band", "mode"))
    last = con.execute("SELECT MAX(ts) FROM quotes").fetchone()[0]
    if last:
        for m, sd, pr, sz, ib, md in con.execute(
            "SELECT market,side,price,size,in_band,mode FROM quotes WHERE ts=? ORDER BY market,side", (last,)):
            print("%-40s %-4s %6.3f %5.0f %4s %-4s" % (m[:40], sd, pr, sz, "Y" if ib else "N", md))
    print("\nopen orders (live):")
    for m, sd, pr, sz, oid in con.execute(
        "SELECT market,side,price,size,poly_oid FROM orders WHERE status='placed'"):
        print("  %-38s %-4s %.3f x%.0f  oid=%s" % (m[:38], sd, pr, sz, oid))
    print("\nfills:")
    for m, sd, pr, sz, mf in con.execute(
        "SELECT market,side,price,size,mid_at_fill FROM fills ORDER BY ts DESC LIMIT 20"):
        print("  %-38s %-4s %.3f x%.0f  mid@fill=%.3f  adverse=%+.3f" % (
            m[:38], sd, pr, sz, mf or 0, (mf - pr) if (mf and sd == "BUY") else (pr - mf if mf else 0)))
    con.close()


def hedge_positions(cfg):
    """ACTIVE MANAGEMENT: complete the hedge on every NAKED position by resting a maker BID on the
    under-held outcome (post_only + priced in the reward band -> completes the hedge AND earns reward
    while it fills; we're a buyer, so no selling into thin books). Flags MERGEABLE complete-sets
    (merge on the UI to reclaim capital now = don't hold non-earners) and REDEEMABLE resolved ones.
    Dry-run unless LP_AUTOEXEC=1. Maker bids may not fill instantly in a fast-moving market — re-run
    (or the loop) chases them; for a guaranteed immediate lock you'd cross the book instead."""
    from collections import defaultdict
    F = os.environ.get("POLY_FUNDER_ADDRESS", "")
    raw = scan.g(f"https://data-api.polymarket.com/positions?user={F}")
    if not isinstance(raw, list):
        print("  positions fetch failed"); return
    mk = defaultdict(dict)
    for p in raw:
        try: mk[p["conditionId"]][int(p.get("outcomeIndex", 0))] = p
        except Exception: continue
    print(f"=== HEDGE MANAGER [{'LIVE' if cfg.live else 'DRY'}]  {len(mk)} market(s) held ===")
    client = None
    for cond, sides in mk.items():
        y = sides.get(0); n = sides.get(1)
        ysz = float(y["size"]) if y else 0.0
        nsz = float(n["size"]) if n else 0.0
        row = y or n
        title = scan._ascii(row.get("title", "?"))[:44]
        if row.get("redeemable"):
            print(f"  {title:44s} REDEEMABLE (resolved) -> redeem on UI"); continue
        naked = round(ysz - nsz, 2)
        if abs(naked) < 1.0:                                    # already ~hedged
            if (y and y.get("mergeable")) or (n and n.get("mergeable")):
                print(f"  {title:44s} HEDGED+MERGEABLE -> merge {min(ysz,nsz):.0f} pairs on UI (reclaim capital)")
            else:
                print(f"  {title:44s} hedged Y{ysz:.0f}/N{nsz:.0f} -> hold, redeems clean")
            continue
        if is_toxic(title):                                     # TOXIC naked leg -> FLATTEN (sell out),
            # don't spend scarce capital completing a losing informed-flow bet. Sell marketable at the
            # best bid; skip near-worthless legs (let those resolve — selling salvages ~nothing).
            if naked > 0:
                sell_token = y["asset"]; sell_out = "YES"; qty = round(naked, 2)
            else:
                sell_token = n["asset"]; sell_out = "NO"; qty = round(-naked, 2)
            bids, _ = scan.book(sell_token)
            if not bids:
                print(f"  {title:44s} TOXIC NAKED (Y{ysz:.0f}/N{nsz:.0f}) -> no bids to sell, SKIP"); continue
            best_bid = bids[0][0]
            floor = float(os.environ.get("LP_FLATTEN_MIN_BID", "0.03") or 0.03)
            if best_bid < floor:
                print(f"  {title:44s} TOXIC NAKED -> {sell_out} bid {best_bid:.3f} < {floor:.2f}: near-worthless, let resolve (SKIP)")
                continue
            tick = 0.01
            price = min(max(round(round(best_bid / tick) * tick, 4), tick), 1 - tick)
            print(f"  {title:44s} TOXIC NAKED (Y{ysz:.0f}/N{nsz:.0f}) -> FLATTEN: SELL {sell_out} {qty:.0f} "
                  f"@ {price:.3f} (bid {best_bid:.3f}) frees ~${qty*price:.2f}")
            if cfg.live:
                if client is None: client = build_client()
                oid, err = place_sell(client, sell_token, price, qty)
                print("       ->", ("sold " + str(oid)) if oid else ("FAILED " + str(err)[:120]))
            continue
        if naked > 0:                                           # long YES -> buy NO to complete
            buy_token = y.get("oppositeAsset") if y else n["asset"]; buy_out = "NO"; need = round(naked, 2)
        else:                                                   # long NO -> buy YES to complete
            buy_token = n.get("oppositeAsset") if n else y["asset"]; buy_out = "YES"; need = round(-naked, 2)
        bids, asks = scan.book(buy_token)
        if not bids or not asks:
            print(f"  {title:44s} NAKED (Y{ysz:.0f}/N{nsz:.0f}) -> no book for {buy_out} token, SKIP"); continue
        mid = (bids[0][0] + asks[0][0]) / 2.0
        if mid > cfg.hedge_max_price:                          # missing side near-certain -> completing buys the expensive winner
            print(f"  {title:44s} NAKED (Y{ysz:.0f}/N{nsz:.0f}) -> {buy_out} mid {mid:.3f} > cap {cfg.hedge_max_price:.2f}: "
                  f"NEAR-DECIDED, do NOT complete (would lock a big loss). Let it resolve or sell the dying leg for residual.")
            continue
        tick = 0.01
        k = max(tick, cfg.k_cents / 100.0)
        price = min(max(round(round((mid - k) / tick) * tick, 4), tick), 1 - tick)
        print(f"  {title:44s} NAKED (Y{ysz:.0f}/N{nsz:.0f}) -> BID {buy_out} {need:.0f} @ {price:.3f} (mid {mid:.3f}) to complete hedge")
        if cfg.live:
            if client is None: client = build_client()
            oid, err = place_live(client, {"q": title},
                                  dict(token=buy_token, price=price, size=need, side="BUY", outcome=buy_out), cfg)
            print("       ->", ("placed " + str(oid)) if oid else ("FAILED " + str(err)[:120]))
    print("  (dry-run: nothing placed. Set LP_AUTOEXEC=1 to rest the hedge bids.)" if not cfg.live else "")


def place_sell(client, token, price, size):
    """Marketable SELL (post_only=False) to EXIT a naked leg — crosses to the bid so it fills.
    (place_live is post_only=True for maker quotes; a flatten must be allowed to take.)"""
    try:
        r = client.place_limit_order(token_id=token, price=price, size=size, side="SELL", post_only=False)
    except Exception as e:
        return None, str(e)[:160]
    if getattr(r, "ok", False):
        return getattr(r, "order_id", None), None
    return None, str(getattr(r, "error", "rejected"))[:160]


def flatten_positions(cfg):
    """CAPITAL-LIGHT NEUTRALIZATION: SELL each NAKED leg to exit — frees capital AND cuts directional
    risk (vs completing the hedge, which SPENDS capital you may not have). Sells marketable (limit at
    the best bid) so it fills. Skips near-worthless legs (best bid < LP_FLATTEN_MIN_BID) — let those
    resolve, selling salvages ~nothing. Leaves hedged cores alone (merge those on the UI). Dry-run
    unless LP_AUTOEXEC=1."""
    from collections import defaultdict
    F = os.environ.get("POLY_FUNDER_ADDRESS", "")
    raw = scan.g(f"https://data-api.polymarket.com/positions?user={F}")
    if not isinstance(raw, list):
        print("  positions fetch failed"); return
    floor = float(os.environ.get("LP_FLATTEN_MIN_BID", "0.03") or 0.03)
    mk = defaultdict(dict)
    for p in raw:
        try: mk[p["conditionId"]][int(p.get("outcomeIndex", 0))] = p
        except Exception: continue
    print(f"=== FLATTEN naked legs (SELL) [{'LIVE' if cfg.live else 'DRY'}] ===")
    client = None; freed = 0.0
    for cond, sides in mk.items():
        y = sides.get(0); n = sides.get(1)
        ysz = float(y["size"]) if y else 0.0
        nsz = float(n["size"]) if n else 0.0
        naked = round(ysz - nsz, 2)
        row = y or n
        title = scan._ascii(row.get("title", "?"))[:44]
        if abs(naked) < 1.0:
            continue                                            # hedged core -> merge on UI, don't sell
        if naked > 0:
            sell_token = y["asset"]; sell_out = "YES"; qty = round(naked, 2)
        else:
            sell_token = n["asset"]; sell_out = "NO"; qty = round(-naked, 2)
        bids, _ = scan.book(sell_token)
        if not bids:
            print(f"  {title:44s} SELL {sell_out} {qty:.0f} -> no bids, SKIP"); continue
        best_bid = bids[0][0]
        if best_bid < floor:
            print(f"  {title:44s} {sell_out} bid {best_bid:.3f} < {floor:.2f} -> near-worthless, let resolve (SKIP)"); continue
        tick = 0.01
        price = min(max(round(round(best_bid / tick) * tick, 4), tick), 1 - tick)
        freed += qty * price
        print(f"  {title:44s} SELL {sell_out} {qty:.0f} @ {price:.3f} (bid {best_bid:.3f}) -> frees ~${qty*price:.2f}")
        if cfg.live:
            if client is None: client = build_client()
            oid, err = place_sell(client, sell_token, price, qty)
            print("       ->", ("sold " + str(oid)) if oid else ("FAILED " + str(err)[:120]))
    print(f"  total capital freed ~${freed:.2f}" + ("  (dry-run: nothing sold. Set LP_AUTOEXEC=1 to sell.)" if not cfg.live else ""))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--hedge", action="store_true", help="active mgmt: complete hedges on naked positions + flag merge/redeem")
    ap.add_argument("--flatten", action="store_true", help="active mgmt: SELL naked legs to exit (capital-light) + skip near-worthless")
    ap.add_argument("--merge", action="store_true", help="merge all complete-set positions back to USDC (reclaim capital)")
    ap.add_argument("--loop", action="store_true", help="re-quote every LP_INTERVAL_S seconds")
    args = ap.parse_args()
    cfg = Cfg()
    if args.report:
        report()
    elif args.hedge:
        hedge_positions(cfg)
    elif args.flatten:
        flatten_positions(cfg)
    elif args.loop:
        while True:
            cycle(cfg)
            time.sleep(cfg.interval)
    else:
        cycle(cfg)
