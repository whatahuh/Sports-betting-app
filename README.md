# POLY-QUANT

Streamlit terminal for Polymarket + Kalshi: value plays, explore, bet audit, sentiment, arbs, ledger.

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
5. Confirm the header shows the latest **Build** tag (e.g. `2.2.0-arb-autosuggest`)

## Current build

See `APP_BUILD` in `app.py` — displayed in the app header when deployed correctly.

**v4.0** adds a Command Center dashboard, modern UI, per-tab guides, and live stat tiles across all views.
