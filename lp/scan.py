"""Polymarket Liquidity-Rewards (LP) farming scanner.

Polymarket pays a daily reward pool to makers whose resting limit orders sit
within a market's `rewardsMaxSpread` of the midpoint, scored by size x closeness.
This finds the FARMABLE markets = high reward pool relative to how much maker
size is already competing for it, and estimates a small-account daily yield.

The edge is NOT prediction — it's harvesting the subsidy on LOW-competition
markets, ideally STABLE ones (far from resolution / low volatility) so resting
orders rarely get adversely filled. See memory project_orderflow_research
(far-from-close passive MM has ~nil adverse selection) and project_mm_replication
(broad MM net-neg on spread alone — the reward subsidy is the missing income).

  python scan.py            - rank farmable markets now
  python scan.py log        - snapshot the ranking to lp.db (competition over time)
  python scan.py weather    - only the incentivized weather temp markets (our edge)

CAVEAT: est_daily uses total qualifying size as a competition proxy (Polymarket's
real score is size x a closeness-quadratic); treat yields as RELATIVE ranking, not
exact. Requires VPN to actually post orders (UK /order geoblock). Paper-only here.
"""
import json, sqlite3, sys, time, urllib.request, ssl, datetime
from pathlib import Path

DB = Path(__file__).resolve().parent / "lp.db"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE


def g(url, tries=2):
    for _ in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=_ctx, timeout=25))
        except Exception:
            time.sleep(0.4)
    return None


def _ascii(s):
    return (s or "").encode("ascii", "replace").decode("ascii")


def reward_rate(m):
    """Daily reward pool $ for a market (clobRewards is the live field)."""
    cr = m.get("clobRewards") or []
    best = 0.0
    now = datetime.date.today().isoformat()
    for r in cr:
        try:
            rate = float(r.get("rewardsDailyRate") or 0)
        except Exception:
            rate = 0
        # only count a reward window that is currently active
        sd, ed = r.get("startDate") or "0000", r.get("endDate") or "9999"
        if rate > best and sd <= now <= ed:
            best = rate
    if not best:
        try:
            best = float(m.get("rewardsDailyRate") or 0)
        except Exception:
            best = 0
    return best


def book(token):
    b = g(f"https://clob.polymarket.com/book?token_id={token}")
    if not isinstance(b, dict):
        return None, None
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks


def analyse(m):
    """Return a farmability record for one incentivized market, or None."""
    pool = reward_rate(m)
    if pool <= 0:
        return None
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
    except Exception:
        return None
    if not toks:
        return None
    max_spr = float(m.get("rewardsMaxSpread") or 3.0) / 100.0   # cents -> price
    min_sz = float(m.get("rewardsMinSize") or 0)
    bids, asks = book(toks[0])
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    # qualifying resting SIZE within maxSpread of mid = the competition for the pool
    qb = sum(sz for p, sz in bids if p >= mid - max_spr)
    qa = sum(sz for p, sz in asks if p <= mid + max_spr)
    comp = qb + qa
    # a small account posts min_sz per side near mid -> rough pool share
    mine = 2 * min_sz if min_sz else 200
    share = mine / (comp + mine) if (comp + mine) else 0
    est_daily = pool * share
    # capital tied up ~ your resting notional (bid pays mid, ask pays 1-mid)
    capital = min_sz * mid + min_sz * (1 - mid) if min_sz else mine * 0.5
    yld = (est_daily / capital * 100) if capital else 0   # % daily on capital
    # stability: near-resolution (mid extreme) or tight book = benign fills
    stability = abs(mid - 0.5)  # 0=coinflip(risky), 0.5=near-resolved(stable)
    q = _ascii(m.get("question", ""))
    weather = ("temperature" in q.lower())
    return dict(q=q[:46], pool=pool, maxspr=max_spr*100, minsz=min_sz, mid=mid,
                comp=comp, est_daily=est_daily, capital=capital, yld=yld,
                stability=stability, weather=weather, vol=float(m.get("volume24hr") or 0))


def fetch_incentivized():
    out = []
    seen = set()
    for off in range(0, 6000, 500):
        d = g(f"https://gamma-api.polymarket.com/markets?closed=false&limit=500&offset={off}&order=volume24hr&ascending=false")
        if not isinstance(d, list) or not d:
            break
        for m in d:
            if reward_rate(m) > 0 and m.get("id") not in seen:
                seen.add(m.get("id")); out.append(m)
        time.sleep(0.15)
    return out


def scan(weather_only=False, do_log=False):
    mks = fetch_incentivized()
    recs = []
    for m in mks:
        r = analyse(m)
        if r and (not weather_only or r["weather"]):
            recs.append(r)
        time.sleep(0.03)
    # rank by estimated daily yield on capital, then by pool/competition
    recs.sort(key=lambda r: (r["yld"], r["est_daily"]), reverse=True)
    print(f"incentivized markets scanned: {len(recs)}"
          + ("  [weather only]" if weather_only else ""))
    print("%-46s %6s %5s %6s %9s %7s %6s %4s" % (
        "market", "pool$/d", "maxc", "minSz", "compSize", "est$/d", "yld%/d", "stab"))
    print("-" * 104)
    for r in recs[:30]:
        flag = " W" if r["weather"] else ""
        print("%-46s %6.0f %5.1f %6.0f %9.0f %7.1f %6.1f %4.2f%s" % (
            r["q"], r["pool"], r["maxspr"], r["minsz"], r["comp"],
            r["est_daily"], r["yld"], r["stability"], flag))
    tot = sum(r["est_daily"] for r in recs[:10])
    print(f"\ntop-10 combined est reward: ${tot:.0f}/day (~${tot*30:.0f}/mo) at min-size, "
          f"pre-competition-decay, paper. 'W'=weather market (our forecast edge).")
    if do_log:
        con = sqlite3.connect(DB)
        con.execute("""CREATE TABLE IF NOT EXISTS lp_snap (ts INTEGER, q TEXT, pool REAL,
                       maxspr REAL, minsz REAL, mid REAL, comp REAL, est_daily REAL, yld REAL)""")
        now = int(time.time())
        for r in recs:
            con.execute("INSERT INTO lp_snap VALUES (?,?,?,?,?,?,?,?,?)",
                        (now, r["q"], r["pool"], r["maxspr"], r["minsz"], r["mid"],
                         r["comp"], r["est_daily"], r["yld"]))
        con.commit(); con.close()
        print(f"logged {len(recs)} markets to lp.db")


def income(capital, decay=1.0):
    """Realistic take-home: allocate `capital` $ across farmable markets by water-filling
    (reward pool_i * x_i/(C_i+x_i) is concave -> equalize marginal reward pool_i*C_i/(C_i+x_i)^2).
    decay multiplies existing competition C to model crowding as you/others pile in.
    Returns (gross_daily, allocations). Rewards are GROSS (adverse-fill risk is separate,
    minimised by the quote overlay)."""
    mks = fetch_incentivized()
    farms = []
    for m in mks:
        r = analyse(m)
        if r and r["pool"] > 0:
            C = max(r["comp"] * decay, r["minsz"] or 1)   # effective competition (shares~$ near 0.5)
            farms.append((r["pool"], C, r["minsz"] or 0, r["q"]))
        time.sleep(0.02)
    # bisection on lambda: x_i(lambda)=max(0, sqrt(pool*C/lambda)-C); find lambda st sum x = capital
    def alloc(lam):
        xs = []
        for pool, C, mn, q in farms:
            x = max(0.0, math.sqrt(pool * C / lam) - C) if lam > 0 else 0
            if x > 0 and x < mn:      # below min qualifying size -> not worth it
                x = 0
            xs.append(x)
        return xs
    lo, hi = 1e-9, 1e6
    for _ in range(80):
        mid = (lo * hi) ** 0.5
        if sum(alloc(mid)) > capital:
            lo = mid
        else:
            hi = mid
    xs = alloc((lo * hi) ** 0.5)
    total = 0.0
    out = []
    for (pool, C, mn, q), x in sorted(zip(farms, xs), key=lambda t: -t[1]):
        if x <= 0:
            continue
        daily = pool * x / (C + x)
        total += daily
        out.append((q, x, daily, pool, C))
    return total, out


import math  # noqa (used by income)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "income":
        cap = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
        print(f"=== realistic LP take-home for ${cap:.0f} capital (water-filled) ===")
        for lbl, dc in (("OPTIMISTIC (current competition)", 1.0),
                        ("REALISTIC (competition x2.5 as farms crowd)", 2.5)):
            tot, out = income(cap, dc)
            print(f"\n{lbl}:  ~${tot:.0f}/day  (~${tot*30:.0f}/mo)  across {len(out)} markets")
            for q, x, daily, pool, C in out[:8]:
                print(f"    ${x:6.0f} into {q:38s} -> ${daily:5.1f}/d  (pool ${pool:.0f}, comp ${C:.0f})")
        print("\nGROSS rewards; adverse-fill risk separate (quote overlay minimises it).")
        print("Real take-home lands between the two rows; VPN needed to post; paper.")
        sys.exit(0)
    if cmd == "weather":
        scan(weather_only=True)
    elif cmd == "log":
        scan(do_log=True)
    else:
        scan()
