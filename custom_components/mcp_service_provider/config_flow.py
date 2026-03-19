"""Config flow for MCP Service Provider integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import json

from .const import DOMAIN, CONF_URL, CONF_HEADERS, CONF_QUERY_PARAMS

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_HEADERS, default="{}"): str,
        vol.Optional(CONF_QUERY_PARAMS, default="{}"): str,
    }
)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    # We should add logic here to test the connection later
    try:
        json.loads(data.get(CONF_HEADERS, "{}"))
        json.loads(data.get(CONF_QUERY_PARAMS, "{}"))
    except ValueError as err:
        raise ValueError("Headers and Query Params must be valid JSON strings.") from err

    return {"title": data[CONF_URL]}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MCP Service Provider."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception %s", err)
                errors["base"] = "invalid_input"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except Exception as err:
                _LOGGER.exception("Unexpected exception %s", err)
                errors["base"] = "invalid_input"
            else:
                return self.async_update_reload_and_abort(
                    entry=entry,
                    data=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, entry.data
            ),
            errors=errors,
        )
