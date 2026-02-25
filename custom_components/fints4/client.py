"""FinTS client wrapper and shared models."""

from __future__ import annotations

from collections import namedtuple
import logging
from typing import Any

from fints.client import FinTS3PinTanClient
from fints.models import SEPAAccount

_LOGGER = logging.getLogger(__name__)

BankCredentials = namedtuple("BankCredentials", "blz login pin url product_id system_id")  # noqa: PYI024


class FinTsClient:
    """Wrapper around the FinTS3PinTanClient.

    The FinTS library persists the current dialog with the bank and stores bank
    capabilities. So caching the client is beneficial.
    """

    PUSHTAN_CODE = "921"

    def __init__(
        self,
        credentials: BankCredentials,
        name: str,
        account_config: dict[str, str | None],
        holdings_config: dict[str, str | None],
    ) -> None:
        self._credentials = credentials
        self._account_information: dict[str, dict[str, Any]] = {}
        self._account_information_fetched = False
        self.name = name
        self.account_config = account_config
        self.holdings_config = holdings_config
        self._client: FinTS3PinTanClient | None = None

    @property
    def client(self) -> FinTS3PinTanClient:
        """Get (and cache) the FinTS client object."""
        if self._client is None:
            self._client = FinTS3PinTanClient(
                self._credentials.blz,
                self._credentials.login,
                self._credentials.pin,
                self._credentials.url,
                product_id=self._credentials.product_id,
                system_id=self._credentials.system_id,
            )
        return self._client

    def init_tan_mechanism(self) -> None:
        """Initialize TAN mechanism (pushTAN if available)."""
        if not self._client:
            return
        try:
            with self._client:
                mechanisms = self._client.get_tan_mechanisms()
                if not mechanisms:
                    _LOGGER.warning("No TAN mechanisms available")
                    return

                if self.PUSHTAN_CODE in mechanisms:
                    _LOGGER.info("Using pushTAN (%s)", self.PUSHTAN_CODE)
                    self._client.set_tan_mechanism(self.PUSHTAN_CODE)
                    if mechanisms[self.PUSHTAN_CODE].description_required:
                        try:
                            media = self._client.get_tan_media()
                            if media and len(media) > 1 and media[1]:
                                self._client.set_tan_medium(media[1][0])
                        except Exception as e:
                            _LOGGER.warning("Could not set TAN medium: %s", e)
                else:
                    _LOGGER.warning("pushTAN (921) not available, available: %s", list(mechanisms.keys()))
        except Exception as e:
            _LOGGER.warning("Could not initialize TAN mechanism: %s", e)

    @property
    def system_id(self) -> str | None:
        """Get the system ID from the client."""
        if self._client:
            return getattr(self._client, "system_id", None)
        return None

    def get_account_information(self, iban: str) -> dict[str, Any] | None:
        """Get account information for an IBAN, if available."""
        if not self._account_information_fetched:
            info = self.client.get_information()
            self._account_information = {
                account["iban"]: account for account in info.get("accounts", [])
            }
            self._account_information_fetched = True
        return self._account_information.get(iban)

    def is_balance_account(self, account: SEPAAccount) -> bool:
        """Determine if the given account is of type balance account."""
        if not account.iban:
            return False

        account_information = self.get_account_information(account.iban)
        if not account_information:
            return False

        if account_type := account_information.get("type"):
            return 1 <= account_type <= 9

        if (
            account_information.get("iban") in self.account_config
            or account_information.get("account_number") in self.account_config
        ):
            return True

        return False

    def is_holdings_account(self, account: SEPAAccount) -> bool:
        """Determine if the given account is of type holdings account."""
        if not account.iban:
            return False

        account_information = self.get_account_information(account.iban)
        if not account_information:
            return False

        if account_type := account_information.get("type"):
            return 30 <= account_type <= 39

        if (
            account_information.get("iban") in self.holdings_config
            or account_information.get("account_number") in self.holdings_config
        ):
            return True

        return False

    def detect_accounts(self) -> tuple[list[SEPAAccount], list[SEPAAccount]]:
        """Identify the accounts of the bank."""
        balance_accounts: list[SEPAAccount] = []
        holdings_accounts: list[SEPAAccount] = []

        for account in self.client.get_sepa_accounts():
            if self.is_balance_account(account):
                balance_accounts.append(account)
            elif self.is_holdings_account(account):
                holdings_accounts.append(account)
            else:
                _LOGGER.warning(
                    "Could not determine type of account %s from %s",
                    getattr(account, "iban", None),
                    getattr(self.client, "user_id", None),
                )

        return balance_accounts, holdings_accounts

