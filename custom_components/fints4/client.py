"""FinTS client wrapper and shared models."""

from __future__ import annotations

from collections import namedtuple
import logging
from typing import Any

from fints.client import FinTS3PinTanClient
from fints.models import SEPAAccount

_LOGGER = logging.getLogger(__name__)

BankCredentials = namedtuple("BankCredentials", "blz login pin url product_id system_id")  # noqa: PYI024

PREFERRED_TAN_CODE = "921"  # pushTAN (decoupled)


def auto_bootstrap(client: FinTS3PinTanClient, preferred_tan_code: str = PREFERRED_TAN_CODE) -> None:
    """Non-interactively configure TAN mechanism on a fresh client.

    Must be called after FinTS3PinTanClient() and before opening a dialog
    (``with client:``).  The FinTS library fetches BPD during __init__, so
    mechanism data is already available without an extra dialog.

    Selects pushTAN (921) if available, otherwise the first non-999 method.
    """
    if client.get_current_tan_mechanism():
        return  # Already set (e.g. restored from system_id session state)

    client.fetch_tan_mechanisms()
    mechanisms = client.get_tan_mechanisms()
    if not mechanisms:
        _LOGGER.warning("Bank returned no TAN mechanisms")
        return

    if preferred_tan_code in mechanisms:
        selected = preferred_tan_code
    else:
        selected = next(
            (k for k in mechanisms if k != "999"),
            list(mechanisms.keys())[0],
        )

    _LOGGER.info(
        "Auto-selecting TAN mechanism: %s (%s)", selected, mechanisms[selected].name
    )
    client.set_tan_mechanism(selected)

    # Some banks require choosing a TAN medium (which phone gets the pushTAN).
    if client.selected_tan_medium is None and client.is_tan_media_required():
        try:
            media_result = client.get_tan_media()
            media_list = media_result[1] if len(media_result) > 1 else []
            if media_list:
                client.set_tan_medium(media_list[0])
                _LOGGER.info("Auto-selected TAN medium: %s", media_list[0])
            else:
                # Workaround: banks (e.g. Sparkasse) that return HKTAM required
                # but accept an empty medium string.
                client.selected_tan_medium = ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not fetch TAN media (continuing anyway): %s", exc)


class FinTsClient:
    """Wrapper around the FinTS3PinTanClient.

    The FinTS library persists the current dialog with the bank and stores bank
    capabilities. So caching the client is beneficial.
    """

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
        """Get (and cache) the FinTS client object.

        auto_bootstrap() is called once after creation so the correct TAN
        mechanism (pushTAN 921) is selected before any dialog is opened.
        Without this the bank rejects the dialog with an auth error.
        """
        if self._client is None:
            self._client = FinTS3PinTanClient(
                self._credentials.blz,
                self._credentials.login,
                self._credentials.pin,
                self._credentials.url,
                product_id=self._credentials.product_id,
                system_id=self._credentials.system_id,
            )
            auto_bootstrap(self._client)
        return self._client

    @property
    def system_id(self) -> str | None:
        """Get the system ID from the client."""
        if self._client:
            return getattr(self._client, "system_id", None)
        return None

    def get_account_information(self, iban: str) -> dict[str, Any] | None:
        """Get account information for an IBAN, if available (uses cached data)."""
        return self._account_information.get(iban)

    def is_balance_account(self, account: SEPAAccount) -> bool:
        """Determine if the given account is of type balance account."""
        if not account.iban:
            return False

        account_information = self.get_account_information(account.iban)
        if not account_information:
            # get_information() failed or account not yet indexed.
            # Fall back: include when no explicit account filter is configured.
            return not self.account_config

        if account_type := account_information.get("type"):
            return 1 <= account_type <= 9

        if (
            account_information.get("iban") in self.account_config
            or account_information.get("account_number") in self.account_config
        ):
            return True

        # Type info present but outside 1-9 range and not in explicit config.
        return not self.account_config

    def is_holdings_account(self, account: SEPAAccount) -> bool:
        """Determine if the given account is of type holdings account.

        German depot (Wertpapierdepot) accounts typically have no IBAN –
        only an account number – so we must not short-circuit on missing IBAN.
        """
        iban = account.iban
        account_number = getattr(account, "accountnumber", None)

        # Look up type info by IBAN (available for most accounts)
        account_information = self.get_account_information(iban) if iban else None

        if not account_information:
            # No type info available.
            if self.holdings_config:
                # Explicit filter: include only if account number is listed
                return bool(account_number and account_number in self.holdings_config)
            # Auto-detect: treat as holdings when there is an account number but no IBAN
            return bool(account_number and not iban)

        if account_type := account_information.get("type"):
            return 30 <= account_type <= 39

        if (
            account_information.get("iban") in self.holdings_config
            or account_information.get("account_number") in self.holdings_config
        ):
            return True

        return not self.holdings_config and not iban

    def detect_accounts(self) -> tuple[list[SEPAAccount], list[SEPAAccount]]:
        """Identify the accounts of the bank.

        Opens a single authenticated dialog to fetch both the account list and
        the account information (used for type classification).
        """
        balance_accounts: list[SEPAAccount] = []
        holdings_accounts: list[SEPAAccount] = []

        with self.client:
            sepa_accounts = self.client.get_sepa_accounts()

            # Pre-fetch account type information while the dialog is open
            if not self._account_information_fetched:
                try:
                    info = self.client.get_information()
                    self._account_information = {
                        account["iban"]: account
                        for account in info.get("accounts", [])
                        if account.get("iban")
                    }
                    self._account_information_fetched = True
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Could not fetch account information: %s", exc)

        for account in sepa_accounts:
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
