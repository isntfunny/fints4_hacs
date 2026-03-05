"""FinTS config flow."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from fints.client import FinTS3PinTanClient, NeedTANResponse
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .client import auto_bootstrap
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



def _build_fints_client(user_input: dict[str, Any]) -> FinTS3PinTanClient:
    """Construct a FinTS3PinTanClient from user_input / stored entry data.

    For reauth the stored system_id is passed so the bank can avoid
    issuing a new TAN if the session is still valid.
    """
    return FinTS3PinTanClient(
        user_input[CONF_BIN],
        user_input[CONF_USERNAME],
        user_input[CONF_PIN],
        user_input[CONF_URL],
        product_id=user_input.get(CONF_PRODUCT_ID) or None,
        # Reuse stored system_id so the bank does not require a new TAN
        # on every login.  On first setup this is None.
        system_id=user_input.get("system_id") or None,
    )


def _connect(user_input: dict[str, Any]) -> tuple:
    """Open a FinTS dialog and return (status, ...) – runs in a thread.

    Returns one of:
      ("need_tan", client, tan_request, dialog_data)
      ("ok", system_id)
    Raises on unrecoverable errors (caught by the caller).
    """
    client = _build_fints_client(user_input)
    auto_bootstrap(client)

    with client:
        if isinstance(client.init_tan_response, NeedTANResponse):
            # A TAN is required for login (common with pushTAN / decoupled).
            # Pause the dialog so we can resume it after the user confirms.
            tan_request = client.init_tan_response
            dialog_data = client.pause_dialog()
            # Exiting `with client:` here does NOT close the dialog (it is paused).
            return "need_tan", client, tan_request, dialog_data

        # No TAN needed – already authenticated (system_id reused or bank
        # does not require 2FA for login).
        system_id = getattr(client, "system_id", None)
        return "ok", system_id


class FinTSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle FinTS config flow.

    Supports:
      - Initial setup   (async_step_user)
      - TAN / pushTAN   (async_step_confirm_tan) – shared between setup & reauth
      - Re-authentication (async_step_reauth / async_step_reauth_confirm)
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._user_input: dict[str, Any] = {}
        self._raw_client: FinTS3PinTanClient | None = None
        self._dialog_data: Any = None
        self._tan_request: NeedTANResponse | None = None
        self._tan_challenge: str = ""
        self._tan_decoupled: bool = False
        # Set to the existing ConfigEntry when running a re-auth flow
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: collect credentials and connect."""
        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_build_user_data_schema()
            )

        self._user_input = user_input

        return await self._async_connect_and_handle(
            on_error=lambda: self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(user_input),
                errors={"base": "cannot_connect"},
            )
        )

    # ------------------------------------------------------------------
    # Re-authentication (triggered by HA when the integration signals it)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> FlowResult:
        """Called by HA when the config entry needs re-authentication.

        Fetch the existing entry so we can update it later without asking
        the user to re-enter BLZ, URL, username, etc.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-auth step: only the PIN is re-entered (all other data is kept).

        The existing system_id is passed to the bank so it can skip issuing
        a new TAN if the session is still valid.  If the 60-day period has
        passed the bank will issue a new pushTAN challenge as usual.
        """
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            # Merge stored entry data with the (possibly updated) PIN
            self._user_input = {
                **self._reauth_entry.data,
                CONF_PIN: user_input[CONF_PIN],
            }
            return await self._async_connect_and_handle(
                on_error=lambda: self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=vol.Schema(
                        {vol.Required(CONF_PIN): str}
                    ),
                    description_placeholders={"name": self._reauth_entry.title},
                    errors={"base": "cannot_connect"},
                )
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
            description_placeholders={"name": self._reauth_entry.title},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # TAN confirmation – shared between initial setup and re-auth
    # ------------------------------------------------------------------

    async def async_step_confirm_tan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show TAN challenge; user confirms on phone (pushTAN) or enters a TAN code."""
        errors: dict[str, str] = {}

        if not self._raw_client:
            # Session lost (e.g. HA restart during flow) – restart from scratch
            if self._reauth_entry:
                return await self.async_step_reauth_confirm()
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_data_schema(self._user_input),
                errors={"base": "unknown"},
            )

        if user_input is not None:
            # Decoupled (pushTAN): bank ignores the TAN value – always send "".
            # Other methods: user has typed the TAN code into the form.
            tan_value = (
                "" if self._tan_decoupled else user_input.get(CONF_TAN, "").strip()
            )

            raw_client = self._raw_client
            tan_request = self._tan_request
            dialog_data = self._dialog_data

            def _send_tan() -> tuple:
                """Resume the paused dialog and send the TAN (runs in a thread)."""
                with raw_client.resume_dialog(dialog_data):
                    result = raw_client.send_tan(tan_request, tan_value)

                    if isinstance(result, NeedTANResponse):
                        # Decoupled: app not yet confirmed (or wrong TAN).
                        # Pause again so the user can retry.
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
                    return await self._async_finish(system_id)

        # Build form schema: decoupled = just a confirm button; others = TAN input
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

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _async_connect_and_handle(self, *, on_error) -> FlowResult:
        """Run _connect() in a thread and route to the correct next step."""
        try:
            result = await self.hass.async_add_executor_job(
                _connect, self._user_input
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error connecting to bank: %s", err)
            return on_error()

        if result[0] == "need_tan":
            _, self._raw_client, self._tan_request, self._dialog_data = result
            self._tan_challenge = getattr(self._tan_request, "challenge", "") or ""
            self._tan_decoupled = bool(
                getattr(self._tan_request, "decoupled", False)
            )
            _LOGGER.info(
                "TAN required (decoupled=%s). Challenge: %s",
                self._tan_decoupled,
                self._tan_challenge,
            )
            return await self.async_step_confirm_tan()

        _, system_id = result
        return await self._async_finish(system_id)

    async def _async_finish(self, system_id: str | None) -> FlowResult:
        """Finalise the flow: create a new entry or update the existing one.

        For re-auth the existing entry is updated in-place (so all entity
        IDs, automations, etc. keep working) and the integration is reloaded.
        For initial setup a new entry is created as usual.
        """
        entry_data = dict(self._user_input)
        if system_id:
            entry_data["system_id"] = system_id
            _LOGGER.info("Saving system_id: %s", system_id)

        if self._reauth_entry:
            # Re-authentication: update the existing entry and reload
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={**self._reauth_entry.data, **entry_data},
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # Initial setup: create a new entry
        user_input = self._user_input
        await self.async_set_unique_id(
            f"{user_input[CONF_BIN]}-{user_input[CONF_USERNAME]}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=user_input.get(CONF_NAME) or user_input[CONF_BIN],
            data=entry_data,
        )
