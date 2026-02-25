"""FinTS config flow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fints.client import NeedTANResponse
from fints.exceptions import FinTSClientError
from fints.utils import minimal_interactive_cli_bootstrap
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
    _tan_sent: bool = False

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
            None,
        )

        client = FinTsClient(
            credentials,
            user_input.get(CONF_NAME) or user_input[CONF_BIN],
            {},
            {},
        )

        client = FinTsClient(
            credentials,
            user_input.get(CONF_NAME) or user_input[CONF_BIN],
            {},
            {},
        )

        try:
            await self.hass.async_add_executor_job(
                minimal_interactive_cli_bootstrap, client.client
            )
            _LOGGER.info("Bootstrap completed")
        except Exception as e:
            _LOGGER.warning("Error during bootstrap: %s", e)

        self._pending_client = client

        def try_get_accounts():
            with client.client:
                return client.client.get_sepa_accounts()

        try:
            accounts = await self.hass.async_add_executor_job(try_get_accounts)
        except Exception as err:  # noqa: BLE001
            err_str = str(err)
            if "NeedTANResponse" in type(err).__name__ or "NeedTANResponse" in err_str:
                _LOGGER.info("TAN required, showing confirm button")
                return await self.async_step_confirm_tan()

            _LOGGER.exception("Error connecting to bank: %s", err)
            errors["base"] = "cannot_connect"
            self._user_input = user_input
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(user_input),
                errors=errors,
            )

        if accounts:
            system_id = getattr(client.client, "system_id", None)
            await self.async_set_unique_id(
                f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            entry_data = {**user_input}
            if system_id:
                entry_data["system_id"] = system_id
            return self.async_create_entry(
                title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
                data=entry_data,
            )

        errors["base"] = "cannot_connect"
        self._user_input = user_input
        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_data_schema(user_input),
            errors=errors,
        )

    async def async_step_confirm_tan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show a button to confirm after TAN is accepted."""
        if user_input is not None:
            return await self.async_step_tan_confirmed()

        return self.async_show_form(
            step_id="confirm_tan",
            data_schema=vol.Schema({}),
            description_placeholders={
                "message": "Bitte bestätige die pushTAN auf deinem Smartphone und klicke dann auf 'Weiter'."
            },
        )

    async def async_step_tan_confirmed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Try to get accounts after user confirmed TAN."""
        if not self._pending_client:
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(self._user_input),
                errors={"base": "unknown"},
            )

        def try_get_accounts():
            with self._pending_client.client:
                return self._pending_client.client.get_sepa_accounts()

        try:
            accounts = await self.hass.async_add_executor_job(try_get_accounts)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error after TAN confirm: %s", err)
            return await self.async_step_confirm_tan()

        if accounts:
            system_id = getattr(self._pending_client.client, "system_id", None)
            assert self._user_input is not None
            await self.async_set_unique_id(
                f"{self._user_input[CONF_BIN]}-{self._user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            entry_data = {**self._user_input}
            if system_id:
                entry_data["system_id"] = system_id
            return self.async_create_entry(
                title=self._user_input.get(CONF_NAME) or self._user_input[CONF_BIN],
                data=entry_data,
            )

        return await self.async_step_confirm_tan()

    async def _handle_tan_challenge(
        self,
        user_input: dict[str, Any],
        client: FinTsClient,
        challenge: NeedTANResponse,
        dialog_data: Any = None,
    ) -> None:
        """Store state so we can finish after pushTAN."""
        self._pending_client = client
        self._tan_request = challenge
        if dialog_data is not None:
            self._dialog_data = dialog_data
        else:
            self._dialog_data = client.client.pause_dialog()
        self._user_input = user_input
        self._tan_task = None
        self._tan_error = False
        self._tan_sent = False

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
            progress_task=self._tan_task,
        )

    async def _wait_for_tan(self) -> None:
        """Background task that polls for the pushTAN completion."""

        for _ in range(6):
            await asyncio.sleep(10)
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
        ):
            _LOGGER.info("Missing client or tan_request")
            return False

        try:
            with self._pending_client.client:
                if self._tan_request.decoupled:
                    import time
                    if not self._tan_sent:
                        _LOGGER.info("Decoupled TAN - waiting 30s and sending empty TAN...")
                        time.sleep(30)
                        self._tan_sent = True

                    _LOGGER.info("Sending empty TAN for decoupled...")
                    self._pending_client.client.send_tan(self._tan_request, "")
                else:
                    self._pending_client.client.send_tan(self._tan_request, "")

                _LOGGER.info("Checking client.init_tan_response...")
                if isinstance(self._pending_client.client.init_tan_response, NeedTANResponse):
                    _LOGGER.info("TAN still needed: %s", self._pending_client.client.init_tan_response)
                    self._tan_request = self._pending_client.client.init_tan_response
                    return False

                _LOGGER.info("TAN confirmed - getting accounts...")
                accounts = self._pending_client.client.get_sepa_accounts()
                if accounts:
                    _LOGGER.info("Got %d accounts - success!", len(accounts))
                    return True

                _LOGGER.info("No accounts returned")
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

        system_id = None
        if self._pending_client:
            system_id = getattr(self._pending_client.client, "system_id", None)

        entry_data = {**self._user_input}
        if system_id:
            entry_data["system_id"] = system_id
            _LOGGER.info("Saving system_id: %s", system_id)

        return self.async_create_entry(
            title=self._user_input.get(CONF_NAME) or self._user_input[CONF_BIN],
            data=entry_data,
        )
