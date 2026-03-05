"""The FinTS4 integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .client import BankCredentials, FinTsClient
from .const import CONF_ACCOUNT, CONF_ACCOUNTS, CONF_BIN, CONF_HOLDINGS, CONF_PRODUCT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FinTS4 from a config entry.

    The bank connection (detect_accounts) is established here – before
    async_forward_entry_setups – so that ConfigEntryAuthFailed is raised at
    the right time and HA can immediately show the re-auth notification
    instead of propagating the exception through the sensor platform.
    """
    hass.data.setdefault(DOMAIN, {})

    data = entry.data
    system_id = data.get("system_id")
    credentials = BankCredentials(
        data[CONF_BIN],
        data[CONF_USERNAME],
        data[CONF_PIN],
        data[CONF_URL],
        data.get(CONF_PRODUCT_ID),
        system_id,
    )
    fints_name = data.get(CONF_NAME, data[CONF_BIN])
    account_config = {
        acc[CONF_ACCOUNT]: acc.get(CONF_NAME)
        for acc in data.get(CONF_ACCOUNTS, [])
    }
    holdings_config = {
        acc[CONF_ACCOUNT]: acc.get(CONF_NAME)
        for acc in data.get(CONF_HOLDINGS, [])
    }

    client = FinTsClient(credentials, fints_name, account_config, holdings_config)

    try:
        balance_accounts, holdings_accounts = await hass.async_add_executor_job(
            client.detect_accounts
        )
    except Exception as err:  # noqa: BLE001
        err_str = str(err)
        _LOGGER.error("FinTS connection failed for %s: %s", fints_name, err_str)
        # Treat failures as auth errors so HA shows the re-auth notification.
        # This covers expired system_id (60-day pushTAN renewal) and wrong PIN.
        raise ConfigEntryAuthFailed(err_str) from err

    # Persist a refreshed system_id when the bank issues a new one
    new_system_id = client.system_id
    if new_system_id and new_system_id != system_id:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "system_id": new_system_id},
        )

    # Share the authenticated client + account lists with platform setup
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "balance_accounts": balance_accounts,
        "holdings_accounts": holdings_accounts,
        "fints_name": fints_name,
        "account_config": account_config,
        "holdings_config": holdings_config,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
