"""DataUpdateCoordinator for FinTS4 integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
import logging
from typing import Any

from fints.models import SEPAAccount

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import FinTsClient
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN, TRANSACTION_LOOKBACK_DAYS

_LOGGER = logging.getLogger(__name__)


def _tx_hash(tx: dict[str, Any]) -> str:
    """Generate a composite hash for a serialized transaction dict.

    Used for deduplication between polling cycles.
    """
    parts = [
        str(tx.get("date", "")),
        str(tx.get("amount", "")),
        str(tx.get("currency", "")),
        tx.get("bank_reference", "") or tx.get("end_to_end_reference", "") or "",
        tx.get("customer_reference", "") or "",
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()  # noqa: S324


def _serialize_tx(tx: Any) -> dict[str, Any]:
    """Serialize a transaction object into a plain dict for HA attributes/events."""
    d = tx.data if hasattr(tx, "data") else tx
    amount = d.get("amount")
    amount_value = float(getattr(amount, "amount", amount)) if amount else None
    currency = getattr(amount, "currency", d.get("currency")) if amount else None
    status = d.get("status", "")  # 'C' credit, 'D' debit
    direction = "incoming" if status == "C" else "outgoing" if status == "D" else "unknown"

    return {
        "date": str(d.get("date", "")),
        "entry_date": str(d.get("entry_date", "")),
        "amount": amount_value,
        "currency": currency,
        "direction": direction,
        "applicant_name": d.get("applicant_name", ""),
        "recipient_name": d.get("recipient_name", ""),
        "purpose": d.get("purpose", ""),
        "posting_text": d.get("posting_text", ""),
        "end_to_end_reference": d.get("end_to_end_reference", ""),
        "bank_reference": d.get("bank_reference", ""),
    }


def event_payload(tx: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of a serialized transaction used as HA event data."""
    return {
        "amount": tx.get("amount"),
        "currency": tx.get("currency"),
        "date": tx.get("date"),
        "direction": tx.get("direction"),
        "applicant_name": tx.get("applicant_name"),
        "recipient_name": tx.get("recipient_name"),
        "purpose": tx.get("purpose"),
        "end_to_end_reference": tx.get("end_to_end_reference"),
    }


def account_identifier(account: SEPAAccount, client_name: str) -> str:
    """Return the best available unique string for an account."""
    return account.iban or getattr(account, "accountnumber", None) or client_name


def get_account_device_info(
    entry: ConfigEntry,
    account: SEPAAccount,
    client_name: str,
) -> DeviceInfo:
    """Create device info — one device per account (IBAN or account number)."""
    iban = getattr(account, "iban", None)
    account_number = getattr(account, "accountnumber", None)
    identifier = iban or account_number or client_name
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{identifier}")},
        name=iban or account_number or "Unknown Account",
        manufacturer="FinTS",
        model="Bank Account" if iban else "Depot",
    )


@dataclass
class FinTsAccountData:
    """Data for a single balance account."""

    balance: Any | None = None
    booked_transactions: list[dict[str, Any]] = field(default_factory=list)
    pending_transactions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FinTsCoordinatorData:
    """Aggregated data from a single FinTS polling cycle."""

    accounts: dict[str, FinTsAccountData] = field(default_factory=dict)
    holdings: dict[str, list[Any]] = field(default_factory=dict)
    new_booked: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    new_pending: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class FinTsDataUpdateCoordinator(DataUpdateCoordinator[FinTsCoordinatorData]):
    """Coordinator that fetches all FinTS data in a single bank dialog."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: FinTsClient,
        balance_accounts: list[SEPAAccount],
        holdings_accounts: list[SEPAAccount],
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"FinTS {client.name}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.client = client
        self.balance_accounts = balance_accounts
        self.holdings_accounts = holdings_accounts

        # Deduplication state (in-memory, resets on HA restart)
        self._seen_booked_ids: dict[str, set[str]] = {}
        self._seen_pending_ids: dict[str, set[str]] = {}
        self._first_run = True

    async def _async_update_data(self) -> FinTsCoordinatorData:
        """Fetch data from the bank — runs _fetch_all in an executor thread."""
        try:
            data = await self.hass.async_add_executor_job(self._fetch_all)
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error communicating with bank: {err}") from err

        return data

    def _fetch_all(self) -> FinTsCoordinatorData:
        """Synchronous fetch: opens ONE dialog for all operations."""
        result = FinTsCoordinatorData()
        today = date.today()
        start_date = today - timedelta(days=TRANSACTION_LOOKBACK_DAYS)

        try:
            bank = self.client.client
            with bank:
                # --- Balance accounts: balance + transactions ---
                for account in self.balance_accounts:
                    iban = account.iban
                    if not iban:
                        continue

                    account_data = FinTsAccountData()

                    try:
                        account_data.balance = bank.get_balance(account)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning("Failed to get balance for %s: %s", iban, exc)

                    try:
                        raw_transactions = bank.get_transactions(
                            account,
                            start_date=start_date,
                            end_date=today,
                            include_pending=True,
                        )
                        booked, pending = self._split_transactions(raw_transactions)
                        account_data.booked_transactions = booked
                        account_data.pending_transactions = pending
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "Failed to get transactions for %s: %s", iban, exc
                        )

                    result.accounts[iban] = account_data

                    # Deduplication
                    new_booked, new_pending = self._deduplicate(
                        iban, account_data.booked_transactions, account_data.pending_transactions
                    )
                    result.new_booked[iban] = new_booked
                    result.new_pending[iban] = new_pending

                # --- Holdings accounts ---
                for account in self.holdings_accounts:
                    acc_nr = getattr(account, "accountnumber", None)
                    if not acc_nr:
                        continue
                    try:
                        result.holdings[acc_nr] = bank.get_holdings(account)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "Failed to get holdings for %s: %s", acc_nr, exc
                        )

        except Exception as err:
            err_str = str(err).lower()
            if "pin" in err_str or "auth" in err_str or "9931" in err_str or "9010" in err_str:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise

        if self._first_run:
            self._first_run = False
            # Suppress events on first run — dedup sets were just seeded
            for iban in result.new_booked:
                result.new_booked[iban] = []
                result.new_pending[iban] = []

        return result

    @staticmethod
    def _split_transactions(
        raw_transactions: list[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split raw transactions into booked and pending lists."""
        booked: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        for tx in raw_transactions:
            serialized = _serialize_tx(tx)
            d = tx.data if hasattr(tx, "data") else tx
            # Heuristic: pending transactions often lack entry_date
            if d.get("entry_date") is None:
                pending.append(serialized)
            else:
                booked.append(serialized)

        return booked, pending

    def _deduplicate(
        self,
        iban: str,
        booked: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Find new transactions since last poll. Seeds sets on first call per IBAN."""
        booked_ids = {_tx_hash(tx): tx for tx in booked}
        pending_ids = {_tx_hash(tx): tx for tx in pending}

        prev_booked = self._seen_booked_ids.get(iban, set())
        prev_pending = self._seen_pending_ids.get(iban, set())

        new_booked = [tx for tx_id, tx in booked_ids.items() if tx_id not in prev_booked]
        new_pending = [tx for tx_id, tx in pending_ids.items() if tx_id not in prev_pending]

        # Update seen sets (replaced wholesale — naturally bounded by 14-day window)
        self._seen_booked_ids[iban] = set(booked_ids.keys())
        self._seen_pending_ids[iban] = set(pending_ids.keys())

        return new_booked, new_pending
