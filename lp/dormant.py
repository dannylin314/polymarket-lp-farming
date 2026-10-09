"""Dormant stable-farm finder — the ONLY profitable LP configuration.

Research (9/4-9/7) showed LP farming only nets meaningfully positive on DORMANT,
low-competition, long-dated markets: the price is stable (no adverse selection) AND
few other makers are present (you capture most/all of the reward pool). Those windows
are RARE and TRANSIENT — a niche market (a governor race, a "will X insult Y") goes
quiet for hours/days, then wakes up. This scanner catches them when they appear.

Pipeline is throttle-safe (the CLOB /book endpoint rate-limits bursts):
  1. gamma  : incentivized, non-toxic, long-dated (>=DAYS), pool>=MINPOOL   (no book)
  2. trades : data-api trade count last 48h; DORMANT = <=MAXTRADES           (no book, not throttled)
  3. book   : only the dormant finalists -> confirm comp<=MAXCOMP, our share (few book calls)

  python dormant.py         - scan and print any dormant low-comp farms live now
  python dormant.py --alert  - same, but exit code 2 if a GOOD farm is found (for loops)

Env: DORM_DAYS(20) DORM_MINPOOL(30) DORM_MAXTRADES(8) DORM_MAXCOMP(60) DORM_CAP(40)
Paper/discovery only; posting needs a VPN (UK /order geoblock).
"""
import os, sys, json, time, ssl, urllib.request, datetime, sqlite3
from pathlib import Path
import scan

LOGDB = Path(__file__).resolve().parent / "dormant_log.db"


def log_scan(ncand, ndorm, good):
    con = sqlite3.connect(LOGDB)
    con.execute("CREATE TABLE IF NOT EXISTS scan (ts INTEGER, ncand INT, ndorm INT, ngood INT)")
    con.execute("CREATE TABLE IF NOT EXISTS goodfarm (ts INTEGER, q TEXT, pool REAL, trades INT, comp REAL, est_daily REAL)")
    now = int(time.time())
    con.execute("INSERT INTO scan VALUES (?,?,?,?)", (now, ncand, ndorm, len(good)))
    for g in good:
        con.execute("INSERT INTO goodfarm VALUES (?,?,?,?,?,?)", (now, g["q"], g["pool"], g["trades"], g["comp"], g["est_daily"]))
    con.commit(); con.close()

_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
EXCLUDE = ("temperature", "bitcoin", "ethereum", "up or down", "higher or lower", "solana", " xrp ", "dogecoin")

DAYS      = float(os.environ.get("DORM_DAYS", "20") or 20)
MINPOOL   = float(os.environ.get("DORM_MINPOOL", "30") or 30)
MAXTRADES = int(os.environ.get("DORM_MAXTRADES", "8") or 8)
MAXCOMP   = float(os.environ.get("DORM_MAXCOMP", "60") or 60)
OUR       = float(os.environ.get("DORM_CAP", "40") or 40)   # shares/side we'd rest


def ntrades_48h(cond):
    try:
        d = json.load(urllib.request.urlopen(
            urllib.request.Request(f"https://data-api.polymarket.com/trades?market={cond}&limit=500",
                                   headers=UA), context=_ctx, timeout=15))
    except Exception:
        return -1
    if not isinstance(d, list):
        return -1
    cut = time.time() - 48 * 3600
    return sum(1 for t in d if (t.get("timestamp") or 0) > cut)


def scan_dormant():
    now = datetime.datetime.now(datetime.timezone.utc)
    # 1. gamma candidates (no book)
    cands = []
    for m in scan.fetch_incentivized():
        ql = scan._ascii(m.get("question", "")).lower()
        if any(x in ql for x in EXCLUDE):
            continue
        pool = scan.reward_rate(m)
        if pool < MINPOOL:
            continue
        try:
            end = datetime.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
            if (end - now).days < DAYS:
                continue
        except Exception:
            continue
        cands.append((pool, m))
    cands.sort(key=lambda r: -r[0])
    # 2. dormancy via trades (no throttle)
    dormant = []
    for pool, m in cands[:45]:
        cond = m.get("conditionId")
        if not cond:
            continue
        nt = ntrades_48h(cond)
        if 0 <= nt <= MAXTRADES:
            dormant.append((pool, nt, m))
        time.sleep(0.04)
    # 3. book only dormant finalists -> comp / share
    good = []
    for pool, nt, m in dormant[:15]:
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        if not toks:
            continue
        ms = float(m.get("rewardsMaxSpread") or 3) / 100.0
        b, a = scan.book(toks[0])
        if not b or not a:
            continue
        mid = (b[0][0] + a[0][0]) / 2
        comp = sum(s for p, s in b if p >= mid - ms) + sum(s for p, s in a if p <= mid + ms)
        if comp > MAXCOMP:
            continue
        share = (2 * OUR) / (comp + 2 * OUR) if (comp + 2 * OUR) else 1.0
        est_daily = pool * share
        good.append(dict(q=scan._ascii(m.get("question", ""))[:44], pool=pool, trades=nt,
                         comp=comp, share=share, mid=mid, est_daily=est_daily))
        time.sleep(0.1)
    return len(cands), len(dormant), good


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ncand, ndorm, good = scan_dormant()
    log_scan(ncand, ndorm, good)
    print(f"dormant-farm scan: {ncand} candidates -> {ndorm} dormant(<= {MAXTRADES} trd/48h) -> {len(good)} GOOD (comp<= {MAXCOMP:.0f})")
    if good:
        print("%6s %5s %6s %5s %5s %8s  %s" % ("pool$", "trd", "comp", "shr", "mid", "est$/d", "market"))
        for g in sorted(good, key=lambda x: -x["est_daily"]):
            print("%6.0f %5d %6.0f %5.2f %5.2f %8.2f  %s" % (
                g["pool"], g["trades"], g["comp"], g["share"], g["mid"], g["est_daily"], g["q"]))
        print("\n*** GOOD dormant farm(s) live — the profitable LP window. (VPN to post.) ***")
    else:
        print("no dormant low-comp farm live now (expected — they are rare/transient).")
    if "--alert" in sys.argv and good:
        sys.exit(2)
