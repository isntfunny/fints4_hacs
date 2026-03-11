"""Constants for the FinTS4 integration."""

DOMAIN = "fints4"

CONF_BIN = "bank_identification_number"
CONF_PRODUCT_ID = "product_id"
CONF_ACCOUNTS = "accounts"
CONF_HOLDINGS = "holdings"
CONF_ACCOUNT = "account"
CONF_UPDATE_INTERVAL = "update_interval"

DEFAULT_UPDATE_INTERVAL = 240  # minutes
TRANSACTION_LOOKBACK_DAYS = 14

ATTR_BANK = "bank"
ATTR_ACCOUNT_TYPE = "account_type"

ACCOUNT_TYPE_BALANCE = "balance"
ACCOUNT_TYPE_AVAILABLE_BALANCE = "available_balance"
ACCOUNT_TYPE_UPCOMING_TRANSACTIONS = "upcoming_transactions"
ACCOUNT_TYPE_HOLDINGS = "holdings"

