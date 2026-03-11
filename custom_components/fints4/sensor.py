"""FinTS4 sensor entities: balance, holdings, available balance, upcoming transactions."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, cast

from fints.models import SEPAAccount
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import BankCredentials, FinTsClient
from .const import (
    ACCOUNT_TYPE_AVAILABLE_BALANCE,
    ACCOUNT_TYPE_BALANCE,
    ACCOUNT_TYPE_HOLDINGS,
    ACCOUNT_TYPE_UPCOMING_TRANSACTIONS,
    ATTR_ACCOUNT_TYPE,
    ATTR_BANK,
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIN,
    CONF_HOLDINGS,
    CONF_PRODUCT_ID,
    DOMAIN,
)
from .coordinator import (
    FinTsDataUpdateCoordinator,
    account_identifier,
    get_account_device_info,
)


def _serialize_attribute_value(value: Any, depth: int = 0, max_depth: int = 10) -> Any:
    """Serialize attribute values, converting non-JSON types to strings."""
    if depth > max_depth:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_serialize_attribute_value(v, depth + 1, max_depth) for v in value]
    if isinstance(value, dict):
        serialized: dict[str, Any] = {}
        for key, val in value.items():
            serialized[str(key)] = _serialize_attribute_value(val, depth + 1, max_depth)
        return serialized
    return str(value)


_LOGGER = logging.getLogger(__name__)

ICON = "mdi:currency-eur"


# ---------------------------------------------------------------------------
# Legacy YAML platform schema (kept for backwards compatibility)
# ---------------------------------------------------------------------------

ATTR_ACCOUNT = CONF_ACCOUNT

SCHEMA_ACCOUNTS = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): cv.string,
        vol.Optional(CONF_NAME, default=None): vol.Any(None, cv.string),
    }
)

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_BIN): cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PIN): cv.string,
        vol.Required(CONF_URL): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PRODUCT_ID): cv.string,
        vol.Optional(CONF_ACCOUNTS, default=[]): cv.ensure_list(SCHEMA_ACCOUNTS),
        vol.Optional(CONF_HOLDINGS, default=[]): cv.ensure_list(SCHEMA_ACCOUNTS),
    }
)

SCAN_INTERVAL = timedelta(hours=4)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the sensors from YAML (legacy path, not used for config entries)."""
    credentials = BankCredentials(
        config[CONF_BIN],
        config[CONF_USERNAME],
        config[CONF_PIN],
        config[CONF_URL],
        config.get(CONF_PRODUCT_ID),
        None,
    )
    fints_name = cast(str, config.get(CONF_NAME, config[CONF_BIN]))

    account_config = {
        acc[CONF_ACCOUNT]: acc.get(CONF_NAME) for acc in config[CONF_ACCOUNTS]
    }
    holdings_config = {
        acc[CONF_ACCOUNT]: acc.get(CONF_NAME) for acc in config[CONF_HOLDINGS]
    }

    client = FinTsClient(credentials, fints_name, account_config, holdings_config)
    balance_accounts, holdings_accounts = client.detect_accounts()
    entities = _create_legacy_entities(
        client, fints_name, account_config, holdings_config,
        balance_accounts, holdings_accounts,
    )
    add_entities(entities, True)


def _create_legacy_entities(
    client: FinTsClient,
    fints_name: str,
    account_config: dict[str, str | None],
    holdings_config: dict[str, str | None],
    balance_accounts: list[SEPAAccount],
    holdings_accounts: list[SEPAAccount],
) -> list[SensorEntity]:
    """Return legacy (non-coordinator) entities for YAML setup."""
    entities: list[SensorEntity] = []

    for account in balance_accounts:
        if account_config and account.iban not in account_config:
            continue
        account_name = account_config.get(account.iban)
        if not account_name:
            account_name = account.accountnumber or account.iban or "FinTS balance"
        entities.append(FinTsLegacyAccount(client, account, account_name))

    for account in holdings_accounts:
        if holdings_config and account.accountnumber not in holdings_config:
            continue
        account_name = holdings_config.get(account.accountnumber)
        if not account_name:
            account_name = account.accountnumber or account.iban or "FinTS holdings"
        entities.append(FinTsLegacyHoldingsAccount(client, account, account_name))

    return entities


# ---------------------------------------------------------------------------
# Config entry setup — coordinator-based entities
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FinTS sensors from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FinTsDataUpdateCoordinator = entry_data["coordinator"]
    client: FinTsClient = entry_data["client"]
    fints_name: str = entry_data["fints_name"]
    account_config: dict[str, str | None] = entry_data["account_config"]
    holdings_config: dict[str, str | None] = entry_data["holdings_config"]
    balance_accounts: list[SEPAAccount] = entry_data["balance_accounts"]
    holdings_accounts: list[SEPAAccount] = entry_data["holdings_accounts"]

    entities: list[SensorEntity] = []

    for account in balance_accounts:
        iban = account.iban
        if account_config and iban not in account_config:
            _LOGGER.debug("Skipping account %s for bank %s", iban, fints_name)
            continue

        account_name = account_config.get(iban)
        if not account_name:
            account_name = account.accountnumber or iban or "FinTS balance"

        entities.append(
            FinTsBalanceSensor(coordinator, entry, client, account, account_name)
        )
        entities.append(
            FinTsAvailableBalanceSensor(coordinator, entry, client, account, account_name)
        )
        entities.append(
            FinTsUpcomingTransactionsSensor(coordinator, entry, client, account, account_name)
        )
        _LOGGER.debug("Creating sensors for account %s (bank %s)", iban, fints_name)

    for account in holdings_accounts:
        acc_nr = account.accountnumber
        if holdings_config and acc_nr not in holdings_config:
            _LOGGER.debug("Skipping holdings %s for bank %s", acc_nr, fints_name)
            continue

        account_name = holdings_config.get(acc_nr)
        if not account_name:
            account_name = acc_nr or account.iban or "FinTS holdings"

        entities.append(
            FinTsHoldingsSensor(coordinator, entry, client, account, account_name)
        )
        _LOGGER.debug("Creating holdings sensor for %s (bank %s)", acc_nr, fints_name)

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Coordinator-based entities
# ---------------------------------------------------------------------------


class FinTsBalanceSensor(CoordinatorEntity[FinTsDataUpdateCoordinator], SensorEntity):
    """Sensor for a FinTS balance account — reads from coordinator data."""

    _attr_icon = ICON
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._iban = account.iban
        ident = account_identifier(account, client.name)
        self._attr_unique_id = f"{entry.entry_id}_{ident}_balance"
        self._attr_name = f"{name} Balance"
        self._attr_device_info = get_account_device_info(entry, account, client.name)
        self._attr_extra_state_attributes: dict[str, Any] = {
            "account": account.iban,
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_BALANCE,
            "account_number": getattr(account, "accountnumber", None),
            "iban": account.iban,
            "bic": getattr(account, "bic", None),
        }
        if client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = client.name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update state from coordinator data."""
        if not self.coordinator.data:
            return
        account_data = self.coordinator.data.accounts.get(self._iban)
        if not account_data or not account_data.balance:
            self._attr_available = False
            self.async_write_ha_state()
            return

        balance = account_data.balance
        if (
            balance.amount
            and getattr(balance.amount, "amount", None) is not None
        ):
            self._attr_native_value = balance.amount.amount
            self._attr_native_unit_of_measurement = balance.amount.currency
            self._attr_extra_state_attributes["balance_date"] = (
                str(balance.date) if balance.date else None
            )
            self._attr_available = True
        else:
            self._attr_available = False

        self.async_write_ha_state()


class FinTsAvailableBalanceSensor(CoordinatorEntity[FinTsDataUpdateCoordinator], SensorEntity):
    """Sensor showing balance minus pending outgoing transactions."""

    _attr_icon = "mdi:cash-minus"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._iban = account.iban
        ident = account_identifier(account, client.name)
        self._attr_unique_id = f"{entry.entry_id}_{ident}_available_balance"
        self._attr_name = f"{name} Available Balance"
        self._attr_device_info = get_account_device_info(entry, account, client.name)
        self._attr_extra_state_attributes: dict[str, Any] = {
            "account": account.iban,
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_AVAILABLE_BALANCE,
        }
        if client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = client.name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Calculate balance - pending outgoing."""
        if not self.coordinator.data:
            return
        account_data = self.coordinator.data.accounts.get(self._iban)
        if not account_data or not account_data.balance:
            self._attr_available = False
            self.async_write_ha_state()
            return

        balance = account_data.balance
        if not (balance.amount and getattr(balance.amount, "amount", None) is not None):
            self._attr_available = False
            self.async_write_ha_state()
            return

        balance_amount = float(balance.amount.amount)
        pending_outgoing_sum = 0.0
        pending_outgoing_count = 0
        for tx in account_data.pending_transactions:
            if tx.get("direction") == "outgoing" and tx.get("amount") is not None:
                pending_outgoing_sum += abs(tx["amount"])
                pending_outgoing_count += 1

        self._attr_native_value = round(balance_amount - pending_outgoing_sum, 2)
        self._attr_native_unit_of_measurement = balance.amount.currency
        self._attr_extra_state_attributes["balance"] = balance_amount
        self._attr_extra_state_attributes["pending_outgoing_sum"] = round(
            pending_outgoing_sum, 2
        )
        self._attr_extra_state_attributes["pending_outgoing_count"] = pending_outgoing_count
        self._attr_available = True
        self.async_write_ha_state()


class FinTsUpcomingTransactionsSensor(CoordinatorEntity[FinTsDataUpdateCoordinator], SensorEntity):
    """Sensor showing pending/upcoming transactions."""

    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._iban = account.iban
        ident = account_identifier(account, client.name)
        self._attr_unique_id = f"{entry.entry_id}_{ident}_upcoming_transactions"
        self._attr_name = f"{name} Upcoming Transactions"
        self._attr_device_info = get_account_device_info(entry, account, client.name)
        self._attr_extra_state_attributes: dict[str, Any] = {
            "account": account.iban,
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_UPCOMING_TRANSACTIONS,
            "transactions": [],
        }
        if client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = client.name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update pending transactions list."""
        if not self.coordinator.data:
            return
        account_data = self.coordinator.data.accounts.get(self._iban)
        if not account_data:
            self._attr_available = False
            self.async_write_ha_state()
            return

        pending = account_data.pending_transactions
        self._attr_native_value = len(pending)
        # pending entries are already plain dicts from _serialize_tx(); no
        # further serialization needed — just shallow-copy the list.
        self._attr_extra_state_attributes["transactions"] = list(pending)
        self._attr_available = True
        self.async_write_ha_state()


class FinTsHoldingsSensor(CoordinatorEntity[FinTsDataUpdateCoordinator], SensorEntity):
    """Sensor for a FinTS holdings account — reads from coordinator data."""

    _attr_icon = ICON
    _attr_native_unit_of_measurement = "EUR"

    def __init__(
        self,
        coordinator: FinTsDataUpdateCoordinator,
        entry: ConfigEntry,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._acc_nr = getattr(account, "accountnumber", None) or ""
        self._attr_unique_id = f"{entry.entry_id}_{self._acc_nr}_holdings"
        self._attr_name = name
        self._attr_device_info = get_account_device_info(entry, account, client.name)
        self._holdings_attributes: dict[str, Any] = {}
        self._attr_extra_state_attributes: dict[str, Any] = {
            "account": self._acc_nr,
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_HOLDINGS,
            "account_number": self._acc_nr,
            "iban": getattr(account, "iban", None),
            "bic": getattr(account, "bic", None),
        }
        if client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = client.name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update holdings from coordinator data."""
        if not self.coordinator.data:
            return
        holdings = self.coordinator.data.holdings.get(self._acc_nr)
        if holdings is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        total = sum(
            h.total_value
            for h in holdings
            if getattr(h, "total_value", None) is not None
        )
        self._attr_native_value = total if total else 0
        self._attr_available = True

        # Build per-holding attributes
        attrs: dict[str, Any] = {}
        for holding in holdings:
            if holding.name:
                attrs[f"{holding.name} total"] = _serialize_attribute_value(
                    getattr(holding, "total_value", None)
                )
                attrs[f"{holding.name} pieces"] = _serialize_attribute_value(
                    getattr(holding, "pieces", None)
                )
                attrs[f"{holding.name} price"] = _serialize_attribute_value(
                    getattr(holding, "market_value", None)
                )
        self._holdings_attributes = attrs
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional attributes of the sensor."""
        attributes = dict(self._attr_extra_state_attributes)
        attributes.update(self._holdings_attributes)
        return attributes


# ---------------------------------------------------------------------------
# Legacy (YAML-only) entities — sync update(), no coordinator
# ---------------------------------------------------------------------------


class FinTsLegacyAccount(SensorEntity):
    """Legacy sensor for a FinTS balance account (YAML setup)."""

    def __init__(
        self,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        self._client = client
        self._account = account
        self._balance = None
        account_ident = (
            getattr(account, "iban", None)
            or getattr(account, "accountnumber", None)
            or self._client.name
        )
        self._attr_unique_id = f"{self._client.name}_{account_ident}_balance"
        self._attr_name = name
        self._attr_icon = ICON
        self._attr_extra_state_attributes = {
            "account": self._account.iban,
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_BALANCE,
        }
        if self._client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = self._client.name

    def update(self) -> None:
        """Get the current balance and currency for the account."""
        try:
            bank = self._client.client
            with bank:
                self._balance = bank.get_balance(self._account)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error updating balance for %s: %s", self.name, err)
            self._attr_available = False
            return

        self._attr_available = True
        if (
            self._balance is not None
            and self._balance.amount
            and getattr(self._balance.amount, "amount", None) is not None
        ):
            self._attr_native_value = self._balance.amount.amount
            self._attr_native_unit_of_measurement = self._balance.amount.currency
            self._attr_extra_state_attributes["balance_date"] = (
                str(self._balance.date) if self._balance.date else None
            )
        else:
            self._attr_available = False


class FinTsLegacyHoldingsAccount(SensorEntity):
    """Legacy sensor for a FinTS holdings account (YAML setup)."""

    def __init__(
        self,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
    ) -> None:
        self._client = client
        self._attr_name = name
        self._account = account
        self._holdings: list[Any] = []
        self._holdings_attributes: dict[str, Any] = {}
        account_ident = (
            getattr(account, "accountnumber", None) or self._client.name
        )
        self._attr_unique_id = f"{self._client.name}_{account_ident}_holdings"
        self._attr_icon = ICON
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_extra_state_attributes = {
            "account": getattr(account, "accountnumber", None),
            ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_HOLDINGS,
        }
        if self._client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = self._client.name

    def update(self) -> None:
        """Get the current holdings for the account."""
        try:
            bank = self._client.client
            with bank:
                self._holdings = bank.get_holdings(self._account)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error updating holdings for %s: %s", self.name, err)
            self._attr_available = False
            return

        self._attr_available = True
        total = sum(
            h.total_value
            for h in self._holdings
            if getattr(h, "total_value", None) is not None
        )
        self._attr_native_value = total if total else 0

        attrs: dict[str, Any] = {}
        for holding in self._holdings:
            if holding.name:
                attrs[f"{holding.name} total"] = _serialize_attribute_value(
                    getattr(holding, "total_value", None)
                )
                attrs[f"{holding.name} pieces"] = _serialize_attribute_value(
                    getattr(holding, "pieces", None)
                )
                attrs[f"{holding.name} price"] = _serialize_attribute_value(
                    getattr(holding, "market_value", None)
                )
        self._holdings_attributes = attrs

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional attributes of the sensor."""
        attributes = dict(self._attr_extra_state_attributes)
        attributes.update(self._holdings_attributes)
        return attributes
