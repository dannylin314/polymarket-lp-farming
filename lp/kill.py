"""Kill switch — cancel ALL open orders for the LP-farm account, immediately.

  python kill.py           - cancel every open order on the account (client.cancel_all)
  python kill.py --ours     - cancel only orders farm.py recorded as 'placed' in pilot_lp.db

Uses the same POLY_* creds/VPN as farm.py. cancel_all nukes EVERY resting order on
the account, so run it on the dedicated pilot account only; --ours is the surgical
option that leaves any unrelated orders alone.
"""
import os, sys, sqlite3, argparse
from pathlib import Path

DB = Path(__file__).resolve().parent / "pilot_lp.db"


def build_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(
        api_key=os.environ.get("POLY_API_KEY", ""),
        api_secret=os.environ.get("POLY_API_SECRET", ""),
        api_passphrase=os.environ.get("POLY_API_PASSPHRASE", ""),
    )
    c = ClobClient(
        "https://clob.polymarket.com", chain_id=137,
        key=os.environ.get("POLY_PRIVATE_KEY", ""), creds=creds,
        signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "2")),
        funder=os.environ.get("POLY_FUNDER_ADDRESS", ""),
    )
    c.set_api_creds(creds)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", action="store_true", help="cancel only pilot_lp.db 'placed' orders")
    args = ap.parse_args()
    client = build_client()
    if args.ours:
        if not DB.exists():
            print("no pilot_lp.db — nothing to cancel"); return
        con = sqlite3.connect(DB)
        ids = [r[0] for r in con.execute(
            "SELECT poly_oid FROM orders WHERE status='placed' AND poly_oid IS NOT NULL").fetchall()]
        if not ids:
            print("no open 'placed' orders in pilot_lp.db"); con.close(); return
        resp = client.cancel_orders(ids)
        con.execute("UPDATE orders SET status='canceled' WHERE status='placed'")
        con.commit(); con.close()
        print(f"cancelled {len(ids)} tracked order(s): {resp}")
    else:
        resp = client.cancel_all()
        if DB.exists():
            con = sqlite3.connect(DB)
            con.execute("UPDATE orders SET status='canceled' WHERE status='placed'")
            con.commit(); con.close()
        print(f"cancel_all -> {resp}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
