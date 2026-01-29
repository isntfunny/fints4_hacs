"""FinTS config flow."""

from __future__ import annotations

import logging
from typing import Any

from fints.exceptions import FinTSClientError
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .client import BankCredentials, FinTsClient
from .const import CONF_BIN, CONF_PRODUCT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

def _build_user_data_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    defaults = user_input or {}

    def _required(key: str) -> vol.Required:
        if key in defaults:
            return vol.Required(key, default=defaults[key])
        return vol.Required(key)

    def _optional(key: str) -> vol.Optional:
        if key in defaults:
            return vol.Optional(key, default=defaults[key])
        return vol.Optional(key)

    return vol.Schema(
        {
            _required(CONF_BIN): str,
            _required(CONF_USERNAME): str,
            _required(CONF_PIN): str,
            _required(CONF_URL): str,
            _optional(CONF_NAME): str,
            _optional(CONF_PRODUCT_ID): str,
        }
    )


class FinTSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle FinTS config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""

        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_build_user_data_schema()
            )

        credentials = BankCredentials(
            user_input[CONF_BIN],
            user_input[CONF_USERNAME],
            user_input[CONF_PIN],
            user_input[CONF_URL],
            user_input.get(CONF_PRODUCT_ID),
        )

        client = FinTsClient(
            credentials,
            user_input.get(CONF_NAME) or user_input[CONF_BIN],
            {},
            {},
        )

        try:
            await self.hass.async_add_executor_job(client.detect_accounts)
        except FinTSClientError as err:
            _LOGGER.warning("FinTS validation failed: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during FinTS validation")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(
                f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_data_schema(user_input),
            errors=errors,
        )
