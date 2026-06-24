# Streamlit Cloud deploy checklist

If the app looks unchanged after pushes to GitHub:

1. Open [Streamlit Cloud](https://share.streamlit.io) → your app → **⋮ Manage app**
2. Confirm **Repository:** `whatahuh/Sports-betting-app`
3. Confirm **Branch:** `main`
4. Confirm **Main file path:** `app.py`
5. Click **Reboot app** (required after every merge to `main`)
6. Hard-refresh browser (Ctrl+Shift+R / pull-to-refresh on mobile)

## Verify you are on the latest build

The green bar at the top of the app must show:

- **LIVE BUILD 3.0.0-perf-calendar**
- **commit d52a166** (or newer after subsequent deploys)

Browser tab title should read: `POLY-QUANT · 3.0.0-perf-calendar`

If the build string is older (e.g. `2.2.0-arb-autosuggest`), Streamlit Cloud is **not** serving current `main`.
