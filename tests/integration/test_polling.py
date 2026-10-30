"""Periodic and manual refreshes: one /signals call per vehicle, disabled sensors skipped.

Before 0.21.9.2 the hourly loop called async_update() on every sensor: one
/signals call each, plus an error per registry-disabled sensor (HA never adds
those, so their hass is None).
"""

import copy
from datetime import timedelta

import httpx
import respx
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nissan_na.const import DOMAIN

from .test_flow import API_CLIENT_ID, USER, VEH, VIN, V3, _conn, _mock_connections, _mock_token, _mock_vin
from .test_webhook import LIVE_SNAPSHOT, MGMT

BATTERY = "sensor.2024_nissan_leaf_battery"
RANGE = "sensor.2024_nissan_leaf_range"


def _snapshot_with_soc(soc):
    snap = copy.deepcopy(LIVE_SNAPSHOT)
    for sig in snap["data"]:
        if sig["attributes"]["code"] == "tractionbattery-stateofcharge":
            sig["attributes"]["body"]["value"] = soc
    return snap


@respx.mock
async def test_refreshes_fetch_once_and_skip_disabled_sensors(hass, caplog):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER, "management_token": MGMT},
    )
    entry.add_to_hass(hass)
    # A sensor the user disabled, as Adamo did with the ones the Ariya can't fill
    reg = er.async_get(hass)
    disabled = reg.async_get_or_create(
        "sensor", DOMAIN, f"{VIN}_battery.range", config_entry=entry,
        suggested_object_id="2024_nissan_leaf_range",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert disabled.entity_id == RANGE

    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    signals = respx.get(f"{V3}/vehicles/{VEH}/signals").mock(
        return_value=httpx.Response(200, json=LIVE_SNAPSHOT)
    )
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.states.get(BATTERY).state == "80"
    assert hass.states.get(RANGE) is None  # disabled: never added

    # Hourly poll (default update_interval 60 min)
    signals.mock(return_value=httpx.Response(200, json=_snapshot_with_soc(55)))
    before = signals.call_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=61))
    await hass.async_block_till_done()
    assert signals.call_count - before == 1
    assert hass.states.get(BATTERY).state == "55"  # state is written, not just computed

    # nissan_na.refresh_status
    signals.mock(return_value=httpx.Response(200, json=_snapshot_with_soc(60)))
    before = signals.call_count
    await hass.services.async_call(DOMAIN, "refresh_status", {"vehicle_id": VEH}, blocking=True)
    await hass.async_block_till_done()
    assert signals.call_count - before == 1
    assert hass.states.get(BATTERY).state == "60"

    # Options > Refresh sensors
    signals.mock(return_value=httpx.Response(200, json=_snapshot_with_soc(65)))
    before = signals.call_count
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "refresh_sensors"})
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "refresh_complete", r
    assert r["description_placeholders"]["failed"] == "0"
    assert signals.call_count - before == 1
    assert hass.states.get(BATTERY).state == "65"

    assert "Failed to update sensor" not in caplog.text
    assert "Failed to update vehicle" not in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
