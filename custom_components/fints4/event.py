"""FinTS4 event entities: new transaction and new pending transaction."""

from __future__ import annotations

import logging
from typing import Any

from fints.models import SEPAAccount

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import FinTsClient
from .const import DOMAIN
from .coordinator import (
    FinTsDataUpdateCoordinator,
    account_identifier,
    event_payload,
    get_account_device_info,
)

_LOGGER = logging.getLogger(__name__)

EVENT_NEW_TRANSACTION = "new_transaction"
EVENT_NEW_PENDING_TRANSACTION = "new_pending_transaction"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FinTS event entities from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FinTsDataUpdateCoordinator = entry_data["coordinator"]
    client: FinTsClient = entry_data["client"]
    account_config: dict[str, str | None] = entry_data["account_config"]
    balance_accounts: list[SEPAAccount] = entry_data["balance_accounts"]

    entities: list[EventEntity] = []

    for account in balance_accounts:
        iban = account.iban
        if account_config and iban not in account_config:
            continue

        account_name = account_config.get(iban)
        if not account_name:
            account_name = account.accountnumber or iban or "FinTS"

        entities.append(
            FinTsNewTransactionEvent(coordinator, entry, client, account, account_name)
        )
        entities.append(
            FinTsNewPendingTransactionEvent(coordinator, entry, client, account, account_name)
        )

    async_add_entities(entities)


class _FinTsBaseTransactionEvent(
    CoordinatorEntity[FinTsDataUpdateCoordinator], EventEntity
):
    """Base class for FinTS transaction event entities."""

    _event_type: str  # overridden by subclasses
    _data_key: str  # "new_booked" or "new_pending"

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
        *,
        unique_suffix: str,
        name_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._iban = account.iban
        ident = account_identifier(account, client.name)
        self._attr_unique_id = f"{entry.entry_id}_{ident}_{unique_suffix}"
        self._attr_name = f"{name} {name_suffix}"
        self._attr_device_info = get_account_device_info(entry, account, client.name)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire events for each new transaction of the relevant type."""
        if not self.coordinator.data:
            return

        new_txs: list[dict[str, Any]] = getattr(
            self.coordinator.data, self._data_key, {}
        ).get(self._iban, [])

        if not new_txs:
            return  # nothing new — skip the state write

        for tx in new_txs:
            self._trigger_event(self._event_type, event_payload(tx))

        self.async_write_ha_state()


class FinTsNewTransactionEvent(_FinTsBaseTransactionEvent):
    """Event entity that fires when new booked transactions are detected."""

    _attr_icon = "mdi:bank-transfer"
    _attr_event_types = [EVENT_NEW_TRANSACTION]
    _event_type = EVENT_NEW_TRANSACTION
    _data_key = "new_booked"

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(
            coordinator, entry, client, account, name,
            unique_suffix="new_transaction",
            name_suffix="New Transaction",
        )


class FinTsNewPendingTransactionEvent(_FinTsBaseTransactionEvent):
    """Event entity that fires when new pending transactions appear."""

    _attr_icon = "mdi:bank-transfer-in"
    _attr_event_types = [EVENT_NEW_PENDING_TRANSACTION]
    _event_type = EVENT_NEW_PENDING_TRANSACTION
    _data_key = "new_pending"

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(
            coordinator, entry, client, account, name,
            unique_suffix="new_pending_transaction",
            name_suffix="New Pending Transaction",
        )
