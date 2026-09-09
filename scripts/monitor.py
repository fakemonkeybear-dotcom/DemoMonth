import requests, time, json, os, smtplib
import pandas as pd
from email.mime.text import MIMEText
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = "monitor_wallets.txt"
SEEN_FILE = "state/seen_fills.json"
LEDGER_FILE = "state/paper_ledger.csv"

VIRTUAL_ALLOCATION_PER_WALLET = 1000  # paper-trade $1000 "following" each wallet, adjust as you like

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_TO_EMAIL = os.environ.get("ALERT_TO_EMAIL")

def hl_post(body, retries=3):
    for i in range(retries):
        try:
            r = requests.post(API_URL, json=body, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(1*(i+1))
    return None

def get_fills(addr):
    return hl_post({"type": "userFills", "user": addr, "aggregateByTime": False}) or []

def load_wallets():
    with open(WALLETS_FILE) as f:
        return [l.strip() for l in f if l.strip()]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}

def save_seen(seen):
    os.makedirs("state", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)

def send_email(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and ALERT_TO_EMAIL):
        print("Email credentials missing - skipping send.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO_EMAIL
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_TO_EMAIL, msg.as_string())
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")

def append_ledger(row):
    os.makedirs("state", exist_ok=True)
    exists = os.path.exists(LEDGER_FILE)
    pd.DataFrame([row]).to_csv(LEDGER_FILE, mode="a", header=not exists, index=False)

def main():
    wallets = load_wallets()
    seen = load_seen()
    new_alerts = []

    for addr in wallets:
        fills = get_fills(addr)
        if not fills:
            continue

        seen_ids = set(seen.get(addr, []))
        # hyperliquid fills have a unique 'tid' (trade id) or 'hash' - use tid
        new_fills = [f for f in fills if str(f.get("tid", f.get("hash",""))) not in seen_ids]

        if not new_fills:
            continue

        for f in new_fills:
            coin = f.get("coin", "")
            side = "BUY/LONG" if f.get("side") == "B" else "SELL/SHORT"
            direction = f.get("dir", "")
            px = f.get("px", "")
            sz = f.get("sz", "")
            closed_pnl = float(f.get("closedPnl", 0) or 0)
            ts = datetime.fromtimestamp(f.get("time", 0)/1000, tz=timezone.utc)

            alert_text = (
                f"Wallet: {addr}\n"
                f"Coin: {coin}\n"
                f"Action: {direction} ({side})\n"
                f"Price: {px}  Size: {sz}\n"
                f"Closed PnL (real): {closed_pnl}\n"
                f"Time: {ts.isoformat()}\n"
            )
            new_alerts.append(alert_text)

            # paper ledger entry
            notional = float(px or 0) * float(sz or 0)
            scale = VIRTUAL_ALLOCATION_PER_WALLET / notional if notional else 0
            append_ledger({
                "wallet": addr, "coin": coin, "direction": direction,
                "price": px, "real_size": sz, "real_closed_pnl": closed_pnl,
                "paper_scale_factor": round(scale, 6),
                "paper_closed_pnl_estimate": round(closed_pnl * scale, 2) if closed_pnl else 0,
                "time": ts.isoformat()
            })

        seen[addr] = list(seen_ids.union(str(f.get("tid", f.get("hash",""))) for f in new_fills))
        time.sleep(0.2)

    save_seen(seen)

    if new_alerts:
        subject = f"[Wallet Monitor] {len(new_alerts)} new trade(s) detected"
        body = "\n---\n".join(new_alerts)
        send_email(subject, body)
        print(f"{len(new_alerts)} new fills processed and alerted.")
    else:
        print("No new fills this run.")

if __name__ == "__main__":
    main()
  
