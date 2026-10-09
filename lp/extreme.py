"""Extreme-priced LP-farm checker — tests the "95/5 small-account" tweet claim.

The tweet ("$1500/mo, buy the <5c side, two legs in the band, drag daily") is
mechanically correct (= farm.py) but the edge depends entirely on MARKET SELECTION:
a $45-$1400 account only earns if it's a meaningful fraction of the resting size.
On any extreme market with a live pool, competition is enormous (100k-11M shares
resting in the band) so a small account's reward share rounds to ~0. The few that
ARE low-comp are short-dated toxic markets (SPY-daily, temp-daily, gold-up/down)
where the cheap side decays to zero at resolution and you lose principal to adverse
fills faster than rewards accrue.

A GOOD extreme farm would need ALL of: extreme mid (>=0.95 or <=0.05), real pool
(>=MINPOOL), LOW competition (small acct share >= MINSHARE), and LONG-DATED
(>=DAYS to resolution, i.e. NOT the toxic daily-decay markets). This scanner
confirms every cycle whether such a thing exists (it hasn't).

Pipeline mirrors dormant.py (throttle-safe): gamma filter first (no book), book
only the long-dated extreme finalists.

  python extreme.py          - scan and print any earnable extreme farm live now
  python extreme.py --alert  - same, exit code 2 if a GOOD farm is found (loop hook)

Env: EX_MINPOOL(30) EX_DAYS(20) EX_MINSHARE(0.10) EX_OURSH(1000)
Paper/discovery only; posting needs a VPN (UK /order geoblock).
"""
import os, sys, json, time, datetime, sqlite3
from pathlib import Path
import scan

LOGDB = Path(__file__).resolve().parent / "extreme_log.db"

MINPOOL  = float(os.environ.get("EX_MINPOOL", "30") or 30)
DAYS     = float(os.environ.get("EX_DAYS", "20") or 20)      # long-dated => excludes toxic daily-decay
MINSHARE = float(os.environ.get("EX_MINSHARE", "0.10") or 0.10)
OURSH    = float(os.environ.get("EX_OURSH", "1000") or 1000)  # ~$45 on a 4.5c side
EXCLUDE  = ("bitcoin", "ethereum", "up or down", "higher or lower", "solana", " xrp ", "dogecoin")


def log_scan(nextreme, nlong, good):
    con = sqlite3.connect(LOGDB)
    con.execute("CREATE TABLE IF NOT EXISTS scan (ts INTEGER, nextreme INT, nlong INT, ngood INT)")
    con.execute("CREATE TABLE IF NOT EXISTS goodfarm (ts INTEGER, q TEXT, mid REAL, pool REAL, comp REAL, share REAL, est_daily REAL)")
    now = int(time.time())
    con.execute("INSERT INTO scan VALUES (?,?,?,?)", (now, nextreme, nlong, len(good)))
    for g in good:
        con.execute("INSERT INTO goodfarm VALUES (?,?,?,?,?,?,?)",
                    (now, g["q"], g["mid"], g["pool"], g["comp"], g["share"], g["est_daily"]))
    con.commit(); con.close()


def scan_extreme():
    now = datetime.datetime.now(datetime.timezone.utc)
    extreme = []
    for m in scan.fetch_incentivized():
        ql = scan._ascii(m.get("question", "")).lower()
        if any(x in ql for x in EXCLUDE):
            continue
        try:
            bb = float(m.get("bestBid") or 0); ba = float(m.get("bestAsk") or 0)
        except Exception:
            continue
        mid = (bb + ba) / 2 if (bb and ba) else (bb or ba)
        if not mid or (0.05 < mid < 0.95):
            continue
        extreme.append((mid, m))
    # long-dated + real pool (no book yet)
    longdated = []
    for mid, m in extreme:
        pool = scan.reward_rate(m)
        if pool < MINPOOL:
            continue
        try:
            end = datetime.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
            if (end - now).days < DAYS:
                continue
        except Exception:
            continue
        longdated.append((mid, pool, m))
    longdated.sort(key=lambda r: -r[1])
    # book only the finalists -> competition / small-acct share
    good = []
    for mid, pool, m in longdated[:15]:
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        if not toks:
            continue
        ms_ = float(m.get("rewardsMaxSpread") or 3) / 100.0
        b, a = scan.book(toks[0])
        if not b or not a:
            continue
        m2 = (b[0][0] + a[0][0]) / 2
        comp = sum(s for p, s in b if p >= m2 - ms_) + sum(s for p, s in a if p <= m2 + ms_)
        share = OURSH / (comp + OURSH) if (comp + OURSH) else 1.0
        est_daily = pool * share
        if share >= MINSHARE:
            good.append(dict(q=scan._ascii(m.get("question", ""))[:40], mid=mid, pool=pool,
                             comp=comp, share=share, est_daily=est_daily))
        time.sleep(0.12)
    return len(extreme), len(longdated), good


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    nextreme, nlong, good = scan_extreme()
    log_scan(nextreme, nlong, good)
    print(f"extreme-farm scan: {nextreme} extreme(>=.95/<=.05) -> {nlong} long-dated pool>=${MINPOOL:.0f} "
          f"-> {len(good)} EARNABLE (small-acct share>={MINSHARE:.0%})")
    if good:
        print("%5s %8s %9s %6s %9s  %s" % ("mid", "pool$", "comp", "share", "est$/d", "market"))
        for g in sorted(good, key=lambda x: -x["est_daily"]):
            print("%5.3f %8.0f %9.0f %6.2f %9.2f  %s" % (
                g["mid"], g["pool"], g["comp"], g["share"], g["est_daily"], g["q"]))
        print("\n*** EARNABLE extreme farm(s) live — 95/5 tweet would actually pay here. (VPN to post.) ***")
    else:
        print("no earnable extreme farm (saturated comp or short-dated toxic) — confirms 95/5 tweet dead for small accts.")
    if "--alert" in sys.argv and good:
        sys.exit(2)
