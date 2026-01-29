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
    """Set up the sensors.

    Login to the bank and get a list of existing accounts. Create a
    sensor for each account.
    """
    credentials = BankCredentials(
        config[CONF_BIN],
        config[CONF_USERNAME],
        config[CONF_PIN],
        config[CONF_URL],
        config.get(CONF_PRODUCT_ID),
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
        client, fints_name, account_config, holdings_config, balance_accounts, holdings_accounts
    )

    add_entities(accounts, True)

def _create_entities(
    client: FinTsClient,
    fints_name: str,
    account_config: dict[str, str | None],
    holdings_config: dict[str, str | None],
    balance_accounts: list[SEPAAccount],
    holdings_accounts: list[SEPAAccount],
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
        accounts.append(FinTsAccount(client, account, account_name))
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
        accounts.append(FinTsHoldingsAccount(client, account, account_name))
        _LOGGER.debug(
            "Creating holdings %s for bank %s", account.accountnumber, fints_name
        )

    return accounts


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FinTS from a config entry."""

    data = entry.data
    credentials = BankCredentials(
        data[CONF_BIN],
        data[CONF_USERNAME],
        data[CONF_PIN],
        data[CONF_URL],
        data.get(CONF_PRODUCT_ID),
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
    balance_accounts, holdings_accounts = await hass.async_add_executor_job(
        client.detect_accounts
    )
    accounts = _create_entities(
        client, fints_name, account_config, holdings_config, balance_accounts, holdings_accounts
    )

    async_add_entities(accounts, True)


class FinTsAccount(SensorEntity):
    """Sensor for a FinTS balance account.

    A balance account contains an amount of money (=balance). The amount may
    also be negative.
    """

    def __init__(self, client: FinTsClient, account, name: str) -> None:
        """Initialize a FinTs balance account."""
        self._client = client
        self._account = account
        self._attr_name = name
        self._attr_icon = ICON
        self._attr_extra_state_attributes = {
            ATTR_ACCOUNT: self._account.iban,
            ATTR_ACCOUNT_TYPE: "balance",
        }
        if self._client.name:
            self._attr_extra_state_attributes[ATTR_BANK] = self._client.name

    def update(self) -> None:
        """Get the current balance and currency for the account."""
        bank = self._client.client
        balance = bank.get_balance(self._account)
        self._attr_native_value = balance.amount.amount
        self._attr_native_unit_of_measurement = balance.amount.currency
        _LOGGER.debug("updated balance of account %s", self.name)


class FinTsHoldingsAccount(SensorEntity):
    """Sensor for a FinTS holdings account.

    A holdings account does not contain money but rather some financial
    instruments, e.g. stocks.
    """

    def __init__(self, client: FinTsClient, account, name: str) -> None:
        """Initialize a FinTs holdings account."""
        self._client = client
        self._attr_name = name
        self._account = account
        self._holdings: list[Any] = []
        self._attr_icon = ICON
        self._attr_native_unit_of_measurement = "EUR"

    def update(self) -> None:
        """Get the current holdings for the account."""
        bank = self._client.client
        self._holdings = bank.get_holdings(self._account)
        self._attr_native_value = sum(h.total_value for h in self._holdings)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional attributes of the sensor.

        Lists each holding of the account with the current value.
        """
        attributes = {
            ATTR_ACCOUNT: self._account.accountnumber,
            ATTR_ACCOUNT_TYPE: "holdings",
        }
        if self._client.name:
            attributes[ATTR_BANK] = self._client.name
        for holding in self._holdings:
            total_name = f"{holding.name} total"
            attributes[total_name] = holding.total_value
            pieces_name = f"{holding.name} pieces"
            attributes[pieces_name] = holding.pieces
            price_name = f"{holding.name} price"
            attributes[price_name] = holding.market_value

        return attributes
