# NIFTY 50 Live OI Monitor (Streamlit + Upstox)

Live NIFTY spot, option-chain OI, change in OI, a bullish/bearish/neutral signal,
and an **option strike idea** (buy CE/PE at ATM, or sell PE/CE at OI support/resistance).

> Educational tool only. Not financial advice. Option trading, especially selling, can lose more than you put in.

## Demo mode
Tick **Demo mode** in the sidebar (on by default when no token is set) to see the app with synthetic sample data. No token needed, works when the market is closed. The signal needs a few refreshes before it appears.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Paste your Upstox access token in the sidebar (or set `UPSTOX_ACCESS_TOKEN`).
Upstox tokens expire every day, so you need a fresh one each morning.

## GitHub + Streamlit Community Cloud
1. Create a GitHub repo and push these files:
   ```bash
   git init && git add . && git commit -m "nifty oi monitor"
   git branch -M main
   git remote add origin https://github.com/<you>/nifty-oi-monitor.git
   git push -u origin main
   ```
2. Go to https://share.streamlit.io -> **New app** -> pick the repo, branch `main`, file `app.py`.
3. App -> **Settings -> Secrets**, add: `UPSTOX_ACCESS_TOKEN = "your-token"` (or just paste it in the sidebar each day).
4. Never commit your token. `.gitignore` already excludes `secrets.toml`.
