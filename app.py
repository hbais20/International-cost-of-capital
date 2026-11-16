"""
International Cost of Capital App
=================================
Compares a project's cost of equity, cost of debt and WACC in USD and in the
currency of a selected country.

Data (fetched live, with a refresh button and manual overrides):
  * Unlevered industry betas ........ Damodaran (NYU Stern) betas.xls
  * USD BBB borrowing spread ........ Damodaran ratings page (Baa2/BBB, large non-financial firms)
  * Corporate tax rates ............. OECD statutory corporate income tax rates
  * 10-year government bond yields .. Trading Economics

Currency conversion uses interest-rate parity based on the two 10-year
government yields:  (1 + r_FC) = (1 + r_USD) * (1 + y_FC) / (1 + y_US)
"""

import re
import unicodedata
from datetime import datetime, timezone
from io import BytesIO, StringIO

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
BETA_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
    "full-report/statutory-corporate-income-tax-rates_440f9286.html"
)
TE_URL = "https://tradingeconomics.com/bonds"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"
DEFAULT_ERP = 6.5      # percent
DEFAULT_DEBT = 30      # percent

# Placeholders used ONLY if a live fetch fails and the user has not typed a value.
# They are illustrative, not current market data.
PLACEHOLDER = {"beta": 1.00, "tax": 25.0, "spread": 1.50, "yield": 4.00}
US_TAX_OECD = 25.77  # OECD combined (federal + state) statutory rate, entered manually if fetch fails

ALIASES = {
    "south korea": "korea", "republic of korea": "korea",
    "czech republic": "czechia", "slovak republic": "slovakia",
    "turkey": "turkiye", "russian federation": "russia",
    "united states of america": "united states", "usa": "united states",
    "us": "united states", "uk": "united kingdom",
    "china peoples republic of": "china", "peoples republic of china": "china",
    "hong kong china": "hong kong", "viet nam": "vietnam",
}

EUR = ["austria", "belgium", "croatia", "cyprus", "estonia", "finland", "france",
       "germany", "greece", "ireland", "italy", "latvia", "lithuania",
       "luxembourg", "malta", "netherlands", "portugal", "slovakia", "slovenia", "spain"]
CURRENCY = {c: "EUR" for c in EUR}
CURRENCY.update({
    "united states": "USD", "united kingdom": "GBP", "japan": "JPY", "canada": "CAD",
    "australia": "AUD", "new zealand": "NZD", "switzerland": "CHF", "sweden": "SEK",
    "norway": "NOK", "denmark": "DKK", "poland": "PLN", "czechia": "CZK",
    "hungary": "HUF", "turkiye": "TRY", "israel": "ILS", "korea": "KRW",
    "china": "CNY", "india": "INR", "brazil": "BRL", "mexico": "MXN",
    "chile": "CLP", "colombia": "COP", "south africa": "ZAR", "indonesia": "IDR",
    "thailand": "THB", "malaysia": "MYR", "singapore": "SGD", "philippines": "PHP",
    "vietnam": "VND", "iceland": "ISK", "romania": "RON", "bulgaria": "BGN",
    "argentina": "ARS", "peru": "PEN", "russia": "RUB", "hong kong": "HKD",
    "taiwan": "TWD", "saudi arabia": "SAR", "egypt": "EGP", "nigeria": "NGN",
})


SLUG_OVERRIDES = {"korea": "south-korea", "turkiye": "turkey", "czechia": "czech-republic",
                  "russia": "russia", "hong kong": "hong-kong"}


def slug_for(key: str) -> str:
    return SLUG_OVERRIDES.get(key, key.replace(" ", "-"))


def norm_country(name: str) -> str:
    """Normalise country names so OECD and Trading Economics names can be matched."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def http_get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# Core finance (pure functions, no Streamlit) -- these are what we verify
# --------------------------------------------------------------------------
def compute_capital_costs(beta_u, tax, erp, debt_share, rf_us, rf_fc,
                          spread_usd, spread_fc=None):
    """All rates are decimals (0.05 = 5%). Returns a dict of results.

    USD side
        D/E        = d / (1 - d)
        beta_L     = beta_U * (1 + (1 - t) * D/E)
        Ke_USD     = rf_US + beta_L * ERP
        Kd_USD     = rf_US + spread_USD
        WACC_USD   = (1 - d) * Ke_USD + d * Kd_USD * (1 - t)

    Foreign-currency side (same beta, t, d, ERP; converted by yield parity)
        g          = (1 + y_FC) / (1 + y_US)
        Ke_FC      = (1 + Ke_USD) * g - 1
        Kd_FC      = (1 + Kd_USD) * g - 1                (auto spread)
                   = y_FC + spread_FC                    (manual spread override)
        WACC_FC    = (1 - d) * Ke_FC + d * Kd_FC * (1 - t)
    """
    if not 0 <= debt_share < 1:
        raise ValueError("Debt share must be in [0, 1).")
    de = debt_share / (1 - debt_share)
    beta_l = beta_u * (1 + (1 - tax) * de)

    ke_usd = rf_us + beta_l * erp
    kd_usd = rf_us + spread_usd
    wacc_usd = (1 - debt_share) * ke_usd + debt_share * kd_usd * (1 - tax)

    g = (1 + rf_fc) / (1 + rf_us)
    ke_fc = (1 + ke_usd) * g - 1
    kd_fc_auto = (1 + kd_usd) * g - 1
    spread_fc_auto = kd_fc_auto - rf_fc
    kd_fc = kd_fc_auto if spread_fc is None else rf_fc + spread_fc
    wacc_fc = (1 - debt_share) * ke_fc + debt_share * kd_fc * (1 - tax)

    r12 = lambda d_: {k: round(v, 12) for k, v in d_.items()}  # noqa: E731
    return {
        "de_ratio": de, "beta_levered": beta_l, "parity_factor": g,
        "usd": r12({"rf": rf_us, "ke": ke_usd, "kd_pre": kd_usd,
                    "kd_post": kd_usd * (1 - tax), "wacc": wacc_usd,
                    "spread": spread_usd}),
        "fc": r12({"rf": rf_fc, "ke": ke_fc, "kd_pre": kd_fc,
                   "kd_post": kd_fc * (1 - tax), "wacc": wacc_fc,
                   "spread": kd_fc - rf_fc}),
        "spread_fc_auto": spread_fc_auto,
    }


# --------------------------------------------------------------------------
# Data loaders (cached; they never raise -- they return ok/error dicts)
# --------------------------------------------------------------------------
def _find_year_text(strings):
    for s in strings:
        if re.search(r"(19|20)\d{2}", s):
            return s.strip()
    return None


def parse_betas_workbook(content: bytes):
    """Parse the Damodaran betas.xls bytes -> (DataFrame, as_of_text)."""
    raw, last_err = None, None
    for engine in ("xlrd", None):
        try:
            raw = pd.read_excel(BytesIO(content), sheet_name=None, header=None, engine=engine)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if raw is None:
        raise last_err

    for sheet in raw.values():
        for i in range(min(40, len(sheet))):
            row = sheet.iloc[i].astype(str).str.strip().str.lower().tolist()
            if "industry name" in row and "unlevered beta" in row:
                pre = [str(x) for x in sheet.iloc[:i].values.ravel() if isinstance(x, str)]
                asof = _find_year_text(pre)
                df = sheet.iloc[i + 1:].copy()
                df.columns = [str(c).strip() for c in sheet.iloc[i]]
                df = df.rename(columns={"Industry Name": "industry",
                                        "Unlevered beta": "beta_u"})
                keep = [c for c in ["industry", "beta_u", "Number of firms",
                                    "Beta", "D/E Ratio"] if c in df.columns]
                df = df[keep]
                df["beta_u"] = pd.to_numeric(df["beta_u"], errors="coerce")
                df["industry"] = df["industry"].astype(str).str.strip()
                df = df.dropna(subset=["beta_u"])
                df = df[~df["industry"].str.lower().str.startswith("total market")]
                df = df[df["industry"].str.lower() != "nan"]
                return df.reset_index(drop=True), asof
    raise ValueError("Could not find 'Industry Name' / 'Unlevered beta' columns.")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_betas():
    out = {"ok": False, "df": None, "asof": None, "error": None, "fetched_at": now_str()}
    try:
        r = http_get(BETA_URL)
        df, asof = parse_betas_workbook(r.content)
        out.update(ok=True, df=df,
                   asof=asof or r.headers.get("Last-Modified") or "not stated in file")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def parse_bbb_spread(html: str):
    """Baa2/BBB spread (%) for LARGE NON-FINANCIAL firms.
    The page puts the large-firm table first and the financial-firm table to its
    right in the same rows, so the FIRST 'Baa2/BBB <x>%' in the page text is the right one."""
    text = BeautifulSoup(html, "lxml").get_text(" ")
    m = re.search(r"Baa2/BBB\s+(-?\d+(?:\.\d+)?)\s*%", text)
    if not m:
        raise ValueError("Baa2/BBB row not found on ratings page.")
    return float(m.group(1))


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_ratings():
    out = {"ok": False, "spread": None, "asof": None, "error": None, "fetched_at": now_str()}
    try:
        r = http_get(RATINGS_URL)
        spread = parse_bbb_spread(r.text)
        text = BeautifulSoup(r.text, "lxml").get_text(" ")
        m = re.search(r"(?:Last [Uu]pdated|Date of analysis|[Dd]ata (?:used )?(?:is )?as of|Updated)"
                      r"[:\s]*([A-Za-z]+\.? ?\d{0,2},? ?\d{4})", text)
        out.update(ok=True, spread=spread,
                   asof=(m.group(1) if m else r.headers.get("Last-Modified") or "not stated on page"))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _to_pct(x):
    m = re.search(r"-?\d+(\.\d+)?", str(x).replace(",", ""))
    if not m:
        return None
    v = float(m.group())
    return v if 0 <= v <= 70 else None


def parse_oecd_tables(html: str):
    """Pick the table with a United States row; use the latest year column."""
    soup = BeautifulSoup(html, "lxml")
    best = None
    for tbl in soup.find_all("table"):
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:  # noqa: BLE001
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(p) for p in c if "Unnamed" not in str(p)).strip()
                          for c in df.columns]
        df.columns = [str(c) for c in df.columns]
        first = df.columns[0]
        names = df[first].astype(str).map(norm_country)
        if "united states" not in set(names):
            continue
        years = [(int(re.search(r"(19|20)\d{2}", c).group()), c)
                 for c in df.columns[1:] if re.search(r"(19|20)\d{2}", c)]
        if not years:
            continue
        cap = tbl.find("caption")
        title = cap.get_text(" ", strip=True) if cap else ""
        if not title:
            prev = tbl.find_previous(["h1", "h2", "h3", "h4", "p"])
            title = prev.get_text(" ", strip=True)[:140] if prev else ""
        score = 2 if re.search(r"combined|statutory", title, re.I) else 0
        years.sort(reverse=True)
        for yr, col in years:
            vals = df[col].map(_to_pct)
            if vals.notna().sum() >= 20:
                cand = (score, yr, title, df[first].astype(str).tolist(), vals.tolist())
                if best is None or cand[:2] > best[:2]:
                    best = cand
                break
    if best is None:
        raise ValueError("No OECD tax-rate table with a United States row was found.")
    _, yr, title, ctry, vals = best
    rates, display = {}, {}
    for c, v in zip(ctry, vals):
        if v is not None:
            k = norm_country(c)
            rates.setdefault(k, v)
            display.setdefault(k, re.sub(r"\s*\d+$", "", c).strip())
    return rates, display, yr, title


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_oecd_tax():
    out = {"ok": False, "rates": {}, "names": {}, "year": None, "title": None,
           "error": None, "fetched_at": now_str()}
    try:
        r = http_get(OECD_URL)
        rates, names, yr, title = parse_oecd_tables(r.text)
        out.update(ok=True, rates=rates, names=names, year=yr, title=title)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


TE_URLS = [TE_URL,
           "https://tradingeconomics.com/country-list/government-bond-yield",
           "https://tradingeconomics.com/country-list/bonds"]


def parse_te_yields(html: str):
    """Row-based parser: first alphabetic cell = country, first number after it = yield."""
    soup = BeautifulSoup(html, "lxml")
    result = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        ni = next((i for i, c in enumerate(cells)
                   if re.fullmatch(r"[A-Za-z][A-Za-z .'\u2019&-]{2,40}", c)), None)
        if ni is None:
            continue
        y, date = None, "n/a"
        for c in cells[ni + 1:]:
            if y is None and re.fullmatch(r"-?\d+(\.\d+)?\s*%?", c):
                y = float(c.replace("%", ""))
            elif re.fullmatch(r"[A-Z][a-z]{2}/\d{2}", c):
                date = c
        if y is not None and -1 < y < 80:
            result.setdefault(norm_country(cells[ni]), {"name": cells[ni], "yield": y, "date": date})
    if not result:
        raise ValueError("No yield rows found in page.")
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def load_country_yield(key: str):
    """Fallback when the bulk /bonds page has no rows: read one country's 10-yr page."""
    out = {"ok": False, "data": None, "error": None, "url": None, "fetched_at": now_str()}
    url = f"https://tradingeconomics.com/{slug_for(key)}/government-bond-yield"
    try:
        r = http_get(url)
        out["url"] = url
        out["data"] = parse_country_yield_page(r.text, key)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{url.split('tradingeconomics.com')[-1]} -> {type(e).__name__}: {e}"
    return out


def parse_country_yield_page(html: str, key: str):
    text = BeautifulSoup(html, "lxml").get_text(" ")
    m = re.search(r"Actual\s+Previous\s+Highest\s+Lowest\s+Dates\s+Unit\s+Frequency\s+(-?\d+(?:\.\d+)?)", text)
    if not m:
        m = re.search(r"10[- ]?Y(?:ear)?[^%]{0,120}?\bto\s+(-?\d+(?:\.\d+)?)\s*%", text, re.I)
    if not m:
        raise ValueError("10-year yield not found on page")
    d = re.search(r"\bon ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    return {"name": key.title(), "yield": float(m.group(1)), "date": d.group(1) if d else "see page"}


@st.cache_data(ttl=3600, show_spinner=False)
def load_yields():
    out = {"ok": False, "data": {}, "error": None, "url": None, "fetched_at": now_str()}
    errs = []
    for url in TE_URLS:
        try:
            out["data"] = parse_te_yields(http_get(url).text)
            out.update(ok=True, url=url)
            return out
        except Exception as e:  # noqa: BLE001
            errs.append(f"{url.split('tradingeconomics.com')[-1] or '/'} -> {type(e).__name__}: {e}")
    out["error"] = " | ".join(errs)
    return out


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def override_input(label, auto, key, fallback, step, min_v, max_v, fmt="%.2f", help_=None):
    """Checkbox + number input. Off -> shows fetched value (disabled). Returns (value, source)."""
    missing = auto is None
    on = st.checkbox(f"Override: {label}", value=missing, key=f"ov_{key}_{missing}", help=help_)
    base = fallback if missing else auto
    wkey = f"in_{key}_on" if on else f"in_{key}_off_{round(float(base), 6)}"
    val = st.number_input(label, value=float(base), step=step, min_value=min_v,
                          max_value=max_v, format=fmt, disabled=not on, key=wkey)
    if on:
        return val, ("Manual input (fetch failed)" if missing else "Manual override")
    return val, "Fetched"


def pct_cfg(label):
    return st.column_config.NumberColumn(label, format="%.2f%%")


def main():
    st.set_page_config(page_title="International Cost of Capital", page_icon="🌍", layout="wide")
    st.title("🌍 International Cost of Capital: USD vs. Foreign Currency")
    st.caption("Compare a project's cost of equity, cost of debt and WACC in USD and in the "
               "currency of a selected country, using the same beta, tax rate, debt share and "
               "equity risk premium in both currencies.")

    # ---------------- Sidebar: refresh + data ----------------
    with st.sidebar:
        if st.button("🔄 Refresh data from sources"):
            st.cache_data.clear()
            st.rerun()

    betas, ratings, oecd, yields = load_betas(), load_ratings(), load_oecd_tax(), load_yields()

    ind_list = sorted(betas["df"]["industry"].tolist()) if betas["ok"] else [DEFAULT_INDUSTRY]
    ydata = yields["data"] if yields["ok"] else {}
    name_map = {k: k.title() for k in CURRENCY}          # fallback list so the dropdown always works
    name_map.update(oecd.get("names", {}))
    name_map.update({k: v["name"] for k, v in ydata.items()})
    name_map["united states"] = DEFAULT_COUNTRY
    name_to_key = {v: k for k, v in name_map.items()}
    country_list = sorted(name_to_key)

    with st.sidebar:
        st.header("Project settings")
        industry = st.selectbox("Industry", ind_list,
                                index=ind_list.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in ind_list else 0)
        country = st.selectbox("Target country", country_list,
                               index=country_list.index(DEFAULT_COUNTRY))
        ckey = name_to_key[country]
        is_us = ckey == "united states"
        ccy = CURRENCY.get(ckey, "local currency")
        erp_pct = st.number_input("Equity risk premium (%)", value=DEFAULT_ERP, step=0.1,
                                  min_value=0.0, max_value=20.0, format="%.2f")
        debt_pct = st.slider("Target debt share, D/(D+E) (%)", 0, 95, DEFAULT_DEBT, 1)
        tax_basis = st.radio("Project corporate tax rate", ["U.S.", "Selected country"],
                             horizontal=True,
                             help="The same project tax rate is used in both currencies.")

        st.header("Manual overrides")
        st.caption("Tick a box to replace the fetched value with your own.")
        auto_beta = None
        if betas["ok"]:
            m = betas["df"].loc[betas["df"]["industry"] == industry, "beta_u"]
            auto_beta = float(m.iloc[0]) if len(m) else None
        beta_u, beta_src = override_input("Unlevered beta", auto_beta, f"beta_{industry}",
                                          PLACEHOLDER["beta"], 0.01, 0.0, 5.0, "%.3f")
        tax_us, tax_us_src = override_input("U.S. tax rate (%)", oecd["rates"].get("united states"),
                                            "tax_us", US_TAX_OECD, 0.1, 0.0, 70.0)
        def yinfo(key):
            """Bulk page first, then the country's own page."""
            if key in ydata:
                return ydata[key], None
            r = load_country_yield(key)
            return (r["data"], None) if r["ok"] else (None, r["error"])

        us_info, us_err = yinfo("united states")
        fc_info, fc_err = (us_info, us_err) if is_us else yinfo(ckey)
        auto_tax_fc = tax_us if is_us else oecd["rates"].get(ckey)
        tax_fc, tax_fc_src = override_input(f"{country} tax rate (%)", auto_tax_fc,
                                            f"tax_fc_{ckey}_{round(tax_us, 4) if is_us else 'x'}",
                                            PLACEHOLDER["tax"], 0.1, 0.0, 70.0)
        spread_usd, spread_usd_src = override_input("USD BBB spread (%)", ratings.get("spread"),
                                                    "spread_usd", PLACEHOLDER["spread"], 0.05, 0.0, 15.0)
        y_us, y_us_src = override_input("U.S. 10-yr yield (%)",
                                        us_info["yield"] if us_info else None,
                                        "y_us", PLACEHOLDER["yield"], 0.05, -1.0, 60.0)
        auto_yfc = y_us if is_us else (fc_info["yield"] if fc_info else None)
        y_fc, y_fc_src = override_input(f"{country} 10-yr yield (%)", auto_yfc,
                                        f"y_fc_{ckey}_{round(y_us, 4) if is_us else 'x'}",
                                        PLACEHOLDER["yield"], 0.05, -1.0, 60.0)

        # Auto foreign-currency spread (needs the effective inputs above)
        d = debt_pct / 100
        tax = (tax_us if tax_basis == "U.S." else tax_fc) / 100
        base_calc = compute_capital_costs(beta_u, tax, erp_pct / 100, d, y_us / 100, y_fc / 100,
                                          spread_usd / 100)
        auto_spread_fc = base_calc["spread_fc_auto"] * 100
        ov_fc = st.checkbox("Override: foreign-currency BBB spread", value=False, key="ov_spread_fc",
                            help="Off: the spread is implied by parity from the USD spread and the two yields.")
        sk = f"in_spread_fc_on" if ov_fc else f"in_spread_fc_off_{round(auto_spread_fc, 6)}"
        spread_fc_in = st.number_input(f"{country} BBB spread (%)", value=float(auto_spread_fc),
                                       step=0.05, format="%.3f", disabled=not ov_fc, key=sk)
        spread_fc = spread_fc_in / 100 if ov_fc else None
        spread_fc_src = "Manual override" if ov_fc else "Auto (parity-implied)"

    # ---------------- Warnings on failed fetches ----------------
    failed = [n for n, d_ in [("industry betas", betas), ("BBB spread", ratings),
                              ("OECD tax rates", oecd), ("bond yields", {"ok": us_info is not None and fc_info is not None})] if not d_["ok"]]
    if failed:
        st.warning("Could not fetch live data for: **" + ", ".join(failed) + "**. Enter manual values in the "
                   "sidebar (those fields were switched to manual). Press **Refresh** to retry. "
                   "See the *Data sources* tab for error details.")

    # ---------------- Calculation ----------------
    res = compute_capital_costs(beta_u, tax, erp_pct / 100, d, y_us / 100, y_fc / 100,
                                spread_usd / 100, spread_fc)
    U, F = res["usd"], res["fc"]
    fc_label = f"{country} ({ccy})" if not is_us else "United States (USD)"
    tax_label = f"{tax * 100:.2f}% ({'U.S.' if tax_basis == 'U.S.' else country})"

    tab_res, tab_all, tab_src, tab_ver = st.tabs(
        ["📊 Results", "🏭 All industries", "📚 Data sources & assumptions", "✅ Verification"])

    # ---------------- Results tab ----------------
    with tab_res:
        st.subheader(f"{industry}  |  {country}  |  project tax rate {tax_label}")
        c1, c2 = st.columns(2)
        for col, title, r in [(c1, "USD", U), (c2, fc_label, F)]:
            with col:
                st.markdown(f"### {title}")
                m1, m2, m3 = st.columns(3)
                m1.metric("Cost of equity", f"{r['ke'] * 100:.2f}%")
                m2.metric("After-tax cost of debt", f"{r['kd_post'] * 100:.2f}%")
                m3.metric("WACC", f"{r['wacc'] * 100:.2f}%")
        rows = [
            ("Risk-free rate (10-yr government yield)", U["rf"], F["rf"]),
            ("Cost of equity", U["ke"], F["ke"]),
            ("Pre-tax cost of debt", U["kd_pre"], F["kd_pre"]),
            ("BBB spread over government yield", U["spread"], F["spread"]),
            ("After-tax cost of debt", U["kd_post"], F["kd_post"]),
            ("WACC", U["wacc"], F["wacc"]),
        ]
        tbl = pd.DataFrame(rows, columns=["Measure", "USD", fc_label])
        tbl["USD"] *= 100
        tbl[fc_label] *= 100
        tbl["Difference (FC - USD)"] = tbl[fc_label] - tbl["USD"]
        st.dataframe(tbl, hide_index=True,
                     column_config={"USD": pct_cfg("USD"), fc_label: pct_cfg(fc_label),
                                    "Difference (FC - USD)": pct_cfg("Difference (FC - USD)")})
        chart = pd.DataFrame({"USD": [U["ke"] * 100, U["kd_post"] * 100, U["wacc"] * 100],
                              fc_label: [F["ke"] * 100, F["kd_post"] * 100, F["wacc"] * 100]},
                             index=["Cost of equity", "After-tax cost of debt", "WACC"])
        try:
            st.bar_chart(chart, stack=False)
        except TypeError:
            st.bar_chart(chart)

        inputs = pd.DataFrame([
            ("Industry", industry, ""),
            ("Unlevered beta", f"{beta_u:.3f}", beta_src),
            ("Target debt share D/(D+E)", f"{debt_pct:.1f}%", "User input"),
            ("Implied D/E", f"{res['de_ratio']:.4f}", "Calculated"),
            ("Relevered beta", f"{res['beta_levered']:.3f}", "Calculated"),
            ("Equity risk premium", f"{erp_pct:.2f}%", "User input"),
            ("Project tax rate", tax_label, tax_us_src if tax_basis == "U.S." else tax_fc_src),
            ("U.S. 10-yr yield", f"{y_us:.2f}%", y_us_src),
            (f"{country} 10-yr yield", f"{y_fc:.2f}%", y_fc_src),
            ("USD BBB spread", f"{spread_usd:.2f}%", spread_usd_src),
            (f"{country} BBB spread", f"{F['spread'] * 100:.3f}%", spread_fc_src),
            ("Parity factor (1+y_FC)/(1+y_US)", f"{res['parity_factor']:.6f}", "Calculated"),
        ], columns=["Input", "Value", "Source"])
        with st.expander("Inputs used in this calculation", expanded=True):
            st.dataframe(inputs, hide_index=True)

        dl = pd.DataFrame({
            "measure": [r[0] for r in rows],
            "USD_pct": [r[1] * 100 for r in rows],
            f"{country}_pct": [r[2] * 100 for r in rows]})
        meta = pd.DataFrame({"measure": ["industry", "country", "unlevered_beta", "relevered_beta",
                                         "debt_share_pct", "erp_pct", "tax_rate_pct", "tax_basis"],
                             "USD_pct": [industry, country, beta_u, res["beta_levered"], debt_pct,
                                         erp_pct, tax * 100, tax_basis],
                             f"{country}_pct": ["", "", "", "", "", "", "", ""]})
        st.download_button("⬇️ Download these results (CSV)",
                           pd.concat([meta, dl]).to_csv(index=False).encode(),
                           file_name="cost_of_capital_results.csv", mime="text/csv")

    # ---------------- All-industry tab ----------------
    with tab_all:
        st.subheader("All-industry comparison")
        st.caption("Uses the current ERP, debt share, tax rate, yields and spreads. Each industry uses its "
                   "fetched unlevered beta; your beta override (if on) applies to the selected industry only.")
        if betas["ok"]:
            recs = []
            for _, row in betas["df"].iterrows():
                b = beta_u if row["industry"] == industry else row["beta_u"]
                rr = compute_capital_costs(b, tax, erp_pct / 100, d, y_us / 100, y_fc / 100,
                                           spread_usd / 100, spread_fc)
                recs.append({"Industry": row["industry"], "Unlevered beta": b,
                             "Relevered beta": rr["beta_levered"],
                             "Cost of equity, USD (%)": rr["usd"]["ke"] * 100,
                             "WACC, USD (%)": rr["usd"]["wacc"] * 100,
                             f"Cost of equity, {ccy} (%)": rr["fc"]["ke"] * 100,
                             f"WACC, {ccy} (%)": rr["fc"]["wacc"] * 100})
            allr = pd.DataFrame(recs).sort_values("WACC, USD (%)", ascending=False).reset_index(drop=True)
            st.dataframe(allr, hide_index=True, height=520,
                         column_config={c: st.column_config.NumberColumn(c, format="%.2f")
                                        for c in allr.columns if c != "Industry"})
            st.bar_chart(allr.set_index("Industry")[["WACC, USD (%)", f"WACC, {ccy} (%)"]])
            st.download_button("⬇️ Download all-industry table (CSV)", allr.to_csv(index=False).encode(),
                               file_name="all_industry_cost_of_capital.csv", mime="text/csv")
        else:
            st.info("Industry betas could not be fetched, so the all-industry table is unavailable. "
                    "Press Refresh in the sidebar to retry.")

    # ---------------- Sources tab ----------------
    with tab_src:
        st.subheader("Data sources and dates")
        src = pd.DataFrame([
            ("Unlevered beta (industry)", f"{beta_u:.3f}", beta_src, betas.get("asof") or "n/a",
             betas["fetched_at"], "OK" if betas["ok"] else f"FAILED: {betas['error']}", BETA_URL),
            ("USD BBB spread", f"{spread_usd:.2f}%", spread_usd_src, ratings.get("asof") or "n/a",
             ratings["fetched_at"], "OK" if ratings["ok"] else f"FAILED: {ratings['error']}", RATINGS_URL),
            ("U.S. tax rate", f"{tax_us:.2f}%", tax_us_src, f"OECD {oecd.get('year') or 'n/a'}",
             oecd["fetched_at"], "OK" if oecd["ok"] else f"FAILED: {oecd['error']}", OECD_URL),
            (f"{country} tax rate", f"{tax_fc:.2f}%", ("Same as U.S." if is_us else tax_fc_src), f"OECD {oecd.get('year') or 'n/a'}",
             oecd["fetched_at"], "OK" if oecd["ok"] else f"FAILED: {oecd['error']}", OECD_URL),
            ("U.S. 10-yr yield", f"{y_us:.2f}%", y_us_src,
             us_info["date"] if us_info else "n/a", yields["fetched_at"],
             "OK" if us_info else f"FAILED: {us_err or yields['error']}", TE_URL),
            (f"{country} 10-yr yield", f"{y_fc:.2f}%", ("Same as U.S." if is_us else y_fc_src),
             fc_info["date"] if fc_info else "n/a", yields["fetched_at"],
             "OK" if fc_info else f"FAILED: {fc_err or yields['error']}", TE_URL),
        ], columns=["Item", "Value used", "Source type", "Data as-of", "Fetched at", "Status", "URL"])
        st.dataframe(src, hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL")})
        if oecd["ok"]:
            st.caption(f"OECD table used: “{oecd['title'] or 'untitled'}”, latest year column = {oecd['year']}. "
                       "Check this is the combined (central + sub-central) statutory rate you want.")
        if not is_us and ckey not in ydata and yields["ok"]:
            st.info(f"No Trading Economics yield was found for {country}; enter it manually.")

        st.subheader("Main assumptions and formulas")
        st.markdown(f"""
**Same inputs in both currencies:** unlevered beta, relevered beta, project tax rate *t*, debt share *d* and ERP.
The project tax rate is a single choice (U.S. or selected country) applied to both calculations.

| Step | Formula |
|---|---|
| Debt-to-equity | D/E = d / (1 − d) |
| Relevered beta | β_L = β_U × [1 + (1 − t) × D/E] |
| USD cost of equity | Ke(USD) = y_US + β_L × ERP |
| USD cost of debt | Kd(USD) = y_US + BBB spread (USD) |
| USD WACC | (1 − d) × Ke + d × Kd × (1 − t) |
| Currency conversion factor | g = (1 + y_FC) / (1 + y_US) |
| Foreign cost of equity | Ke(FC) = (1 + Ke(USD)) × g − 1 |
| Foreign cost of debt (auto) | Kd(FC) = (1 + Kd(USD)) × g − 1; implied spread = Kd(FC) − y_FC |
| Foreign cost of debt (override) | Kd(FC) = y_FC + spread(FC) |
| Foreign WACC | (1 − d) × Ke(FC) + d × Kd(FC) × (1 − t) |

**Key assumptions and caveats**
- The 10-year government yield is the risk-free rate in each currency. The yield *difference* is used as the
  expected inflation/currency differential (interest-rate parity), so a higher foreign yield raises all
  foreign-currency costs.
- A government yield in a risky country also contains sovereign default risk, so for such countries the conversion
  is an approximation. The ERP is treated as a USD premium and converted with the rest of Ke.
- Industry betas are Damodaran’s *unlevered beta* column (not the cash-corrected one), based on U.S. firms.
- The BBB spread is for large non-financial service firms (Baa2/BBB). Selecting “United States” gives identical results
  in both columns unless you override one yield or the foreign spread.
- The app builds both WACCs from their components. Because the tax shield is applied after conversion,
  the foreign WACC can differ very slightly from converting the USD WACC directly.
- If your class formulas differ (for example how debt is converted), adjust `compute_capital_costs()` in `app.py`.
""")

    # ---------------- Verification tab ----------------
    with tab_ver:
        st.subheader("Built-in verification checks")
        st.markdown("**Check 1: hand-calculated example.** Inputs: β_U = 1.00, t = 25%, ERP = 6%, d = 30%, "
                    "y_US = 4%, y_FC = 8%, USD spread = 1.5%.")
        ex = compute_capital_costs(1.0, 0.25, 0.06, 0.30, 0.04, 0.08, 0.015)
        expected = {("usd", "ke"): 0.1192857143, ("usd", "wacc"): 0.095875,
                    ("fc", "ke"): 0.162335165, ("fc", "kd_pre"): 0.095576923,
                    ("fc", "wacc"): 0.1351394232}
        rows_v = [{"Quantity": f"{k[0].upper()} {k[1]}", "App (%)": ex[k[0]][k[1]] * 100,
                   "Hand calc (%)": v * 100,
                   "Match": "PASS" if abs(ex[k[0]][k[1]] - v) < 1e-6 else "FAIL"}
                  for k, v in expected.items()]
        st.dataframe(pd.DataFrame(rows_v), hide_index=True,
                     column_config={"App (%)": st.column_config.NumberColumn(format="%.4f"),
                                    "Hand calc (%)": st.column_config.NumberColumn(format="%.4f")})

        st.markdown("**Checks 2–4 on your current settings:**")
        args = dict(beta_u=beta_u, tax=tax, erp=erp_pct / 100, debt_share=d,
                    rf_us=y_us / 100, rf_fc=y_fc / 100, spread_usd=spread_usd / 100)
        eq = compute_capital_costs(**{**args, "rf_fc": args["rf_us"]}, spread_fc=None)
        c2 = all(abs(eq["usd"][k] - eq["fc"][k]) < 1e-12 for k in ["ke", "kd_pre", "kd_post", "wacc"])
        z = compute_capital_costs(**{**args, "debt_share": 0.0}, spread_fc=spread_fc)
        c3 = abs(z["usd"]["wacc"] - z["usd"]["ke"]) < 1e-12 and abs(z["fc"]["wacc"] - z["fc"]["ke"]) < 1e-12
        a = compute_capital_costs(**args, spread_fc=None)
        b = compute_capital_costs(**args, spread_fc=0.0777)
        c4 = a["usd"] == b["usd"]
        st.dataframe(pd.DataFrame([
            {"Check": "Equal government yields -> USD and foreign results are equal (foreign spread override off)",
             "Result": "PASS" if c2 else "FAIL"},
            {"Check": "Zero debt -> WACC equals cost of equity (both currencies)",
             "Result": "PASS" if c3 else "FAIL"},
            {"Check": "Overriding the foreign spread leaves all USD results unchanged",
             "Result": "PASS" if c4 else "FAIL"},
        ]), hide_index=True)
        st.caption("To verify independently, rebuild Check 1 in a spreadsheet or calculator using the "
                   "formulas in the Data sources & assumptions tab.")


if __name__ == "__main__":
    main()
