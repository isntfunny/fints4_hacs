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

        try:
            await self.hass.async_add_executor_job(
                minimal_interactive_cli_bootstrap, client.client
            )
            _LOGGER.info("Bootstrap completed")
        except Exception as e:
            _LOGGER.warning("Error during bootstrap: %s", e)

        tan_needed = False
        tan_challenge = None

        def check_tan_and_get_accounts():
            nonlocal tan_needed, tan_challenge
            _LOGGER.info("Opening dialog with client")
            with client.client:
                _LOGGER.info("Inside dialog, checking init_tan_response: %s", getattr(client.client, "init_tan_response", None))
                if hasattr(client.client, "init_tan_response") and isinstance(client.client.init_tan_response, NeedTANResponse):
                    tan_needed = True
                    tan_challenge = client.client.init_tan_response
                    _LOGGER.info("TAN needed detected: %s", tan_challenge)
                    dialog_data = client.client.pause_dialog()
                    _LOGGER.info("Dialog paused, returning dialog_data")
                    return ("tan", dialog_data)
                _LOGGER.info("Getting accounts...")
                return ("accounts", client.client.get_sepa_accounts())

        try:
            result = await self.hass.async_add_executor_job(check_tan_and_get_accounts)
            _LOGGER.info("Result from check_tan_and_get_accounts: %s, tan_needed: %s", result, tan_needed)
        except Exception as err:  # noqa: BLE001
            err_type = type(err).__name__
            err_str = str(err)
            _LOGGER.exception("Exception during FinTS: type=%s, msg=%s", err_type, err_str)
            if "NeedTANResponse" in err_type or "NeedTANResponse" in err_str:
                _LOGGER.info("TAN required (from exception): %s", err)
                tan_response = getattr(err, "response", None) or getattr(err, "tan_request", None)
                if tan_response:
                    def pause_and_handle():
                        with client.client:
                            return client.client.pause_dialog()
                    try:
                        dialog_data = await self.hass.async_add_executor_job(pause_and_handle)
                    except Exception as pd_err:
                        _LOGGER.warning("Could not pause dialog: %s", pd_err)
                        dialog_data = None
                    await self._handle_tan_challenge(user_input, client, tan_response, dialog_data)
                    return await self.async_step_wait_for_tan()
            errors["base"] = "unknown"
        else:
            if tan_needed and tan_challenge:
                dialog_data = result[1] if isinstance(result, tuple) else None
                await self._handle_tan_challenge(user_input, client, tan_challenge, dialog_data)
                return await self.async_step_wait_for_tan()

            if isinstance(result, tuple) and result[0] == "accounts":
                accounts = result[1]
                if isinstance(accounts, NeedTANResponse):
                    def pause_for_accounts():
                        with client.client:
                            return client.client.pause_dialog()
                    try:
                        dialog_data = await self.hass.async_add_executor_job(pause_for_accounts)
                    except Exception as pd_err:
                        _LOGGER.warning("Could not pause dialog for accounts: %s", pd_err)
                        dialog_data = None
                    await self._handle_tan_challenge(user_input, client, accounts, dialog_data)
                    return await self.async_step_wait_for_tan()

                if accounts:
                    await self.async_set_unique_id(
                        f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
                    )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
                        data=user_input,
                    )

            errors["base"] = "cannot_connect"

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
                if self._tan_request.decoupled and not self._tan_sent:
                    import time
                    _LOGGER.info("Decoupled TAN - sending empty TAN and waiting 30s...")
                    time.sleep(30)
                    self._tan_sent = True

                if self._tan_sent:
                    _LOGGER.info("TAN already sent, checking for accounts...")
                    try:
                        accounts = self._pending_client.client.get_sepa_accounts()
                        _LOGGER.info("Got accounts: %s", accounts)
                        if accounts:
                            _LOGGER.info("TAN confirmed successfully!")
                            return True
                    except Exception as e:
                        _LOGGER.info("Error getting accounts: %s", e)

                response = self._pending_client.client.send_tan(
                    self._tan_request, ""
                )
                _LOGGER.info("Response: %s (type: %s)", response, type(response).__name__)

                if isinstance(response, NeedTANResponse):
                    _LOGGER.info("TAN still needed: %s", response)
                    self._tan_request = response
                    self._dialog_data = self._pending_client.client.pause_dialog()
                    return False

                _LOGGER.info("TAN confirmed successfully!")
                return True
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
