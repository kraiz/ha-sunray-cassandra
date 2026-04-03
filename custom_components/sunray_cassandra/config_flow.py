"""Config flow for Sunray / CaSSAndRA integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CASSANDRA_URL,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_SERVER_NAME,
    CONF_USE_HA_MQTT,
    DEFAULT_CASSANDRA_PORT,
    DEFAULT_MQTT_PORT,
    DEFAULT_SERVER_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _ha_mqtt_available(hass: HomeAssistant) -> bool:
    """Return True if the HA MQTT integration is loaded."""
    return hass.config_entries.async_entries("mqtt") != []


class SunrayCassandraConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Sunray / CaSSAndRA."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise."""
        self._use_ha_mqtt: bool = True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1 – choose connection mode."""
        ha_mqtt_ready = _ha_mqtt_available(self.hass)

        if user_input is not None:
            self._use_ha_mqtt = user_input[CONF_USE_HA_MQTT]
            if self._use_ha_mqtt:
                return await self.async_step_server_name()
            return await self.async_step_mqtt_broker()

        schema = vol.Schema(
            {
                vol.Required(CONF_USE_HA_MQTT, default=ha_mqtt_ready): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "ha_mqtt_note": (
                    "Home Assistant MQTT integration detected."
                    if ha_mqtt_ready
                    else "No HA MQTT integration found — you can still provide broker details below."
                )
            },
        )

    async def async_step_mqtt_broker(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2a – custom MQTT broker details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._mqtt_data = user_input
            return await self.async_step_server_name()

        schema = vol.Schema(
            {
                vol.Required(CONF_MQTT_BROKER): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_MQTT_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_MQTT_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="mqtt_broker",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_server_name(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2b / 3 – CaSSAndRA server name + optional HTTP URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            server_name = user_input[CONF_SERVER_NAME].strip()
            if not server_name:
                errors[CONF_SERVER_NAME] = "empty_server_name"
            else:
                await self.async_set_unique_id(server_name)
                self._abort_if_unique_id_configured()

                data: dict[str, Any] = {
                    CONF_SERVER_NAME: server_name,
                    CONF_USE_HA_MQTT: self._use_ha_mqtt,
                    CONF_CASSANDRA_URL: user_input.get(CONF_CASSANDRA_URL, ""),
                }
                if not self._use_ha_mqtt:
                    data.update(getattr(self, "_mqtt_data", {}))

                return self.async_create_entry(
                    title=f"CaSSAndRA – {server_name}",
                    data=data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER_NAME, default=DEFAULT_SERVER_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(
                    CONF_CASSANDRA_URL,
                    description={"suggested_value": f"http://cassandra.local:{DEFAULT_CASSANDRA_PORT}"},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            }
        )
        return self.async_show_form(
            step_id="server_name",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return SunrayCassandraOptionsFlow(config_entry)


class SunrayCassandraOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the Sunray / CaSSAndRA integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CASSANDRA_URL,
                    default=self.config_entry.data.get(CONF_CASSANDRA_URL, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)




