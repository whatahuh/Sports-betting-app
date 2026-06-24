# POLY-QUANT

A modern Streamlit intelligence terminal for Polymarket + Kalshi. It opens on a
data-driven **Dashboard** (live market stats, category breakdowns, and a guided
how-to-use walkthrough) and includes: value plays, explore, bet check, sentiment,
risk-free arbs, and a performance ledger.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud deploy

1. Connect repo `whatahuh/Sports-betting-app`
2. **Branch:** `main`
3. **Main file:** `app.py`
4. After each push to `main`, open the app → **⋮ → Reboot app**
5. Confirm the brand bar shows the latest **Build** chip (e.g. `4.0.0-modern-ui`)

## Current build

See `APP_BUILD` in `app.py` — displayed in the app header when deployed correctly.
