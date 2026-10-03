import math
import os
import random
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://api.upstox.com/v2"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
STRIKE_STEP = 50
IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="NIFTY OI Monitor", page_icon="📈", layout="wide")


# ============================================================
# API
# ============================================================
def api_get(path, token, params):
    r = requests.get(
        f"{BASE_URL}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )
    if r.status_code == 401:
        raise PermissionError("Upstox rejected the token (401). It expires daily (~3:30 AM), must be the ACCESS token (not API key/secret), and on Streamlit Cloud you must reboot the app after editing secrets. Also clear the sidebar token box. Upstox said: " + r.text[:200])
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(j)
    return j["data"]


@st.cache_data(ttl=3600, show_spinner=False)
def get_expiries(token):
    """Upcoming NIFTY expiries (YYYY-MM-DD), nearest first."""
    data = api_get("/option/contract", token, {"instrument_key": NIFTY_KEY})
    today = datetime.now(IST).date()
    out = set()
    for c in data:
        e = c.get("expiry")
        if isinstance(e, (int, float)):
            e = datetime.fromtimestamp(e / 1000, IST).date().isoformat()
        e = str(e)[:10]
        if date.fromisoformat(e) >= today:
            out.add(e)
    return sorted(out)


def get_chain(token, expiry):
    return api_get("/option/chain", token, {"instrument_key": NIFTY_KEY, "expiry_date": expiry})


# ============================================================
# NSE PUBLIC SOURCE (no token needed, best effort)
# ============================================================
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


@st.cache_resource(ttl=240, show_spinner=False)
def nse_session():
    """NSE needs cookies from its home page first; reuse the session for ~4 minutes."""
    sess = requests.Session()
    sess.headers.update(NSE_HEADERS)
    sess.get("https://www.nseindia.com", timeout=10)
    sess.get("https://www.nseindia.com/option-chain", timeout=10)
    return sess


def get_nse_records():
    url = "https://www.nseindia.com/api/option-chain-indices"
    for attempt in range(2):
        try:
            r = nse_session().get(url, params={"symbol": "NIFTY"}, timeout=10)
            r.raise_for_status()
            rec = r.json().get("records")
            if rec and rec.get("data"):
                return rec
        except Exception:
            pass
        nse_session.clear()  # drop stale cookies and retry once
    raise RuntimeError(
        "NSE did not return data. NSE often blocks cloud servers (like Streamlit Cloud) "
        "and returns nothing outside market hours. Run the app on your own PC "
        "(streamlit run app.py) or switch to Demo."
    )


def nse_expiries(rec):
    ex = rec.get("expiryDates", [])
    return sorted(ex, key=lambda e: datetime.strptime(e, "%d-%b-%Y"))


def nse_to_chain(rec, expiry):
    """Convert NSE JSON into the same shape the app uses for Upstox."""
    spot = rec.get("underlyingValue")
    out = []
    for d in rec["data"]:
        if d.get("expiryDate") != expiry:
            continue
        ce, pe = d.get("CE", {}), d.get("PE", {})
        spot = spot or ce.get("underlyingValue") or pe.get("underlyingValue")

        def md(x):
            oi = x.get("openInterest", 0) or 0
            return {"ltp": x.get("lastPrice", 0), "oi": oi, "prev_oi": oi - (x.get("changeinOpenInterest", 0) or 0)}

        out.append({
            "strike_price": float(d["strikePrice"]),
            "underlying_spot_price": spot,
            "call_options": {"market_data": md(ce), "option_greeks": {"iv": ce.get("impliedVolatility"), "delta": None}},
            "put_options": {"market_data": md(pe), "option_greeks": {"iv": pe.get("impliedVolatility"), "delta": None}},
        })
    return out


# ============================================================
# SAMPLE DATA (Demo mode, same shape as the Upstox response)
# ============================================================
def make_sample_chain():
    """Synthetic NIFTY chain. Spot random-walks and OI evolves on every refresh."""
    ss = st.session_state
    if "demo" not in ss:
        spot = 24850.0
        strikes = [24000 + 50 * i for i in range(36)]
        ce = {k: random.randint(60_000, 250_000) + (400_000 if k in (25000, 25200) else 0) for k in strikes}
        pe = {k: random.randint(60_000, 250_000) + (400_000 if k in (24700, 24500) else 0) for k in strikes}
        ss["demo"] = {
            "spot": spot, "drift": random.choice([-2, 2]), "ce": ce, "pe": pe,
            "prev_ce": {k: int(v * random.uniform(0.9, 1.1)) for k, v in ce.items()},
            "prev_pe": {k: int(v * random.uniform(0.9, 1.1)) for k, v in pe.items()},
        }
    d = ss["demo"]
    if random.random() < 0.08:
        d["drift"] = -d["drift"]
    d["spot"] += random.gauss(d["drift"], 5)
    for k in d["ce"]:
        # uptrend -> more put writing; downtrend -> more call writing
        d["pe"][k] += int(random.gauss(2500 if d["drift"] > 0 else 500, 1500))
        d["ce"][k] += int(random.gauss(2500 if d["drift"] < 0 else 500, 1500))
        d["pe"][k], d["ce"][k] = max(d["pe"][k], 0), max(d["ce"][k], 0)

    spot, chain = d["spot"], []
    for k in d["ce"]:
        tv = max(4.0, 110 * math.exp(-abs(k - spot) / 160))
        dc = min(max(0.5 + (spot - k) / 400, 0.02), 0.98)
        chain.append({
            "strike_price": float(k),
            "underlying_spot_price": spot,
            "call_options": {
                "market_data": {"ltp": round(max(spot - k, 0) + tv, 2), "oi": d["ce"][k], "prev_oi": d["prev_ce"][k]},
                "option_greeks": {"iv": round(random.uniform(12, 17), 2), "delta": round(dc, 3)},
            },
            "put_options": {
                "market_data": {"ltp": round(max(k - spot, 0) + tv, 2), "oi": d["pe"][k], "prev_oi": d["prev_pe"][k]},
                "option_greeks": {"iv": round(random.uniform(12, 17), 2), "delta": round(dc - 1, 3)},
            },
        })
    return chain


# ============================================================
# DATA HELPERS
# ============================================================
def build_df(chain):
    rows = []
    for it in chain:
        co, po = it.get("call_options", {}), it.get("put_options", {})
        cm, pm = co.get("market_data", {}), po.get("market_data", {})
        cg, pg = co.get("option_greeks", {}), po.get("option_greeks", {})
        rows.append(
            {
                "strike": it["strike_price"],
                "ce_ltp": cm.get("ltp") or 0,
                "ce_oi": cm.get("oi") or 0,
                "ce_day_doi": (cm.get("oi") or 0) - (cm.get("prev_oi") or 0),
                "ce_iv": cg.get("iv"),
                "ce_delta": cg.get("delta"),
                "pe_ltp": pm.get("ltp") or 0,
                "pe_oi": pm.get("oi") or 0,
                "pe_day_doi": (pm.get("oi") or 0) - (pm.get("prev_oi") or 0),
                "pe_iv": pg.get("iv"),
                "pe_delta": pg.get("delta"),
            }
        )
    return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def near_atm(df, spot, n):
    atm = df.iloc[(df["strike"] - spot).abs().argmin()]["strike"]
    i = df.index[df["strike"] == atm][0]
    return atm, df.iloc[max(0, i - n): i + n + 1].copy()


def window_change(history, strikes, window):
    """Change in CE/PE OI over the last `window` refreshes, on a fixed set of strikes."""
    if len(history) < 2:
        return None
    base = history[-window - 1] if len(history) > window else history[0]
    cur = history[-1]
    ce = pe = 0
    for s in strikes:
        c_now, p_now = cur["oi"].get(s, (0, 0))
        c_old, p_old = base["oi"].get(s, (c_now, p_now))
        ce += c_now - c_old
        pe += p_now - p_old
    return {"ce": ce, "pe": pe, "spot": cur["spot"] - base["spot"]}


# ============================================================
# SIGNAL ENGINE  (score -3 .. +3)
# ============================================================
def calculate_signal(chg, pcr, thr_points):
    if chg is None:
        return "COLLECTING DATA", 0, ["Need at least 2 refreshes to compute change."]

    score, why = 0, []

    if chg["spot"] > thr_points:
        score += 1
        why.append(f"Spot up {chg['spot']:+.1f} pts over the window")
    elif chg["spot"] < -thr_points:
        score -= 1
        why.append(f"Spot down {chg['spot']:+.1f} pts over the window")
    else:
        why.append(f"Spot flat ({chg['spot']:+.1f} pts)")

    activity = abs(chg["ce"]) + abs(chg["pe"])
    if activity > 0:
        ratio = (chg["pe"] - chg["ce"]) / activity
        if ratio > 0.2:
            score += 1
            why.append("Put OI building faster than Call OI (put writing = support)")
        elif ratio < -0.2:
            score -= 1
            why.append("Call OI building faster than Put OI (call writing = resistance)")
        else:
            why.append("Call and Put OI changes are balanced")

    if pcr > 1.2:
        score += 1
        why.append(f"PCR {pcr:.2f} (> 1.2, supportive)")
    elif pcr < 0.8:
        score -= 1
        why.append(f"PCR {pcr:.2f} (< 0.8, weak)")
    else:
        why.append(f"PCR {pcr:.2f} (neutral)")

    label = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    return label, score, why


# ============================================================
# STRIKE IDEAS
# ============================================================
def strike_ideas(label, atm, spot, df, sl_pct, tgt_pct):
    atm_row = df[df["strike"] == atm].iloc[0]
    below = df[df["strike"] <= spot]
    above = df[df["strike"] >= spot]
    support = below.loc[below["pe_oi"].idxmax(), "strike"] if len(below) else atm
    resist = above.loc[above["ce_oi"].idxmax(), "strike"] if len(above) else atm

    ideas = []
    if label == "BULLISH":
        p = atm_row["ce_ltp"]
        ideas.append(("BUY", f"{atm:.0f} CE", p, p * (1 - sl_pct / 100), p * (1 + tgt_pct / 100),
                      "ATM call for a directional bullish view."))
        sp = df[df["strike"] == support].iloc[0]["pe_ltp"]
        ideas.append(("SELL", f"{support:.0f} PE", sp, None, None,
                      f"Put at highest-OI support. Short options carry large risk; use a hedge/stop."))
    elif label == "BEARISH":
        p = atm_row["pe_ltp"]
        ideas.append(("BUY", f"{atm:.0f} PE", p, p * (1 - sl_pct / 100), p * (1 + tgt_pct / 100),
                      "ATM put for a directional bearish view."))
        sp = df[df["strike"] == resist].iloc[0]["ce_ltp"]
        ideas.append(("SELL", f"{resist:.0f} CE", sp, None, None,
                      f"Call at highest-OI resistance. Short options carry large risk; use a hedge/stop."))
    return ideas, support, resist


# ============================================================
# SIDEBAR
# ============================================================
st.sidebar.header("Settings")

try:
    default_token = st.secrets.get("UPSTOX_ACCESS_TOKEN", "")
except Exception:
    default_token = ""
default_token = default_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")

source = st.sidebar.radio("Data source", ["NSE (no token)", "Upstox (token)", "Demo (sample)"], index=0)
demo = source.startswith("Demo")
nse = source.startswith("NSE")
upstox = source.startswith("Upstox")
token = st.sidebar.text_input("Upstox access token", value=default_token, type="password", disabled=not upstox)
# clean common paste mistakes: spaces, quotes, "Bearer " prefix
token = (token or "").strip().strip("\"'").strip()
if token.lower().startswith("bearer "):
    token = token[7:].strip()
if token and upstox:
    src = "sidebar box" if token != default_token.strip().strip("\"'") else "secrets/env"
    st.sidebar.caption(f"Using token from {src}: length {len(token)}, ends with …{token[-4:]}")
    if len(token) < 100:
        st.sidebar.warning("Upstox access tokens are long (200+ chars, JWT starting with 'eyJ'). This looks like an API key/secret instead.")
refresh_s = st.sidebar.slider("Refresh every (sec)", 3, 60, 5)
n_side = st.sidebar.slider("Strikes each side of ATM", 2, 15, 5)
window = st.sidebar.slider("Signal window (refreshes)", 2, 60, 12)
thr_pts = st.sidebar.number_input("Spot move threshold (pts)", 1.0, 100.0, 10.0)
sl_pct = st.sidebar.number_input("Stop-loss on bought premium (%)", 5, 90, 25)
tgt_pct = st.sidebar.number_input("Target on bought premium (%)", 5, 300, 50)

if st.sidebar.button("Reset history"):
    st.session_state.pop("history", None)
    st.session_state.pop("demo", None)

# ============================================================
# MAIN
# ============================================================
st.title("📈 NIFTY 50 Live Price + Option OI Monitor")

if upstox and not token:
    st.info("Enter your Upstox access token, or choose NSE / Demo as the data source.")
    st.stop()

st_autorefresh(interval=refresh_s * 1000, key="auto")

try:
    if demo:
        st.info("🧪 Demo mode: synthetic sample data, not real market prices.")
        expiry = st.sidebar.selectbox("Expiry", ["DEMO"], index=0)
        chain = make_sample_chain()
    elif nse:
        rec = get_nse_records()
        expiries = nse_expiries(rec)
        expiry = st.sidebar.selectbox("Expiry", expiries, index=0)
        chain = nse_to_chain(rec, expiry)
    else:
        expiries = get_expiries(token)
        if not expiries:
            st.error("No upcoming NIFTY expiries returned.")
            st.stop()
        expiry = st.sidebar.selectbox("Expiry", expiries, index=0)
        chain = get_chain(token, expiry)
except Exception as e:
    st.error(f"Data error: {e}")
    st.stop()

if not chain:
    st.warning("No option-chain data returned.")
    st.stop()

now = datetime.now(IST)
open_now = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
if not open_now and not demo:
    st.warning("Market appears closed (NSE: Mon-Fri 9:15-15:30 IST). Data may be stale.")

spot = chain[0]["underlying_spot_price"]
df = build_df(chain)
atm, view = near_atm(df, spot, n_side)

# --- history (kept per browser session) ---
hist = st.session_state.setdefault("history", [])
if not hist or (now - hist[-1]["t"]).total_seconds() >= refresh_s * 0.8:
    hist.append({"t": now, "spot": spot,
                 "oi": {r.strike: (r.ce_oi, r.pe_oi) for r in df.itertuples()}})
    del hist[:-300]

chg = window_change(hist, list(view["strike"]), window)
pcr = df["pe_oi"].sum() / df["ce_oi"].sum() if df["ce_oi"].sum() else 0
label, score, reasons = calculate_signal(chg, pcr, thr_pts)
ideas, support, resist = strike_ideas(label, atm, spot, df, sl_pct, tgt_pct)

# --- header metrics ---
prev_spot = hist[-2]["spot"] if len(hist) > 1 else spot
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("NIFTY", f"{spot:,.2f}", f"{spot - prev_spot:+.2f}")
c2.metric("ATM strike", f"{atm:.0f}")
c3.metric("PCR (full chain)", f"{pcr:.2f}")
c4.metric("Support (max PE OI)", f"{support:.0f}")
c5.metric("Resistance (max CE OI)", f"{resist:.0f}")
st.caption(f"Expiry {expiry}  |  Last update {now.strftime('%H:%M:%S')} IST  |  Snapshots: {len(hist)}")

# --- signal ---
icon = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(label, "🟡")
st.subheader(f"{icon} Signal: {label}  (score {score:+d})")
if chg:
    m1, m2, m3 = st.columns(3)
    m1.metric("CE ΔOI (window)", f"{chg['ce']:+,.0f}")
    m2.metric("PE ΔOI (window)", f"{chg['pe']:+,.0f}")
    m3.metric("Spot move (window)", f"{chg['spot']:+.1f}")
for r in reasons:
    st.write(f"- {r}")

# --- strike ideas ---
st.subheader("🎯 Strike ideas")
if not ideas:
    st.info("No directional edge right now. Staying out is a valid position.")
else:
    for side, strike, prem, sl, tgt, note in ideas:
        with st.container(border=True):
            st.markdown(f"**{side} {strike}**  @ ~₹{prem:,.2f}")
            if sl is not None:
                st.write(f"Stop-loss ≈ ₹{sl:,.2f}  |  Target ≈ ₹{tgt:,.2f}")
            st.caption(note)
st.caption("⚠️ Rule-based idea from OI data only, not a recommendation. Test on paper first; options can expire worthless.")

# --- chain table ---
st.subheader("Option chain (near ATM)")
table = view.rename(columns={
    "strike": "Strike", "ce_ltp": "CE LTP", "ce_oi": "CE OI", "ce_day_doi": "CE Day ΔOI",
    "pe_ltp": "PE LTP", "pe_oi": "PE OI", "pe_day_doi": "PE Day ΔOI",
    "ce_iv": "CE IV", "pe_iv": "PE IV",
})[["Strike", "CE LTP", "CE OI", "CE Day ΔOI", "CE IV", "PE LTP", "PE OI", "PE Day ΔOI", "PE IV"]]

st.dataframe(
    table.style.apply(
        lambda r: ["background-color: rgba(255,200,0,0.25)" if r["Strike"] == atm else "" for _ in r],
        axis=1,
    ).format({"Strike": "{:.0f}", "CE LTP": "{:.2f}", "PE LTP": "{:.2f}",
              "CE OI": "{:,.0f}", "PE OI": "{:,.0f}",
              "CE Day ΔOI": "{:+,.0f}", "PE Day ΔOI": "{:+,.0f}",
              "CE IV": "{:.1f}", "PE IV": "{:.1f}"}, na_rep="-"),
    use_container_width=True,
    hide_index=True,
)

st.subheader("OI by strike")
st.bar_chart(view.set_index("strike")[["ce_oi", "pe_oi"]])
