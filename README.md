# International Cost of Capital App

A Streamlit app that compares a project's **cost of equity, cost of debt and WACC in USD and in the currency of a selected country**, using the same beta, tax rate, debt share and equity risk premium in both currencies.

**Live app:** _paste your Streamlit link here_

## Data sources (fetched live; refresh button in the sidebar)

| Input | Source |
|---|---|
| Unlevered industry betas | Damodaran, `betas.xls` ("Unlevered beta" column) |
| USD BBB borrowing spread | Damodaran ratings page (Baa2/BBB, large non-financial service firms) |
| Corporate tax rates | OECD statutory corporate income tax rates |
| 10-year government yields | Trading Economics (scraped) |

The *Data sources & assumptions* tab shows the value used, source type (fetched / manual), as-of date, fetch time and any fetch error. If a source cannot be fetched, the matching sidebar field switches to manual input.

## Defaults
Software (System & Application), United States, U.S. tax rate, 6.5% equity risk premium, 30% debt share.

## Method
```
D/E        = d / (1 - d)
beta_L     = beta_U * (1 + (1 - t) * D/E)
Ke_USD     = y_US + beta_L * ERP
Kd_USD     = y_US + BBB spread
WACC_USD   = (1 - d) * Ke_USD + d * Kd_USD * (1 - t)

g          = (1 + y_FC) / (1 + y_US)
Ke_FC      = (1 + Ke_USD) * g - 1
Kd_FC      = (1 + Kd_USD) * g - 1        (auto; implied spread = Kd_FC - y_FC)
Kd_FC      = y_FC + spread_FC            (if foreign spread override is on)
WACC_FC    = (1 - d) * Ke_FC + d * Kd_FC * (1 - t)
```
`t` is one project tax rate (U.S. or selected country, chosen with the radio button) used in both currencies.

If your class formulas differ, edit `compute_capital_costs()` in `app.py`.

## Verification
Hand-calculated example (β_U = 1.00, t = 25%, ERP = 6%, d = 30%, y_US = 4%, y_FC = 8%, USD spread = 1.5%):

| | USD | Foreign |
|---|---|---|
| Cost of equity | 11.9286% | 16.2335% |
| Pre-tax cost of debt | 5.50% | 9.5577% |
| WACC | 9.5875% | 13.5139% |

Built-in checks (Verification tab): equal yields give equal results with the foreign spread override off; zero debt gives WACC = cost of equity; overriding the foreign spread leaves USD results unchanged.

## Run locally
```
pip install -r requirements.txt
streamlit run app.py
```

## Deploy
Push `app.py`, `requirements.txt` and `README.md` to GitHub, then create the app at share.streamlit.io with `app.py` as the main file.

## Limitations
- Scraped pages can change layout; use the manual overrides if a fetch fails.
- Yield-parity conversion is an approximation; risky-country yields include sovereign default risk.
- Industry betas are based on U.S. firms.
