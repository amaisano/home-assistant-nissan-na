"""Config flow for Nissan North America integration.

Patched flow: Home Assistant's OAuth2 callback view only forwards ``state``
and ``code`` to a config flow, so the ``user_id`` Smartcar appends to the
Connect redirect never reaches the integration (issue #39). Smartcar v3 API
calls also need the *API Credentials* client ID (``client_01...``), which is
not the Application ID that Smartcar Connect accepts.

So instead of an OAuth round trip, the vehicle is connected once in the
Smartcar Dashboard, and this flow asks for the API Credentials. The user ID
is discovered from the Connections API (or can be pasted from the dashboard).
"""

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_MANAGEMENT_TOKEN,
    CONF_UNIT_SYSTEM,
    CONF_USER_ID,
    DOMAIN,
    UNIT_SYSTEM_IMPERIAL,
    UNIT_SYSTEM_METRIC,
)
from .nissan_api import SmartcarApiClient

_LOGGER = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _credentials_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Form for the Smartcar API Credentials and optional user ID."""
    return vol.Schema(
        {
            vol.Required(
                CONF_CLIENT_ID,
                description={"suggested_value": defaults.get(CONF_CLIENT_ID, "")},
            ): str,
            vol.Required(CONF_CLIENT_SECRET): str,
            vol.Optional(
                CONF_USER_ID,
                description={"suggested_value": defaults.get(CONF_USER_ID, "")},
            ): str,
        }
    )


async def _async_validate(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Check credentials, resolve the user ID, and confirm a vehicle exists.

    Returns (entry_data, errors). entry_data is None when validation failed.
    """
    errors: dict[str, str] = {}
    client_id = user_input[CONF_CLIENT_ID].strip()
    client_secret = user_input[CONF_CLIENT_SECRET].strip()
    user_id = (user_input.get(CONF_USER_ID) or "").strip() or None

    client = SmartcarApiClient(
        client_id=client_id, client_secret=client_secret, user_id=user_id
    )

    try:
        await client.async_validate_credentials()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Smartcar API credential check failed: %s", err)
        # The Application ID (a UUID) only works for Smartcar Connect; the v3
        # API token endpoint needs the API Credentials client ID.
        if _UUID_RE.match(client_id):
            errors[CONF_CLIENT_ID] = "application_id_not_api_client_id"
        else:
            errors["base"] = "invalid_auth"
        return None, errors

    if not user_id:
        try:
            user_ids = await client.discover_user_ids()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not discover Smartcar user ID: %s", err)
            errors[CONF_USER_ID] = "user_id_lookup_failed"
            return None, errors
        if not user_ids:
            errors["base"] = "no_connections"
            return None, errors
        if len(user_ids) > 1:
            _LOGGER.warning("Multiple Smartcar user IDs found: %s", user_ids)
            errors[CONF_USER_ID] = "multiple_users"
            return None, errors
        user_id = user_ids[0]
        client.user_id = user_id
        _LOGGER.debug("Discovered Smartcar user ID %s", user_id)

    try:
        vehicles = await client.get_vehicle_list()
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error fetching vehicles: %s", err, exc_info=True)
        errors["base"] = "cannot_connect"
        return None, errors
    if not vehicles:
        errors["base"] = "no_vehicles"
        return None, errors

    return (
        {
            CONF_CLIENT_ID: client_id,
            CONF_CLIENT_SECRET: client_secret,
            CONF_USER_ID: user_id,
        },
        errors,
    )


class NissanNAConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow using Smartcar API Credentials (client-credentials grant)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Ask for the API Credentials and validate them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate(user_input)
            if data:
                await self.async_set_unique_id(data[CONF_USER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Nissan (Smartcar)", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle reauth when credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        """Re-enter API Credentials for an existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate(user_input)
            if data:
                return self.async_update_reload_and_abort(entry, data_updates=data)

        defaults = user_input or {
            CONF_CLIENT_ID: entry.data.get(CONF_CLIENT_ID, ""),
            CONF_USER_ID: entry.data.get(CONF_USER_ID, ""),
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(defaults),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return NissanNAOptionsFlowHandler()


class NissanNAOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Nissan NA integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict:
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "unit_system": "Configure Units (Metric/Imperial)",
                "refresh_sensors": "Refresh All Sensors",
                "rebuild_sensors": "Rebuild Sensors",
                "reload_entities": "Re-load Entities",
                "webhook_config": "Configure Webhooks",
                "reauth": "Re-authorize Integration",
            },
            description_placeholders={
                "unit_info": (
                    "Configure measurement units"
                    " (kilometers/miles, liters/gallons, etc.)"
                ),
                "refresh_info": "Manually fetch current vehicle data from Smartcar",
                "rebuild_info": "Validate and rebuild all sensors",
                "reload_info": "Discover and load any new entities",
                "webhook_info": "Configure real-time vehicle updates via webhooks",
                "reauth_info": "Update API credentials or re-authorize vehicle access",
            },
        )

    async def async_step_unit_system(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        if user_input is not None:
            new_options = {**self.config_entry.options}
            new_options[CONF_UNIT_SYSTEM] = user_input[CONF_UNIT_SYSTEM]
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=new_options
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # The options flow result *replaces* entry.options; pass them back
            return self.async_create_entry(title="", data=new_options)

        current_unit_system = self.config_entry.options.get(
            CONF_UNIT_SYSTEM, UNIT_SYSTEM_METRIC
        )
        return self.async_show_form(
            step_id="unit_system",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UNIT_SYSTEM, default=current_unit_system): vol.In(
                        {
                            UNIT_SYSTEM_METRIC: "Metric (km, L, °C, bar)",
                            UNIT_SYSTEM_IMPERIAL: "Imperial (mi, gal, °F, psi)",
                        }
                    ),
                }
            ),
            description_placeholders={
                "current_system": (
                    "Metric"
                    if current_unit_system == UNIT_SYSTEM_METRIC
                    else "Imperial"
                ),
            },
        )

    async def async_step_refresh_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        try:
            data = self.hass.data[DOMAIN].get(self.config_entry.entry_id)
            if not data:
                return self.async_abort(reason="integration_not_loaded")

            sensors = data.get("sensors", {})
            total_sensors = sum(len(v) for v in sensors.values())
            if total_sensors == 0:
                return self.async_abort(reason="no_sensors_found")

            refreshed = 0
            failed = 0
            for vehicle_id, vehicle_sensors in sensors.items():
                for sensor_key, sensor in vehicle_sensors.items():
                    try:
                        await sensor.async_update()
                        refreshed += 1
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to refresh sensor %s: %s",
                            (
                                sensor._attr_name
                                if hasattr(sensor, "_attr_name")
                                else sensor_key
                            ),
                            err,
                        )
                        failed += 1

            _LOGGER.info(
                "Manual refresh completed: %d updated, %d failed", refreshed, failed
            )
            return self.async_show_form(
                step_id="refresh_complete",
                description_placeholders={
                    "refreshed": str(refreshed),
                    "failed": str(failed),
                    "total": str(total_sensors),
                },
            )
        except Exception as err:
            _LOGGER.error("Error refreshing sensors: %s", err, exc_info=True)
            return self.async_abort(reason="refresh_failed")

    async def async_step_refresh_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        return await self.async_step_init()

    async def _async_reload_and_count(self) -> tuple[int, int] | None:
        """Reload the entry so entities match Smartcar's current signals.

        Replaces the upstream rebuild/re-load steps: those called the sensor
        platform with a stand-in callback that never registered the new
        entities, and rebuild first removed every sensor from the registry.
        """
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if not data:
            return None
        initial = sum(len(v) for v in data.get("sensors", {}).values())
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id) or {}
        final = sum(len(v) for v in data.get("sensors", {}).values())
        _LOGGER.info("Reloaded Nissan NA: %d -> %d sensors", initial, final)
        return initial, final

    async def async_step_rebuild_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        try:
            counts = await self._async_reload_and_count()
        except Exception as err:
            _LOGGER.error("Error rebuilding sensors: %s", err, exc_info=True)
            return self.async_abort(reason="rebuild_failed")
        if counts is None:
            return self.async_abort(reason="integration_not_loaded")
        initial, final = counts
        return self.async_show_form(
            step_id="rebuild_complete",
            description_placeholders={
                "initial": str(initial),
                "final": str(final),
                "added": str(max(final - initial, 0)),
                "removed": str(max(initial - final, 0)),
            },
        )

    async def async_step_rebuild_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        return await self.async_step_init()

    async def async_step_reload_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        try:
            counts = await self._async_reload_and_count()
        except Exception as err:
            _LOGGER.error("Error reloading entities: %s", err, exc_info=True)
            return self.async_abort(reason="reload_failed")
        if counts is None:
            return self.async_abort(reason="integration_not_loaded")
        initial, final = counts
        return self.async_show_form(
            step_id="reload_complete",
            description_placeholders={
                "initial": str(initial),
                "final": str(final),
                "new": str(max(final - initial, 0)),
            },
        )

    async def async_step_reload_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        return await self.async_step_init()

    async def async_step_webhook_config(
        self, user_input: dict[str, Any] | None = None
    ) -> dict:
        if user_input is not None:
            new_data = {**self.config_entry.data}
            if user_input.get(CONF_MANAGEMENT_TOKEN):
                new_data[CONF_MANAGEMENT_TOKEN] = user_input[CONF_MANAGEMENT_TOKEN]
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            # Keep existing options (e.g. unit_system); data={} would wipe them
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        current_token = self.config_entry.data.get(CONF_MANAGEMENT_TOKEN, "")
        webhook_url = self.config_entry.data.get("webhook_url", "Not configured")

        return self.async_show_form(
            step_id="webhook_config",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MANAGEMENT_TOKEN,
                        description={"suggested_value": current_token},
                    ): str,
                }
            ),
            description_placeholders={
                "webhook_url": webhook_url,
                "webhook_info": (
                    "To enable real-time webhook updates from Smartcar:\n"
                    "1. Get your Application Management"
                    " Token from the Smartcar Dashboard\n"
                    "2. Enter it below\n"
                    "3. Configure this webhook URL in"
                    f" your Smartcar Dashboard: {webhook_url}"
                ),
            },
        )

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None) -> dict:
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={
                    "source": config_entries.SOURCE_REAUTH,
                    "entry_id": self.config_entry.entry_id,
                },
                data=self.config_entry.data,
            )
        )
        return self.async_abort(reason="reauth_triggered")
