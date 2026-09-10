import requests, time, json, os, smtplib
import pandas as pd
from email.mime.text import MIMEText
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = "monitor_wallets.txt"
LAST_SEEN_FILE = "state/last_seen_time.json"
LEDGER_FILE = "state/paper_ledger.csv"

VIRTUAL_ALLOCATION_PER_WALLET = 1000
MAX_ALERTS_PER_RUN = 30  # safety circuit breaker - if we'd exceed this, something is wrong, don't spam

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

def load_last_seen():
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE) as f:
            return json.load(f)
    return {}

def save_last_seen(d):
    os.makedirs("state", exist_ok=True)
    with open(LAST_SEEN_FILE, "w") as f:
        json.dump(d, f)

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
    last_seen = load_last_seen()
    is_bootstrap = len(last_seen) == 0
    new_alerts = []
    total_new_fills = 0

    for addr in wallets:
        fills = get_fills(addr)
        if not fills:
            continue

        df = pd.DataFrame(fills)
        df["time"] = pd.to_numeric(df.get("time", 0), errors="coerce")
        max_time_this_call = int(df["time"].max())

        if is_bootstrap:
            # record the latest timestamp we've seen, alert on nothing yet
            last_seen[addr] = max_time_this_call
            continue

        cutoff = last_seen.get(addr, max_time_this_call)
        new_df = df[df["time"] > cutoff]

        if new_df.empty:
            continue

        total_new_fills += len(new_df)

        for _, f in new_df.iterrows():
            coin = f.get("coin", "")
            direction = f.get("dir", "")
            px = f.get("px", "")
            sz = f.get("sz", "")
            closed_pnl = float(f.get("closedPnl", 0) or 0)
            ts = datetime.fromtimestamp(f.get("time", 0)/1000, tz=timezone.utc)

            new_alerts.append(
                f"Wallet: {addr}\nCoin: {coin}\nAction: {direction}\n"
                f"Price: {px}  Size: {sz}\nClosed PnL (real): {closed_pnl}\nTime: {ts.isoformat()}\n"
            )

            notional = float(px or 0) * float(sz or 0)
            scale = VIRTUAL_ALLOCATION_PER_WALLET / notional if notional else 0
            append_ledger({
                "wallet": addr, "coin": coin, "direction": direction,
                "price": px, "real_size": sz, "real_closed_pnl": closed_pnl,
                "paper_scale_factor": round(scale, 6),
                "paper_closed_pnl_estimate": round(closed_pnl * scale, 2) if closed_pnl else 0,
                "time": ts.isoformat()
            })

        last_seen[addr] = max_time_this_call
        time.sleep(0.2)

    save_last_seen(last_seen)

    if is_bootstrap:
        print(f"Bootstrap complete for {len(wallets)} wallets. No alerts sent. Future runs will alert on genuinely new activity only.")
        return

    if total_new_fills == 0:
        print("No new fills this run.")
        return

    if total_new_fills > MAX_ALERTS_PER_RUN:
        # circuit breaker: something is off (huge backlog, or a bug) - send ONE
        # summary email instead of flooding, and don't touch the ledger further this run
        send_email(
            f"[Wallet Monitor] WARNING: {total_new_fills} new fills detected in one run",
            f"This is way more than expected ({MAX_ALERTS_PER_RUN} is the normal cap) - "
            f"likely a tracking issue rather than real trading activity. "
            f"Check state/last_seen_time.json before trusting further alerts."
        )
        print(f"Circuit breaker triggered - {total_new_fills} fills, sent warning only, not full digest.")
        return

    subject = f"[Wallet Monitor] {len(new_alerts)} new trade(s) detected"
    send_email(subject, "\n---\n".join(new_alerts))
    print(f"{len(new_alerts)} new fills processed and alerted.")

if __name__ == "__main__":
    main()
            
