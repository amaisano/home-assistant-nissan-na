"""The integration uses Home Assistant's shared httpx client.

Before 0.21.9.4 every request built its own httpx.AsyncClient, which loads the
CA bundle inside the event loop: HA logged "Detected blocking call to
load_verify_locations ... by custom integration 'nissan_na'" at each start.
The test harness doesn't run HA's blocking-call detector, so the test fails
on any per-request client instead.
"""

from unittest.mock import patch

import httpx
import respx
from homeassistant import config_entries
from homeassistant.helpers.httpx_client import get_async_client
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nissan_na.const import DOMAIN

from .test_flow import API_CLIENT_ID, USER, VEH, V3, _conn, _mock_connections, _mock_token, _mock_vin
from .test_webhook import LIVE_SNAPSHOT, MGMT


@respx.mock
async def test_setup_and_refresh_use_shared_client(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER, "management_token": MGMT},
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    signals = respx.get(f"{V3}/vehicles/{VEH}/signals").mock(
        return_value=httpx.Response(200, json=LIVE_SNAPSHOT)
    )
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    shared = get_async_client(hass)  # HA's subclass; built before the patch
    no_new_clients = patch.object(httpx, "AsyncClient", side_effect=AssertionError("per-request httpx client"))
    with no_new_clients:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED

    client = hass.data[DOMAIN][entry.entry_id]["client"]
    assert client._http_client is shared

    # A request after setup still works through the shared client, and
    # `async with` must not have closed it.
    before = signals.call_count
    with no_new_clients:
        await hass.services.async_call(DOMAIN, "refresh_status", {"vehicle_id": VEH}, blocking=True)
    assert signals.call_count - before == 1
    assert not shared.is_closed
    assert await hass.config_entries.async_unload(entry.entry_id)
