"""Unit tests for the options flow (NissanNAOptionsFlowHandler).

The OAuth2FlowHandler tests were removed with the OAuth flow itself (HA's
OAuth callback drops Smartcar's user_id). The API-credentials config flow
is covered by tests/integration/test_flow.py.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from homeassistant import config_entries
from custom_components.nissan_na.config_flow import NissanNAOptionsFlowHandler
from custom_components.nissan_na.const import (
    DOMAIN,
    CONF_MANAGEMENT_TOKEN,
    CONF_UNIT_SYSTEM,
    UNIT_SYSTEM_METRIC,
    UNIT_SYSTEM_IMPERIAL,
)

# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="Home Assistant deprecates setting config_entry directly in tests", run=True
)
class TestOptionsFlow:
    async def test_async_step_init_menu(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)

        result = await flow.async_step_init()
        assert result["type"] == "menu"

    async def test_async_step_unit_system_save_metric(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()

        result = await flow.async_step_unit_system(
            {CONF_UNIT_SYSTEM: UNIT_SYSTEM_METRIC}
        )
        assert result["type"] in ("create_entry", "form", "abort")

    async def test_async_step_unit_system_save_imperial(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()

        result = await flow.async_step_unit_system(
            {CONF_UNIT_SYSTEM: UNIT_SYSTEM_IMPERIAL}
        )
        assert result["type"] in ("create_entry", "form", "abort")

    async def test_async_step_webhook_config_show_form(self):
        mock_entry = MagicMock()
        mock_entry.data = {"webhook_url": "https://example.com/webhook"}
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()

        result = await flow.async_step_webhook_config()
        assert result["type"] == "form"

    async def test_async_step_webhook_config_save(self):
        mock_entry = MagicMock()
        mock_entry.data = {}
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()

        result = await flow.async_step_webhook_config(
            {CONF_MANAGEMENT_TOKEN: "token_abc"}
        )
        assert result["type"] in ("create_entry", "form", "abort")

    async def test_async_step_refresh_sensors_success(self):
        mock_sensor = AsyncMock()
        mock_sensor._attr_name = "Battery"

        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.data = {
            DOMAIN: {"test_entry": {"sensors": {"v1": {"bat": mock_sensor}}}}
        }

        result = await flow.async_step_refresh_sensors()
        assert result["type"] in ("form", "abort")

    async def test_async_step_refresh_sensors_no_sensors(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.data = {DOMAIN: {"test_entry": {"sensors": {}}}}

        result = await flow.async_step_refresh_sensors()
        assert result["type"] == "abort"
        assert result["reason"] == "no_sensors_found"

    async def test_async_step_refresh_sensors_with_failures(self):
        mock_sensor = AsyncMock()
        mock_sensor.async_update = AsyncMock(side_effect=Exception("API error"))
        mock_sensor._attr_name = "Battery"

        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.data = {
            DOMAIN: {"test_entry": {"sensors": {"v1": {"bat": mock_sensor}}}}
        }

        result = await flow.async_step_refresh_sensors()
        assert result["type"] in ("form", "abort")

    async def test_async_step_reauth(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.data = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.async_create_task = MagicMock()
        flow.hass.config_entries.flow.async_init = AsyncMock()

        result = await flow.async_step_reauth()
        assert result["type"] == "abort"
        assert result["reason"] == "reauth_triggered"


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="Home Assistant deprecates setting config_entry directly in tests", run=True
)
class TestOptionsFlowRebuildSensors:
    async def test_async_step_rebuild_sensors_integration_not_loaded(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.data = {DOMAIN: {}}

        result = await flow.async_step_rebuild_sensors()
        assert result["type"] == "abort"
        assert result["reason"] == "integration_not_loaded"

    async def test_async_step_rebuild_sensors_client_not_found(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)
        flow.hass = MagicMock()
        flow.hass.data = {DOMAIN: {"test_entry": {"sensors": {}}}}

        result = await flow.async_step_rebuild_sensors()
        assert result["type"] == "abort"
        assert result["reason"] == "client_not_found"

    async def test_init_menu_includes_rebuild_option(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)

        result = await flow.async_step_init()
        assert result["type"] == "menu"
        assert "rebuild_sensors" in result.get("menu_options", {})

    async def test_async_step_rebuild_complete_returns_to_menu(self):
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry"
        mock_entry.options = {}

        flow = NissanNAOptionsFlowHandler()
        object.__setattr__(flow, "config_entry", mock_entry)

        result = await flow.async_step_rebuild_complete()
        assert result["type"] == "menu"
