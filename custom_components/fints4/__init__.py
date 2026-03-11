"""The FinTS4 integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .client import BankCredentials, FinTsClient
from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIN,
    CONF_HOLDINGS,
    CONF_PRODUCT_ID,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .coordinator import FinTsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.EVENT]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FinTS4 from a config entry.

    Creates a single DataUpdateCoordinator that fetches balance, transactions,
    and holdings in one FinTS dialog per poll cycle.
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
        raise ConfigEntryAuthFailed(err_str) from err

    # Persist a refreshed system_id when the bank issues a new one
    new_system_id = client.system_id
    if new_system_id and new_system_id != system_id:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "system_id": new_system_id},
        )

    # Create the coordinator — single polling loop for all entities
    coordinator = FinTsDataUpdateCoordinator(
        hass,
        client,
        balance_accounts,
        holdings_accounts,
        update_interval_minutes=DEFAULT_UPDATE_INTERVAL,
    )
    coordinator.config_entry = entry

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
        "fints_name": fints_name,
        "account_config": account_config,
        "holdings_config": holdings_config,
        "balance_accounts": balance_accounts,
        "holdings_accounts": holdings_accounts,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
