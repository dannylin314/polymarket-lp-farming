"""Data-driven reward-market analysis. Pull ALL incentivized markets and characterize them across
pool / max-spread / min-size / competitiveness / mid / liquidity / 24h volume / type / horizon —
to choose farm filters empirically. Read-only (gamma + data-api). Run: python analyze.py"""
import sys, datetime
from collections import defaultdict
import scan

CATS = [
    ("crypto/index", ("bitcoin", "ethereum", "solana", " btc", " eth", " xrp", "dogecoin", "index",
                      "up or down", "higher or lower")),
    ("weather", ("temperature", "precipitation", "inches of", "rainfall", "snow", "degrees", "weather")),
    ("esports", ("esports", "hitpoint", "demacia", "league of legends", " lol ", "valorant", " dota",
                 "counter-strike", "csgo", "overwatch", "riot", " worlds")),
    ("sports", ("head coach", "rookie of the year", "spread:", "o/u", "over/under", "moneyline",
                "relegat", " vs ", " nba", " nfl", " mlb", " nhl", " ufc", "d1 ", "ncaa", "heisman",
                "rushing", "touchdown", "premier league", "champions league", "world cup", "super bowl")),
    ("entertainment", ("dancing with the stars", "dwts", "oscar", "grammy", "emmy", "eurovision",
                       "bachelor", "survivor", " award", "box office")),
    ("ai/tech", ("ipo", "release", "announce", "debut", "launch", " model ", "gpt", "claude", "gemini",
                 "grok", "llama", "openai", "anthropic")),
    ("macro/rates", ("interest rate", "fed ", "rate hike", "rate cut", "bps", "central bank",
                     "reserve bank", "ecb", "boj", "inflation", " cpi", " gdp", "treasury yield")),
    ("politics/geo", ("president", "election", " war", "ceasefire", "invade", "enter ", "diplomatic",
                      "sanction", " vote", "senate", "minister", "governor", "coup", "nuclear", "prime minister")),
]

def classify(q):
    ql = q.lower()
    for name, kws in CATS:
        if any(k in ql for k in kws):
            return name
    return "other"

def med(xs):
    xs = sorted(xs); return xs[len(xs) // 2] if xs else 0

def main():
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    mk = scan.fetch_incentivized()
    rows = []
    for m in mk:
        q = scan._ascii(m.get("question", ""))
        def f(*keys):
            for k in keys:
                try:
                    v = float(m.get(k));
                    if v: return v
                except Exception: pass
            return 0.0
        bb, ba = f("bestBid"), f("bestAsk")
        mid = (bb + ba) / 2 if (bb and ba) else 0.0
        try:
            end = datetime.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
            days = (end - datetime.datetime.now(datetime.timezone.utc)).days
        except Exception:
            days = None
        rows.append(dict(q=q, cat=classify(q), pool=scan.reward_rate(m),
                         maxspr=f("rewardsMaxSpread"), minsz=f("rewardsMinSize"),
                         comp=f("competitive"), liq=f("liquidityNum", "liquidity"),
                         vol=f("volume24hrClob", "volume24hr"), mid=mid, days=days))
    n = len(rows)
    print(f"=== {n} incentivized markets ===\n")

    cats = defaultdict(list)
    for r in rows: cats[r["cat"]].append(r)
    print("%-14s %4s %7s %6s %6s %6s %6s %8s" % ("category", "n", "avgPool", "avgCmp", "avgMin", "%bal", "avgDay", "medLiq"))
    print("-" * 66)
    for c, rs in sorted(cats.items(), key=lambda x: -sum(r["pool"] for r in x[1]) / len(x[1])):
        withmid = [r for r in rs if r["mid"] > 0]
        bal = (sum(1 for r in withmid if 0.15 <= r["mid"] <= 0.85) / len(withmid) * 100) if withmid else 0
        dd = [r["days"] for r in rs if r["days"] is not None]
        print("%-14s %4d %7.0f %6.2f %6.0f %5.0f%% %6s %8.0f" % (
            c, len(rs), sum(r["pool"] for r in rs) / len(rs), sum(r["comp"] for r in rs) / len(rs),
            sum(r["minsz"] for r in rs) / len(rs), bal,
            ("%.0f" % (sum(dd) / len(dd))) if dd else "-", med([r["liq"] for r in rs])))

    print("\nmin-shares distribution (how many markets require each min size to earn):")
    ms = defaultdict(int)
    for r in rows: ms[int(r["minsz"])] += 1
    for k in sorted(ms): print("  min %4d shares: %3d markets" % (k, ms[k]))

    print("\ncompetitive-score distribution:")
    cb = defaultdict(int)
    for r in rows: cb[round(r["comp"], 1)] += 1
    for k in sorted(cb): print("  competitive ~%.1f: %3d markets" % (k, cb[k]))

    # GOOD-FARM candidates at each size: balanced + low-adverse type + real pool + qualifiable
    good_types = ("macro/rates", "politics/geo")
    for size in (20, 50, 100, 200):
        g = [r for r in rows if r["minsz"] <= size and 0.15 <= r["mid"] <= 0.85
             and r["cat"] in good_types and r["pool"] >= 40 and r["liq"] >= 500]
        print(f"\nFARMABLE @ size {size} (balanced 0.15-0.85, macro/political, pool>=40, liq>=500, minshares<={size}): {len(g)} markets")
        for r in sorted(g, key=lambda r: -r["pool"])[:10]:
            print("  pool%5.0f comp%.2f min%3.0f mid%.2f liq%7.0f %4sd  %s" % (
                r["pool"], r["comp"], r["minsz"], r["mid"], r["liq"], r["days"], r["q"][:38]))

if __name__ == "__main__":
    main()
