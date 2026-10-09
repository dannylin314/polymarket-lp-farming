"""LP-farm PAPER simulator — honest net P&L = rewards - adverse fills.

The scanner (scan.py) reports a reward YIELD but ignores the cost that actually
decides whether farming works: adverse selection. This simulator estimates BOTH
sides from real data, so we get a paper net before spending a cent or a VPN:

  REWARD accrual (the subsidy we'd earn):
    per poll window, reward = pool x [2*our_size/(comp + 2*our_size)] x (dt/24h).
    OPTIMISTIC by design: assumes we stayed in-band the whole window and that the
    competition snapshot held constant. Real uptime <100% and comp varies -> treat
    the reward number as an UPPER estimate.

  FILL P&L (the adverse-selection cost) — from REAL trade prints:
    we poll each farm's actual trades. For each taker trade we model ourselves as
    the resting maker the taker hit, for a size = trade_size x our_side_share
    (our_size / (our_size + comp/2), capped at our_size) at the trade's price P:
      taker BUY  at P  -> we SOLD  at P -> pnl = (P - mid_now) * fill
      taker SELL at P  -> we BOUGHT at P -> pnl = (mid_now - P) * fill
    Marking the past fill to the CURRENT mid is the adverse-selection signal: if
    price trended against us since the fill, pnl is negative. our_side_share ties
    fill volume to competition — on a SOLE-MAKER farm (comp~0) share->1 and we eat
    ALL the toxic flow, exactly the risk that killed passive-MM. PESSIMISTIC where
    queue priority would have spared us. No look-ahead: mark is only ever "now".

  NET = reward + fill_pnl. That is the number that decides the pilot.

  python sim.py poll     - accrue one window (run in the monitoring loop)
  python sim.py report   - net paper P&L to date, per farm
  python sim.py reset    - wipe sim_lp.db
"""
import os, sys, time, json, sqlite3, urllib.request, ssl, datetime, statistics
from pathlib import Path
import scan   # fetch_incentivized, reward_rate, book, _ascii

_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

DURABLE_DEFAULT = "ornn b200 index;lowest temperature in hong kong;lowest temperature in shanghai"
SEED_LOOKBACK_H = 24     # on first sight of a market, ingest this many hours of trades

# BROAD-mode discovery (LP_BROAD=1): instead of the durable-3 name whitelist, track ANY
# currently-live incentivized market that is low-competition and NOT one of the two toxic
# classes we've already measured as adverse-selection traps: same-day temperature (resolves
# on public data intraday) and crypto up/down (SIG/Jump turf). Goal: find whether a BETTER
# safe basket exists. Writes to a SEPARATE db so the durable-3 net stays clean.
BROAD = os.environ.get("LP_BROAD", "0").strip() in ("1", "true", "yes", "on")
BROAD_MAXCOMP = float(os.environ.get("LP_BROAD_MAXCOMP", "150") or 150)
BROAD_MINPOOL = float(os.environ.get("LP_BROAD_MINPOOL", "20") or 20)
EXCLUDE = ("temperature", "bitcoin", "ethereum", " sol ", "solana", "up or down",
           "higher or lower", " xrp ", "dogecoin",
           # toxicity filter (9/14): "highest score achieved by X" AI-benchmark/esports score
           # markets carried ~ALL the adverse fill P&L (-$7.52/53 fills vs +$0.33 on all other
           # farms) — they have informed flow (an observer of the live leaderboard knows the
           # trajectory), unlike stable relegation/political markets. Excluding => net ~= pure reward.
           "highest score")

# MANAGED mode (LP_MANAGED=1): model a DISCIPLINED farmer, not the unmanaged worst
# case — (a) INVENTORY CAP: stop taking fills on a market once |net position| would
# exceed LP_INV_CAP $, (b) EXIT before resolution: don't farm a market within
# LP_EXIT_HOURS of its endDate (that's where adverse selection spiked, e.g. Shanghai
# -$53). This is the apples-to-apples test of the "$450/2d" LP claim: does the reward
# beat adverse once you cap inventory and quit before the close?
MANAGED = os.environ.get("LP_MANAGED", "0").strip() in ("1", "true", "yes", "on")
INV_CAP = float(os.environ.get("LP_INV_CAP", "75") or 75)
EXIT_H = float(os.environ.get("LP_EXIT_HOURS", "12") or 12)

# STABLE mode (LP_STABLE=1): the strategy external sources converge on — farm
# LONG-DATED, stable-probability markets (political / far-off outcomes) where the mid
# barely moves, so adverse selection is minimal. This is the OPPOSITE of the daily
# weather lows we'd been (wrongly) farming. Selects incentivized markets resolving
# >= LP_STABLE_DAYS out, excluding the toxic classes. Run with LP_MANAGED=1.
# EXTREME mode (LP_EXTREME=1): the specific small-account strategy from the 9/4 tweet —
# farm markets where one side sits >0.95 and the other <0.05 (near-DECIDED binaries).
# Price is maximally stable there (outcome ~settled), so adverse selection is minimal
# while a live pool still pays. Selected by GAMMA mid (bestBid/bestAsk) so selection
# needs NO book calls; books only the ~10-15 survivors. Excludes toxic (temp/crypto).
EXTREME = os.environ.get("LP_EXTREME", "0").strip() in ("1", "true", "yes", "on")
STABLE = os.environ.get("LP_STABLE", "0").strip() in ("1", "true", "yes", "on")
STABLE_DAYS = float(os.environ.get("LP_STABLE_DAYS", "20") or 20)
STABLE_MAXCOMP = float(os.environ.get("LP_STABLE_MAXCOMP", "800") or 800)
# curated LOW-COMP long-dated stable farms (found 9/4) — matched by name so STABLE only
# books ~6-8 markets, not a 40-book date-scan (which the CLOB throttles). Override via
# LP_STABLE_MARKETS. These are the ideal farms: stable prob + comp~0 + decent pool.
STABLE_DEFAULT = ("second-best code;ricardo ferr;gladyz butanda;republican party win the mo;"
                  "trump publicly insult;lula da silva;bolsonaro win;jd vance win;putin out;"
                  "james bond;invade iran;lindsay clancy;iranian blockade;astra model;"
                  "sonnet;openai gpt;best ai model;fed rate;ceasefire continues")
STABLE_NAMES = [s.strip().lower() for s in os.environ.get("LP_STABLE_MARKETS", STABLE_DEFAULT).split(";") if s.strip()]

# SHIELD mode (LP_SHIELD=1): tests the "positioning inversion" solution to adverse selection.
# The live pilot bled because we were the SOLE/best maker (ate all informed flow). SHIELD models
# the opposite: quote DEEPER in the band (LP_EDGE_K cents from mid) in COMPETITIVE markets so
# (a) reward = CLOSENESS-weighted ((v-s)/v)^2 share (Polymarket's real scoring — quoting wide
# earns less), and (b) FILLS are QUEUE-SHIELDED: competitors resting ahead absorb the first
# ~comp/2 shares of each taker trade; we only catch the overflow (large trades). Hypothesis:
# competition shields us from most (noise) fills → reward dominates. Writes sim_shield.db.
SHIELD = os.environ.get("LP_SHIELD", "0").strip() in ("1", "true", "yes", "on")
EDGE_K = float(os.environ.get("LP_EDGE_K", "1.5") or 1.5) / 100.0   # SHIELD quote distance from mid (cents)
SHIELD_MINCOMP = float(os.environ.get("LP_SHIELD_MINCOMP", "300") or 300)     # need competitors ahead to shield fills
SHIELD_MAXCOMP = float(os.environ.get("LP_SHIELD_MAXCOMP", "4000") or 4000)   # but not so deep our reward share vanishes

# --- LIVE-FIDELITY knobs (make the paper sim match how farm.py actually behaves) ---
# The live pilot diverged from the sim because the sim was reward-OPTIMISTIC and adverse-COARSE.
# These make every mode model live mechanics: we quote K cents OFF mid (not at it), orders are
# only in-band a fraction of the time (re-quote gaps), fills are marked against the REALIZED
# forward price (true adverse selection), and unwinding inventory costs the spread.
QUOTE_K   = float(os.environ.get("LP_K_CENTS", "1.0") or 1.0) / 100.0   # default quote distance = farm.py's LP_K_CENTS
REWARD_UPTIME = float(os.environ.get("LP_REWARD_UPTIME", "0.85") or 0.85)  # frac of minutes orders actually rest in-band
MARKOUT_H = float(os.environ.get("LP_MARKOUT_H", "2.0") or 2.0)         # mark fills vs realized price this many h forward
EXIT_SLIP = float(os.environ.get("LP_EXIT_SLIP", "1.0") or 1.0) / 100.0  # cents paid crossing the book to unwind a fill
REWARD_MIN_DAY = float(os.environ.get("LP_REWARD_MIN_DAY", "1.0") or 1.0)  # Polymarket pays a day only if it earns >= this ($1); else $0, no rollover
MAX_ACCRUAL_H = float(os.environ.get("LP_MAX_ACCRUAL_H", "2.0") or 2.0)    # cap per-cycle reward window (poll cadence ~1h); prevents crediting rotation-absence gaps
# The doc ("your earnings for that day") is ambiguous whether the $1/day floor is per-MARKET or
# per-ACCOUNT. Decisive for us: per-market zeroes our many sub-$1 farms; per-account sums them
# first. Default to per-market (conservative); flip via LP_REWARD_FLOOR=account. Verify against
# the live rewards page (per-market earnings) / portfolio history (actually-paid) when we have it.
REWARD_FLOOR = os.environ.get("LP_REWARD_FLOOR", "market").strip().lower()
_name = ("sim_shield.db" if SHIELD else "sim_extreme.db" if EXTREME else "sim_stable.db" if STABLE
         else "sim_broad_managed.db" if (BROAD and MANAGED)
         else "sim_broad.db" if BROAD else "sim_managed.db" if MANAGED else "sim_lp.db")
DB = Path(__file__).resolve().parent / _name


def cfg_markets():
    return [s.strip().lower() for s in os.environ.get("LP_MARKETS", DURABLE_DEFAULT).split(";") if s.strip()]

def our_size():
    try: return float(os.environ.get("LP_DEFAULT_SIZE", "20"))
    except Exception: return 20.0

def k_off():
    try: return float(os.environ.get("LP_K_CENTS", "1.0")) / 100.0
    except Exception: return 0.01


def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS reward (ts INTEGER, market TEXT, pool REAL,
                   comp REAL, our_share REAL, dt_h REAL, reward_win REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fill (ts INTEGER, trade_ts INTEGER, market TEXT,
                   taker_side TEXT, price REAL, trade_size REAL, our_fill REAL, mid_now REAL, pnl REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS checkpoint (market TEXT PRIMARY KEY,
                   last_trade_ts INTEGER, last_reward_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS comp_cache (cond TEXT PRIMARY KEY,
                   comp REAL, mid REAL, ts INTEGER)""")
    try:                                              # store cond per fill so flow-toxicity of a
        con.execute("ALTER TABLE fill ADD COLUMN cond TEXT")   # bleeding market is analyzable later
    except Exception:
        pass                                          # column already exists
    con.commit(); con.close()


# comp-cache: booking every candidate each poll trips the CLOB /book rolling-window throttle
# (~24 rapid calls -> transient 403). Cache each market's competition; reuse if fresh, and cap
# how many STALE markets we (re)book per poll so steady-state books only the few farmable ones.
COMP_TTL   = float(os.environ.get("LP_COMP_TTL_H", "6") or 6) * 3600.0   # cache freshness (6h: gentle re-book)
BOOK_BUDGET = int(os.environ.get("LP_BOOK_BUDGET", "3") or 3)            # max fresh books / poll (tiny: never re-trip throttle)


def _comp_cache_load():
    con = sqlite3.connect(DB);
    try:
        rows = con.execute("SELECT cond, comp, mid, ts FROM comp_cache").fetchall()
    except Exception:
        rows = []
    con.close()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _comp_cache_put(cond, comp, mid, ts):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO comp_cache VALUES (?,?,?,?)", (cond, comp, mid, ts))
    con.commit(); con.close()


def fetch_trades(condition_id, token0, since_ts):
    """Real taker trades on token0 for a market, newer than since_ts (data-api)."""
    url = f"https://data-api.polymarket.com/trades?market={condition_id}&limit=500"
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=_ctx, timeout=25))
    except Exception:
        return []
    out = []
    for t in (d if isinstance(d, list) else []):
        try:
            ts = int(t.get("timestamp") or 0)
            if ts <= since_ts:
                continue
            if str(t.get("asset")) != str(token0):
                continue   # only the token we quote (outcome 0)
            out.append((ts, str(t.get("side")).upper(), float(t.get("price")), float(t.get("size"))))
        except Exception:
            continue
    return sorted(out)


def target_farms():
    subs = cfg_markets()
    farms = []
    cands = []
    for m in scan.fetch_incentivized():
        q = scan._ascii(m.get("question", "")); ql = q.lower()
        if EXTREME:
            if any(x in ql for x in EXCLUDE):       # exclude temp/crypto (toxic near-resolution)
                continue
            try:
                bb = float(m.get("bestBid") or 0); ba = float(m.get("bestAsk") or 0)
                gmid = (bb + ba) / 2 if (bb and ba) else 0.5
            except Exception:
                continue
            if not (0 < gmid < 0.08 or gmid > 0.92):   # near-decided extreme only
                continue
        elif STABLE or SHIELD:                           # SHIELD shares STABLE's safe long-dated universe
            if any(x in ql for x in EXCLUDE):            # never farm toxic classes (temp/crypto)
                continue
            if STABLE_NAMES:                             # curated whitelist if one is provided
                if not any(s in ql for s in STABLE_NAMES):
                    continue
            else:                                        # DYNAMIC: any long-dated stable market
                try:
                    end = datetime.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
                    if (end - datetime.datetime.now(datetime.timezone.utc)).days < STABLE_DAYS:
                        continue
                except Exception:
                    continue
        elif BROAD:
            if any(x in ql for x in EXCLUDE):       # drop toxic classes (temp, crypto)
                continue
        else:
            if not any(s in ql for s in subs):      # durable-3 name whitelist
                continue
        pool = scan.reward_rate(m)
        if pool <= 0 or ((BROAD or STABLE) and pool < BROAD_MINPOOL):
            continue
        if MANAGED:                                   # exit before resolution
            try:
                end = datetime.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
                htr = (end - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600.0
                if htr < EXIT_H:
                    continue
            except Exception:
                pass
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        cond = m.get("conditionId")
        if not toks or not cond:
            continue
        max_spr = float(m.get("rewardsMaxSpread") or 3.0) / 100.0
        try:
            liq = float(m.get("liquidity") or 0)
        except Exception:
            liq = 0.0
        try:
            min_size = float(m.get("rewardsMinSize") or 0)   # min shares/order to qualify for rewards
        except Exception:
            min_size = 0.0
        cands.append(dict(q=q, cond=cond, token0=toks[0], max_spr=max_spr, pool=pool, liq=liq, min_size=min_size))

    # book low-liquidity markets FIRST: competition correlates with liquidity, so the farmable
    # low-comp obscure races (low liquidity) get the book budget before the crowded macro pools.
    cands.sort(key=lambda c: c["liq"])   # low-liq first; comp cache accumulates the moderate band across polls

    # resolve competition via cache; only (re)book stale/unknown markets, capped per poll to
    # stay under the /book rolling-window throttle. crowded markets stay cached and are skipped.
    cache = _comp_cache_load()
    now_ts = int(time.time())
    booked = 0
    for c in cands:
        cached = cache.get(c["cond"])
        if cached and (now_ts - cached[2]) < COMP_TTL:
            comp, mid = cached[0], cached[1]
        elif booked < BOOK_BUDGET:
            bids, asks = scan.book(c["token0"])
            booked += 1
            time.sleep(1.5)                           # generous spacing; book() retries once on fail, so keep bursts tiny
            if not bids or not asks:
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            comp = (sum(sz for p, sz in bids if p >= mid - c["max_spr"])
                    + sum(sz for p, sz in asks if p <= mid + c["max_spr"]))
            _comp_cache_put(c["cond"], comp, mid, now_ts)
        else:
            continue                                  # budget spent; this market waits for next poll
        if BROAD and comp > BROAD_MAXCOMP:            # only low-competition farms
            continue
        if STABLE and comp > STABLE_MAXCOMP:
            continue
        if SHIELD and not (SHIELD_MINCOMP <= comp <= SHIELD_MAXCOMP):   # moderate comp: shielded AND worth farming
            continue
        farms.append(dict(q=c["q"], cond=c["cond"], token0=c["token0"], mid=mid, pool=c["pool"], comp=comp, max_spr=c["max_spr"], min_size=c.get("min_size", 0.0)))
    return farms


def poll():
    init_db()
    farms = target_farms()
    now = int(time.time())
    S = our_size()
    con = sqlite3.connect(DB)
    print(f"=== sim poll  {len(farms)} farm(s) live  (our_size={S:.0f}/side) ===")
    for f in farms:
        cp = con.execute("SELECT last_trade_ts, last_reward_ts FROM checkpoint WHERE market=?", (f["q"],)).fetchone()
        first = cp is None
        # MANAGED = forward-only (no 24h seed): the seed backfills near-resolution trades
        # which contaminates the managed net (the exit rule governs FORWARD farming, not
        # historical trades). Unmanaged keeps the seed for faster sampling.
        seed_start = now if (first and (MANAGED or SHIELD)) else (now - SEED_LOOKBACK_H * 3600)
        last_trade_ts = cp[0] if cp else seed_start
        last_reward_ts = cp[1] if cp else now

        # We quote k cents OFF mid (SHIELD quotes deeper; other modes = farm.py's LP_K_CENTS),
        # so reward is CLOSENESS-weighted by ((v-k)/v)^2 (Polymarket's real per-minute scoring) —
        # quoting off-mid earns a smaller share than the old quote-at-mid assumption. Reward is
        # further scaled by REWARD_UPTIME (orders aren't in-band 100% of the time through re-quotes).
        v = f.get("max_spr") or 0.03
        k = min(EDGE_K if SHIELD else QUOTE_K, 0.9 * v)     # our depth from mid (kept inside band)
        cl = ((v - k) / v) ** 2 if v else 1.0               # closeness multiplier (1.0 only at touch)
        eff = 2 * S * cl                                     # our closeness-weighted score
        # reward accrual for the elapsed window (0 on first sight).
        # CAP dt_h at MAX_ACCRUAL_H: a market only earns reward for time it was ACTUALLY in the
        # farm set. If it rotates out (0-farm gap / comp>cap) and back, its checkpoint's
        # last_reward_ts is stale, so a raw (now-last_reward_ts) would credit the whole ABSENCE
        # gap as if farmed continuously (the BoC bug: 130h gap = $494 fake reward). Capping at a
        # bit over the poll cadence means an absent market earns only ~one poll window on re-entry.
        share = eff / (f["comp"] + eff) if (f["comp"] + eff) else 0
        if f.get("min_size", 0) and S < f["min_size"]:      # our order too small to qualify -> $0 reward
            share = 0.0
        dt_h = 0.0 if first else max(0.0, (now - last_reward_ts) / 3600.0)
        dt_h = min(dt_h, MAX_ACCRUAL_H)                     # no crediting rotation-absence gaps
        rwin = f["pool"] * share * (dt_h / 24.0) * REWARD_UPTIME
        con.execute("INSERT INTO reward VALUES (?,?,?,?,?,?,?)",
                    (now, f["q"], f["pool"], f["comp"], share, dt_h, rwin))

        # Adverse fills from real prints since checkpoint, marked to LIVE reality:
        #  (1) DEPTH GATE — we rest k off mid, so a print only fills us if it swept to our depth.
        #      Small trades hit competitors ahead of us; the deep sweeps that reach us skew toward
        #      the large/informed side. This is real adverse selection, not a modeling choice.
        #  (2) FORWARD MARKOUT — each fill is priced against the REALIZED price ~MARKOUT_H hours
        #      later (median of forward prints), NOT the poll mid. Informed flow that keeps running
        #      shows as a loss; noise that mean-reverts shows the captured spread. This is what the
        #      old mark-to-poll-mid missed and why the sim was adverse-blind vs live.
        #  (3) EXIT SLIP — flattening inventory crosses the book, charged per fill.
        #  (4) DEFERRED SETTLE — only fills older than MARKOUT_H settle this poll; newer ones wait
        #      for their forward window (checkpoint isn't advanced past them).
        side_share = S / (S + f["comp"] / 2.0) if (S + f["comp"] / 2.0) else 1.0
        side_share = min(1.0, side_share)
        trades = fetch_trades(f["cond"], f["token0"], last_trade_ts)
        pxs = [(t0, P0) for t0, _s0, P0, _z0 in trades]        # forward-price series for markout
        mark_cut = now - MARKOUT_H * 3600                       # only settle fills w/ a full forward window
        nfill = 0; fpnl = 0.0; newlast = last_trade_ts
        inv = con.execute("SELECT COALESCE(SUM(CASE WHEN taker_side='SELL' THEN our_fill ELSE -our_fill END),0)"
                          " FROM fill WHERE market=?", (f["q"],)).fetchone()[0] if MANAGED else 0.0
        cap_ct = INV_CAP / f["mid"] if f["mid"] else INV_CAP   # inventory cap in contracts
        for ts, tside, P, sz in trades:
            if ts > mark_cut:              # too recent to mark out -> leave for a later poll
                break                      # (trades sorted ascending; checkpoint stays behind them)
            # depth gate: did the print reach our quote resting at mid -/+ k?
            if tside == "BUY" and P < f["mid"] + k:      # ask side: never reached mid+k
                newlast = max(newlast, ts); continue
            if tside == "SELL" and P > f["mid"] - k:     # bid side: never reached mid-k
                newlast = max(newlast, ts); continue
            fill = min(S, sz * side_share)
            if fill <= 0:
                newlast = max(newlast, ts); continue
            fw = [pp for (tt, pp) in pxs if ts <= tt <= ts + MARKOUT_H * 3600]   # realized fwd prices
            fwd = statistics.median(fw) if fw else P
            if tside == "BUY":            # taker lifted asks -> we SOLD at P (short, inv -fill)
                if MANAGED and (inv - fill) < -cap_ct:    # short cap hit -> managed MM pulls ask
                    newlast = max(newlast, ts); continue
                inv -= fill
                pnl = (P - fwd) * fill - EXIT_SLIP * fill   # short profits if price falls to fwd
            else:                          # taker hit bids -> we BOUGHT at P (long, inv +fill)
                if MANAGED and (inv + fill) > cap_ct:     # long cap hit -> managed MM pulls bid
                    newlast = max(newlast, ts); continue
                inv += fill
                pnl = (fwd - P) * fill - EXIT_SLIP * fill   # long profits if price rises to fwd
            con.execute("INSERT INTO fill (ts,trade_ts,market,taker_side,price,trade_size,"
                        "our_fill,mid_now,pnl,cond) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (now, ts, f["q"], tside, P, sz, fill, fwd, pnl, f["cond"]))
            nfill += 1; fpnl += pnl
            newlast = max(newlast, ts)
        con.execute("INSERT OR REPLACE INTO checkpoint VALUES (?,?,?)", (f["q"], newlast, now))
        print("  %-40s pool$%-5.0f comp$%-5.0f share%.2f | +$%.3f reward, %d fills $%+.3f"
              % (f["q"][:40], f["pool"], f["comp"], share, rwin, nfill, fpnl))
    con.commit(); con.close()
    if not farms:
        print("  (no target farms live this poll — durable-3 rotate; nothing to accrue)")


def report():
    init_db()
    con = sqlite3.connect(DB)
    win_h = con.execute("SELECT (MAX(ts)-MIN(ts))/3600.0 FROM reward").fetchone()[0] or 0
    print(f"=== LP paper simulator — net P&L over ~{win_h:.1f}h ===\n")
    print("%-40s %9s %8s %7s %8s %7s" % ("farm", "reward$", "fill$", "fills", "net$", "inv$"))
    print("-" * 86)
    mkts = [r[0] for r in con.execute("SELECT DISTINCT market FROM reward").fetchall()]
    # Polymarket's $1/day floor: a day earning <$1 pays $0 and does NOT roll over (old sim summed
    # the fractions = optimistic). Compute BOTH scopes since the doc is ambiguous:
    #  per-MARKET: each market-day must clear $1 on its own (harsh — zeroes sub-$1 farms)
    #  per-ACCOUNT: sum all markets within a UTC day, pay if the day's total >= $1
    mkt_paid = {}; tot_gross = 0.0
    for m in mkts:
        days = con.execute("SELECT ts/86400 AS d, COALESCE(SUM(reward_win),0) FROM reward "
                           "WHERE market=? GROUP BY d", (m,)).fetchall()
        mkt_paid[m] = sum(r for _d, r in days if r >= REWARD_MIN_DAY)
        tot_gross += sum(r for _d, r in days)
    r_market = sum(mkt_paid.values())
    acct_days = con.execute("SELECT ts/86400 AS d, COALESCE(SUM(reward_win),0) FROM reward GROUP BY d").fetchall()
    r_account = sum(r for _d, r in acct_days if r >= REWARD_MIN_DAY)
    tot_r = r_account if REWARD_FLOOR == "account" else r_market
    tot_f = 0.0
    for m in sorted(mkts):
        rw = mkt_paid[m]
        fp = con.execute("SELECT COALESCE(SUM(pnl),0) FROM fill WHERE market=?", (m,)).fetchone()[0]
        nf = con.execute("SELECT COUNT(*) FROM fill WHERE market=?", (m,)).fetchone()[0]
        inv = con.execute("""SELECT COALESCE(SUM(CASE WHEN taker_side='SELL' THEN our_fill ELSE -our_fill END *
                             price),0) FROM fill WHERE market=?""", (m,)).fetchone()[0]
        tot_f += fp
        print("%-40s %9.3f %8.3f %7d %8.3f %7.1f" % (m[:40], rw, fp, nf, rw + fp, inv))
    print("-" * 86)
    net = tot_r + tot_f
    print("%-40s %9.3f %8.3f %7s %8.3f" % ("TOTAL", tot_r, tot_f, "", net))
    # adverse decomposition
    adv = con.execute("SELECT COALESCE(SUM(pnl),0) FROM fill WHERE pnl<0").fetchone()[0]
    fav = con.execute("SELECT COALESCE(SUM(pnl),0) FROM fill WHERE pnl>=0").fetchone()[0]
    nadv = con.execute("SELECT COUNT(*) FROM fill WHERE pnl<0").fetchone()[0]
    ntot = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    print(f"  reward (subsidy PAID)     : ${tot_r:+.3f}  [floor={REWARD_FLOOR}] closeness-wt x{REWARD_UPTIME:.0%} uptime")
    print(f"     ${tot_gross:.2f} gross → per-market floor ${r_market:.2f} | per-account floor ${r_account:.2f}")
    print(f"  fill P&L (markout - slip) : ${tot_f:+.3f}   from {ntot} modeled fills")
    print(f"    adverse fills ${adv:+.3f} ({nadv}), favorable ${fav:+.3f} ({ntot-nadv})")
    print(f"  ---------------------------------------------")
    print(f"  NET paper                 : ${net:+.3f}")
    if win_h > 1:
        print(f"  annualiz.-free daily rate : ${net/win_h*24:+.2f}/day  (extrapolated, thin sample)")
    print(f"\n  LIVE-FIDELITY model: reward = closeness((v-k)/v)^2 x {REWARD_UPTIME:.0%} uptime; fills"
          f" depth-gated at k={QUOTE_K*100:.1f}c\n  (SHIELD {EDGE_K*100:.1f}c), marked to realized price"
          f" {MARKOUT_H:.0f}h forward minus {EXIT_SLIP*100:.1f}c exit slip. NET is the decision number —")
    print(f"  watch its SIGN and whether adverse markout swamps the subsidy (the live failure mode).")
    con.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "poll":
        poll()
    elif cmd == "reset":
        DB.unlink(missing_ok=True); print("sim_lp.db reset")
    else:
        report()
