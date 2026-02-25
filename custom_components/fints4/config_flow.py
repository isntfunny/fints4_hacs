"""FinTS config flow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fints.client import NeedTANResponse
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
    _pending_client: FinTsClient | None = None
    _tan_request: NeedTANResponse | None = None
    _dialog_data: Any | None = None
    _user_input: dict[str, Any] | None = None
    _tan_task: asyncio.Task | None = None
    _tan_error: bool = False

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
            result = await self.hass.async_add_executor_job(
                client.client.get_sepa_accounts
            )
        except FinTSClientError as err:
            _LOGGER.warning("FinTS validation failed: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during FinTS validation")
            errors["base"] = "unknown"
        else:
            if isinstance(result, NeedTANResponse):
                await self._handle_tan_challenge(user_input, client, result)
                return await self.async_step_wait_for_tan()

            await self.async_set_unique_id(
                f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
                data=user_input,
            )

        if errors:
            self._user_input = user_input
        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_data_schema(user_input),
            errors=errors,
        )

    async def _handle_tan_challenge(
        self,
        user_input: dict[str, Any],
        client: FinTsClient,
        challenge: NeedTANResponse,
    ) -> None:
        """Store state so we can finish after pushTAN."""
        self._pending_client = client
        self._tan_request = challenge
        self._dialog_data = client.client.pause_dialog()
        self._user_input = user_input
        self._tan_task = None
        self._tan_error = False

    async def async_step_wait_for_tan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show progress while waiting for pushTAN confirmation."""

        if user_input:
            if self._tan_error:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_data_schema(self._user_input),
                    errors={"base": "tan_failed"},
                )
            return self.async_show_progress_done(next_step_id="tan_done")

        if self._tan_task is None or self._tan_task.done():
            self._tan_task = self.hass.async_create_task(self._wait_for_tan())

        description = "Bitte bestätige die pushTAN auf deinem Smartphone."
        return self.async_show_progress(
            step_id="wait_for_tan",
            progress_action="wait_for_tan",
            description_placeholders={"description": description},
        )

    async def _wait_for_tan(self) -> None:
        """Background task that polls for the pushTAN completion."""

        for _ in range(30):
            await asyncio.sleep(5)
            success = await self.hass.async_add_executor_job(self._send_pending_tan)
            if success:
                await self.hass.config_entries.flow.async_configure(
                    self.flow_id, user_input={}
                )
                return

        self._tan_error = True
        await self.hass.config_entries.flow.async_configure(
            self.flow_id, user_input={"error": "timeout"}
        )

    def _send_pending_tan(self) -> bool:
        """Attempt to send the pending pushTAN challenge."""

        if (
            not self._pending_client
            or not self._tan_request
            or not self._dialog_data
        ):
            return False

        try:
            with self._pending_client.client.resume_dialog(self._dialog_data):
                response = self._pending_client.client.send_tan(
                    self._tan_request, ""
                )
                if isinstance(response, NeedTANResponse):
                    self._tan_request = response
                    self._dialog_data = self._pending_client.client.pause_dialog()
                    return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error while sending pushTAN: %s", err)
            self._tan_error = True
            return False

        return True

    async def async_step_tan_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish the flow once the pushTAN is confirmed."""

        assert self._user_input is not None
        await self.async_set_unique_id(
            f"{self._user_input[CONF_BIN]}-{self._user_input[CONF_USERNAME]}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._user_input.get(CONF_NAME) or self._user_input[CONF_BIN],
            data=self._user_input,
        )
