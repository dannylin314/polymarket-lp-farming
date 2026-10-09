"""Per-market FLOW-TOXICITY filter for LP farm selection.

The live pilot lost money because the farm quoted markets that looked stable/low-comp
but were repricing on INFORMED flow (Grok AI-release, esports, Sønderjyske relegation) —
our resting bids got run over. A market is TOXIC to farm when recent taker flow shows
informed repricing: the mid has DRIFTED / the price RANGE is wide, and/or flow is one-sided.
Quiet, price-stable, balanced-flow markets are SAFE.

Signal from data-api /trades (NOT throttled). Compute on the dominant traded token so YES/NO
prices aren't mixed.

  flow_metrics(cond, hours) -> {n, rng, drift, imb, vel}
  is_toxic(m)               -> bool  (thresholds below)
  python flowtox.py         -> validate metrics vs sim_stable.db realized fill P&L
"""
import os, sys, json, time, ssl, urllib.request, sqlite3
from pathlib import Path

_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# thresholds (tunable via env); a market is toxic if it has real flow AND is repricing
TOX_MINTRADES = int(os.environ.get("FLOWTOX_MINTRADES", "6") or 6)      # need flow to judge
TOX_MAXRANGE  = float(os.environ.get("FLOWTOX_MAXRANGE", "0.06") or 0.06)  # recent price range (cents)
TOX_MAXDRIFT  = float(os.environ.get("FLOWTOX_MAXDRIFT", "0.04") or 0.04)  # net directional move
TOX_MAXIMB    = float(os.environ.get("FLOWTOX_MAXIMB", "0.60") or 0.60)    # one-sided taker flow


def flow_metrics(cond, hours=24):
    """Recent taker-flow toxicity metrics for a market, on its dominant traded token."""
    url = f"https://data-api.polymarket.com/trades?market={cond}&limit=500"
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=_ctx, timeout=15))
    except Exception:
        return None
    if not isinstance(d, list) or not d:
        return dict(n=0, rng=0.0, drift=0.0, imb=0.0, vel=0.0)
    cut = time.time() - hours * 3600
    tr = [t for t in d if isinstance(t, dict) and (t.get("timestamp") or 0) > cut]
    if not tr:
        return dict(n=0, rng=0.0, drift=0.0, imb=0.0, vel=0.0)
    # dominant token so YES/NO prices aren't mixed
    from collections import Counter
    dom = Counter(str(t.get("asset")) for t in tr).most_common(1)[0][0]
    ta = sorted([t for t in tr if str(t.get("asset")) == dom], key=lambda t: t.get("timestamp") or 0)
    if len(ta) < 2:
        return dict(n=len(ta), rng=0.0, drift=0.0, imb=0.0, vel=0.0)
    ps = [float(t.get("price")) for t in ta]
    buys = sum(1 for t in ta if str(t.get("side")).upper() == "BUY")
    n = len(ta)
    rng = max(ps) - min(ps)                       # realized range
    drift = abs(ps[-1] - ps[0])                    # net directional move
    imb = abs(buys - (n - buys)) / n               # taker-side imbalance
    span_h = max(1e-6, (ta[-1]["timestamp"] - ta[0]["timestamp"]) / 3600.0)
    vel = drift / span_h                            # move per hour
    return dict(n=n, rng=rng, drift=drift, imb=imb, vel=vel)


def is_toxic(m):
    """True = avoid farming this market (informed repricing)."""
    if not m or m["n"] < TOX_MINTRADES:
        return False            # too little flow to be judged toxic (and low-flow = low adverse)
    return (m["rng"] > TOX_MAXRANGE) or (m["drift"] > TOX_MAXDRIFT) or (m["imb"] >= TOX_MAXIMB and m["drift"] > 0.02)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db = Path(__file__).resolve().parent / "sim_stable.db"
    rows = sqlite3.connect(db).execute(
        "SELECT market, cond, COUNT(*) n, SUM(pnl) pnl FROM fill WHERE cond IS NOT NULL GROUP BY cond ORDER BY pnl").fetchall()
    print("validate flow-toxicity vs realized fill P&L (per farmed market):")
    print("%7s %4s %6s %6s %5s %5s  %-38s %s" % ("pnl", "ntr", "rng", "drift", "imb", "TOX?", "market", ""))
    for q, cond, nf, pnl in rows:
        m = flow_metrics(cond)
        if not m:
            print("  (no trades) ", q[:38]); continue
        print("%7.2f %4d %6.3f %6.3f %5.2f  %-4s %-38s" % (
            pnl, m["n"], m["rng"], m["drift"], m["imb"], "TOXIC" if is_toxic(m) else "ok", q[:38]))
        time.sleep(0.15)
