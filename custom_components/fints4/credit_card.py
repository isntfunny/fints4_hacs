"""DKKKU/DIKKU credit card statement parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass
class FinTsAmount:
    """Small balance amount object compatible with mt940 balance objects."""

    amount: float
    currency: str


@dataclass
class FinTsBalance:
    """Small balance object compatible with existing balance sensors."""

    amount: FinTsAmount
    date: date | datetime | None = None


def _parse_german_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    value_str = str(value).strip()
    if not value_str:
        return None
    return float(value_str.replace(".", "").replace(",", "."))


def _parse_fints_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value_str = str(value)
    if len(value_str) == 8 and value_str.isdigit():
        return datetime.strptime(value_str, "%Y%m%d").date()
    return date.fromisoformat(value_str)


def _parse_fints_time(value: Any) -> time | None:
    if not value:
        return None
    if isinstance(value, time):
        return value
    value_str = str(value)
    if len(value_str) == 6 and value_str.isdigit():
        return datetime.strptime(value_str, "%H%M%S").time()
    return time.fromisoformat(value_str)


def _serialize_credit_card_tx(raw_tx: Any, account_key: str) -> dict[str, Any] | None:
    """Serialize one DKKKU credit-card transaction line from DIKKU."""
    if isinstance(raw_tx, bytes):
        raw_tx = raw_tx.decode("iso-8859-1")

    parts = str(raw_tx).split(":")
    if len(parts) < 11:
        _LOGGER.debug("Skipping malformed credit card transaction: %s", raw_tx)
        return None

    amount = _parse_german_float(parts[8])
    if amount is not None and parts[10] == "D":
        amount *= -1

    original_amount = _parse_german_float(parts[4])
    if original_amount is not None and parts[6] == "D":
        original_amount *= -1

    purpose = ""
    for part in parts[11:21]:
        text = part.strip()
        if text == "J":
            break
        purpose += text
        if purpose.endswith("Betrag?"):
            purpose = f"{purpose[:-7]} Betrag "
        elif purpose.endswith("?"):
            purpose = f"{purpose[:-1]} "
        else:
            break

    value_date = _parse_fints_date(parts[2])
    transaction_date = _parse_fints_date(parts[1])

    return {
        "date": str(value_date or ""),
        "entry_date": str(transaction_date or ""),
        "amount": amount,
        "currency": parts[9] or None,
        "direction": "incoming" if (amount or 0) >= 0 else "outgoing",
        "applicant_name": "",
        "recipient_name": "",
        "purpose": purpose or "Credit card transaction",
        "posting_text": "DKKKU",
        "end_to_end_reference": "",
        "bank_reference": "|".join(
            [
                "DKKKU",
                account_key,
                str(transaction_date or ""),
                str(value_date or ""),
                str(amount),
                parts[9],
                purpose,
            ]
        ),
        "original_currency": parts[5] or None,
        "original_amount": original_amount,
        "exchange_rate": _parse_german_float(parts[7]),
    }


def _balance_from_credit_card_segment(segment: Any) -> FinTsBalance | None:
    data = getattr(segment, "_additional_data", [])
    if len(data) < 3:
        return None

    balance_data = data[2]
    if not isinstance(balance_data, list | tuple) or len(balance_data) < 3:
        return None

    amount_data = balance_data[1]
    if not isinstance(amount_data, list | tuple) or len(amount_data) < 2:
        return None

    amount = _parse_german_float(amount_data[0])
    if amount is None:
        return None
    if balance_data[0] == "D":
        amount *= -1

    balance_date = _parse_fints_date(balance_data[2])
    if len(balance_data) > 3 and balance_data[3]:
        balance_time = _parse_fints_time(balance_data[3])
        if balance_date and balance_time:
            return FinTsBalance(
                FinTsAmount(amount, amount_data[1]),
                datetime.combine(balance_date, balance_time),
            )

    return FinTsBalance(FinTsAmount(amount, amount_data[1]), balance_date)


def serialize_credit_card_response(
    segments: list[Any], account_key: str
) -> tuple[FinTsBalance | None, list[dict[str, Any]]]:
    """Serialize DKKKU/DIKKU response segments into balance and transactions."""
    balance = None
    transactions: list[dict[str, Any]] = []

    for segment in segments or []:
        if balance is None:
            balance = _balance_from_credit_card_segment(segment)

        data = getattr(segment, "_additional_data", [])
        for raw_tx in data[5:]:
            tx = _serialize_credit_card_tx(raw_tx, account_key)
            if tx:
                transactions.append(tx)

    return balance, transactions
