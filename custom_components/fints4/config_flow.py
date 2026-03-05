"""FinTS config flow."""

from __future__ import annotations

import logging
from typing import Any

from fints.client import FinTS3PinTanClient, NeedTANResponse
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_BIN, CONF_PRODUCT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_TAN = "tan"


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


def _auto_bootstrap(client: FinTS3PinTanClient, preferred_tan_code: str = "921") -> None:
    """Non-interactively configure TAN mechanism.

    Replaces minimal_interactive_cli_bootstrap for use in a non-interactive context.
    Automatically selects pushTAN (921) if available, otherwise the first non-999 method.
    """
    if client.get_current_tan_mechanism():
        return  # Already set (from persistent state / system_id)

    client.fetch_tan_mechanisms()
    mechanisms = client.get_tan_mechanisms()
    if not mechanisms:
        _LOGGER.warning("Bank returned no TAN mechanisms")
        return

    if preferred_tan_code in mechanisms:
        selected = preferred_tan_code
    else:
        # Fall back to first mechanism that is not the single-step TAN (999)
        selected = next(
            (k for k in mechanisms if k != "999"),
            list(mechanisms.keys())[0],
        )

    _LOGGER.info(
        "Auto-selecting TAN mechanism: %s (%s)", selected, mechanisms[selected].name
    )
    client.set_tan_mechanism(selected)

    # Some banks require choosing a TAN medium (e.g. which phone gets the pushTAN)
    if client.selected_tan_medium is None and client.is_tan_media_required():
        try:
            media_result = client.get_tan_media()
            media_list = media_result[1] if len(media_result) > 1 else []
            if media_list:
                client.set_tan_medium(media_list[0])
                _LOGGER.info("Auto-selected TAN medium: %s", media_list[0])
            else:
                # Workaround for banks (e.g. Sparkasse) that return 3955 but accept ""
                client.selected_tan_medium = ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not fetch TAN media (continuing anyway): %s", exc)


class FinTSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle FinTS config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._user_input: dict[str, Any] = {}
        self._raw_client: FinTS3PinTanClient | None = None
        self._dialog_data: Any = None
        self._tan_request: NeedTANResponse | None = None
        self._tan_challenge: str = ""
        self._tan_decoupled: bool = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: connect to the bank."""
        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_build_user_data_schema()
            )

        self._user_input = user_input

        def _connect() -> tuple:
            """Connect to bank, run bootstrap, check for TAN requirement."""
            client = FinTS3PinTanClient(
                user_input[CONF_BIN],
                user_input[CONF_USERNAME],
                user_input[CONF_PIN],
                user_input[CONF_URL],
                product_id=user_input.get(CONF_PRODUCT_ID) or None,
            )
            _auto_bootstrap(client)

            with client:
                if isinstance(client.init_tan_response, NeedTANResponse):
                    # A TAN is required for login (common with pushTAN / decoupled)
                    tan_request = client.init_tan_response
                    dialog_data = client.pause_dialog()
                    # Exiting `with client:` here does NOT end the dialog (it is paused)
                    return "need_tan", client, tan_request, dialog_data

                # No TAN needed – already authenticated
                system_id = getattr(client, "system_id", None)
                return "ok", system_id

        try:
            result = await self.hass.async_add_executor_job(_connect)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error connecting to bank: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(user_input),
                errors=errors,
            )

        if result[0] == "need_tan":
            _, self._raw_client, self._tan_request, self._dialog_data = result
            self._tan_challenge = getattr(self._tan_request, "challenge", "") or ""
            self._tan_decoupled = bool(getattr(self._tan_request, "decoupled", False))
            _LOGGER.info(
                "TAN required (decoupled=%s). Challenge: %s",
                self._tan_decoupled,
                self._tan_challenge,
            )
            return await self.async_step_confirm_tan()

        _, system_id = result
        return await self._async_create_entry(system_id)

    async def async_step_confirm_tan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show TAN challenge; user confirms on phone (pushTAN) or enters a TAN code."""
        errors: dict[str, str] = {}

        if not self._raw_client:
            # Session lost (e.g. HA restart during flow) – restart from scratch
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(self._user_input),
                errors={"base": "unknown"},
            )

        if user_input is not None:
            # For decoupled (pushTAN) the TAN value is ignored by the bank; send "".
            # For all other methods the user has typed in the TAN code.
            tan_value = "" if self._tan_decoupled else user_input.get(CONF_TAN, "").strip()

            raw_client = self._raw_client
            tan_request = self._tan_request
            dialog_data = self._dialog_data

            def _send_tan() -> tuple:
                """Resume the paused dialog and send the TAN."""
                with raw_client.resume_dialog(dialog_data):
                    result = raw_client.send_tan(tan_request, tan_value)

                    if isinstance(result, NeedTANResponse):
                        # Decoupled: app not yet confirmed; or wrong TAN entered.
                        # Pause the dialog again so we can retry.
                        new_dialog_data = raw_client.pause_dialog()
                        return "need_tan_again", result, new_dialog_data

                    system_id = getattr(raw_client, "system_id", None)
                    return "ok", system_id

            try:
                result = await self.hass.async_add_executor_job(_send_tan)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Error sending TAN: %s", err)
                errors["base"] = "tan_failed"
            else:
                if result[0] == "need_tan_again":
                    _, new_tan_request, new_dialog_data = result
                    self._tan_request = new_tan_request
                    self._dialog_data = new_dialog_data
                    errors["base"] = "not_confirmed_yet"
                else:
                    _, system_id = result
                    return await self._async_create_entry(system_id)

        # Build form: decoupled needs no input (just a confirm button)
        if self._tan_decoupled:
            schema = vol.Schema({})
        else:
            schema = vol.Schema({vol.Optional(CONF_TAN, default=""): str})

        challenge = self._tan_challenge or "Bitte in der Banking-App bestätigen."
        return self.async_show_form(
            step_id="confirm_tan",
            data_schema=schema,
            description_placeholders={"challenge": challenge},
            errors=errors,
        )

    async def _async_create_entry(self, system_id: str | None) -> FlowResult:
        """Create the config entry once authentication is complete."""
        user_input = self._user_input
        await self.async_set_unique_id(
            f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
        )
        self._abort_if_unique_id_configured()

        entry_data = dict(user_input)
        if system_id:
            entry_data["system_id"] = system_id
            _LOGGER.info("Saving system_id: %s", system_id)

        return self.async_create_entry(
            title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
            data=entry_data,
        )
