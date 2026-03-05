"""Read the balance of your bank accounts via FinTS."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, cast

from fints.models import SEPAAccount
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
)
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .client import BankCredentials, FinTsClient
from .const import (
    ATTR_ACCOUNT_TYPE,
    ATTR_BANK,
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIN,
    CONF_HOLDINGS,
    CONF_PRODUCT_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=4)

ICON = "mdi:currency-eur"

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
    accounts = _create_entities(
        client,
        fints_name,
        account_config,
        holdings_config,
        balance_accounts,
        holdings_accounts,
    )
    add_entities(accounts, True)


def _create_entities(
    client: FinTsClient,
    fints_name: str,
    account_config: dict[str, str | None],
    holdings_config: dict[str, str | None],
    balance_accounts: list[SEPAAccount],
    holdings_accounts: list[SEPAAccount],
    config_entry: ConfigEntry | None = None,
) -> list[SensorEntity]:
    """Return a list of entities for the given account lists."""

    accounts: list[SensorEntity] = []

    for account in balance_accounts:
        if account_config and account.iban not in account_config:
            _LOGGER.debug("Skipping account %s for bank %s", account.iban, fints_name)
            continue

        account_name = account_config.get(account.iban)
        if not account_name:
            account_name = f"{fints_name} - {account.iban}"
        accounts.append(FinTsAccount(client, account, account_name, config_entry))
        _LOGGER.debug("Creating account %s for bank %s", account.iban, fints_name)

    for account in holdings_accounts:
        if holdings_config and account.accountnumber not in holdings_config:
            _LOGGER.debug(
                "Skipping holdings %s for bank %s", account.accountnumber, fints_name
            )
            continue

        account_name = holdings_config.get(account.accountnumber)
        if not account_name:
            account_name = f"{fints_name} - {account.accountnumber}"
        accounts.append(
            FinTsHoldingsAccount(client, account, account_name, config_entry)
        )
        _LOGGER.debug(
            "Creating holdings %s for bank %s", account.accountnumber, fints_name
        )

    return accounts


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FinTS sensors from a config entry.

    The bank connection and account discovery are done in __init__.async_setup_entry
    before this is called, so we only need to create the entities from the
    already-fetched data stored in hass.data.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]

    accounts = _create_entities(
        entry_data["client"],
        entry_data["fints_name"],
        entry_data["account_config"],
        entry_data["holdings_config"],
        entry_data["balance_accounts"],
        entry_data["holdings_accounts"],
        config_entry=entry,
    )

    async_add_entities(accounts, True)


class FinTsAccount(SensorEntity):
    """Sensor for a FinTS balance account."""

    def __init__(
        self,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        self._client = client
        self._account = account
        self._balance = None
        self._config_entry = config_entry
        account_identifier = (
            getattr(account, "iban", None)
            or getattr(account, "accountnumber", None)
            or self._client.name
        )
        unique_id = f"{self._client.name}_{account_identifier}_balance"
        if self._config_entry:
            unique_id = f"{self._config_entry.entry_id}_{unique_id}"
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_icon = ICON
        self._attr_extra_state_attributes = {
            ATTR_ACCOUNT: self._account.iban,
            ATTR_ACCOUNT_TYPE: "balance",
        }
        if self._client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = self._client.name
        self._update_account_attributes()

    @property
    def device_info(self):
        info = {
            "identifiers": {(DOMAIN, self._client.name)},
            "name": f"FinTS - {self._client.name}",
            "manufacturer": "FinTS",
            "model": "Bank Account",
        }
        if self._config_entry is not None:
            info["config_entry_id"] = self._config_entry.entry_id
        return info

    def _update_account_attributes(self) -> None:
        self._attr_extra_state_attributes["account_number"] = getattr(
            self._account, "accountnumber", None
        )
        self._attr_extra_state_attributes["iban"] = getattr(self._account, "iban", None)
        self._attr_extra_state_attributes["bic"] = getattr(self._account, "bic", None)
        self._attr_extra_state_attributes["subaccount_number"] = getattr(
            self._account, "subaccountnumber", None
        )
        self._attr_extra_state_attributes["account_type"] = getattr(
            self._account, "type", None
        )
        self._attr_extra_state_attributes["currency"] = getattr(
            self._account, "currency", None
        )

    def update(self) -> None:
        """Get the current balance and currency for the account."""
        try:
            bank = self._client.client
            with bank:
                self._balance = bank.get_balance(self._account)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error updating balance for %s: %s – re-authentication may be needed",
                self.name,
                err,
            )
            self._attr_available = False
            if self._config_entry is not None:
                self._config_entry.async_start_reauth(self.hass)
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
            _LOGGER.warning(
                "Balance for %s has no amount/currency - skipping this account",
                self.name,
            )
            self._attr_available = False
        _LOGGER.debug("updated balance of account %s", self.name)


class FinTsHoldingsAccount(SensorEntity):
    """Sensor for a FinTS holdings account."""

    def __init__(
        self,
        client: FinTsClient,
        account: SEPAAccount,
        name: str,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        self._client = client
        self._attr_name = name
        self._account = account
        self._holdings: list[Any] = []
        self._config_entry = config_entry
        account_identifier = (
            getattr(account, "accountnumber", None) or self._client.name
        )
        unique_id = f"{self._client.name}_{account_identifier}_holdings"
        if self._config_entry:
            unique_id = f"{self._config_entry.entry_id}_{unique_id}"
        self._attr_unique_id = unique_id
        self._attr_icon = ICON
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_extra_state_attributes = {
            ATTR_ACCOUNT: getattr(account, "accountnumber", None),
            ATTR_ACCOUNT_TYPE: "holdings",
        }
        if self._client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = self._client.name
        self._attr_extra_state_attributes["account_number"] = getattr(
            account, "accountnumber", None
        )
        self._attr_extra_state_attributes["iban"] = getattr(account, "iban", None)
        self._attr_extra_state_attributes["bic"] = getattr(account, "bic", None)

    @property
    def device_info(self):
        info = {
            "identifiers": {(DOMAIN, self._client.name)},
            "name": f"FinTS - {self._client.name}",
            "manufacturer": "FinTS",
            "model": "Bank Account",
        }
        if self._config_entry is not None:
            info["config_entry_id"] = self._config_entry.entry_id
        return info

    def update(self) -> None:
        """Get the current holdings for the account."""
        try:
            bank = self._client.client
            with bank:
                self._holdings = bank.get_holdings(self._account)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error updating holdings for %s: %s – re-authentication may be needed",
                self.name,
                err,
            )
            self._attr_available = False
            if self._config_entry is not None:
                self._config_entry.async_start_reauth(self.hass)
            return

        self._attr_available = True
        total = sum(
            h.total_value
            for h in self._holdings
            if getattr(h, "total_value", None) is not None
        )
        self._attr_native_value = total if total else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional attributes of the sensor."""
        attributes = {
            ATTR_ACCOUNT: getattr(self._account, "accountnumber", None),
            ATTR_ACCOUNT_TYPE: "holdings",
        }
        if self._client.name:
            attributes[ATTR_BANK] = self._client.name
        for holding in self._holdings:
            if holding.name:
                attributes[f"{holding.name} total"] = getattr(
                    holding, "total_value", None
                )
                attributes[f"{holding.name} pieces"] = getattr(holding, "pieces", None)
                attributes[f"{holding.name} price"] = getattr(
                    holding, "market_value", None
                )
        return attributes
