# FinTS4 (Home Assistant Custom Integration)

Read balances (and optional holdings) from German banks via FinTS.
This is based on the original Home Assistant Integration but updated for FinTS 4 with Product Id support.

## Install (HACS)

1. HACS → **Integrations** → **⋮** → **Custom repositories**
2. Add your repository URL and select category **Integration**
3. Install **FinTS4**
4. Restart Home Assistant
5. Settings → **Devices & services** → **Add integration** → **FinTS4**

## Manual install

Copy `custom_components/fints4/` into your Home Assistant config folder at:

`<config>/custom_components/fints4/`

Then restart Home Assistant.

## Configuration

Configuration happens via the UI config flow. You’ll need:

- Bank identification number (BLZ)
- Username
- PIN
- Bank URL (FinTS/HBCI endpoint)
- Product ID, name

