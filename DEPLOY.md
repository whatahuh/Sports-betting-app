# Streamlit Cloud deploy checklist

If the app looks unchanged after pushes to GitHub:

1. Confirm the code was merged into the branch Streamlit serves. Cloud-agent feature branches do
   not update the public app until they are merged.
2. Open [Streamlit Cloud](https://share.streamlit.io) → your app → **⋮ Manage app**
3. Confirm **Repository:** `whatahuh/Sports-betting-app`
4. Confirm **Branch:** `main`
5. Confirm **Main file path:** `app.py`
6. Click **Reboot app** (required after every merge to `main`)
7. Hard-refresh browser (Ctrl+Shift+R / pull-to-refresh on mobile)

## Verify you are on the latest build

The brand bar at the top of the app shows a build chip. It must read:

- **Build 4.0.0-modern-ui** (or newer after subsequent deploys)

Browser tab title should read: `POLY-QUANT · 4.0.0-modern-ui`

If the build string is older (e.g. `3.2.0-arb-action-panel`), Streamlit Cloud is **not** serving current `main`.
