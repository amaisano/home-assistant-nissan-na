"""Smartcar v3 commands (docs: api-reference/commands/overview, security/*, charging/*)."""

import json

import httpx
import pytest
import respx
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nissan_na.const import DOMAIN
from custom_components.nissan_na.nissan_api import SmartcarApiClient, SmartcarCommandError

from .test_flow import API_CLIENT_ID, USER, VEH, V3, _conn, _mock_connections, _mock_token, _mock_vin
from .test_webhook import LIVE_SNAPSHOT


def _ok(command_type):
    return {"data": {"id": "exec_1", "type": "command-execution", "attributes": {
        "commandType": command_type, "status": {"value": "SUCCESS"}, "executionMode": "sync"}}}


FAILURE_202 = {"data": {"id": "exec_2", "type": "command-execution", "attributes": {
    "commandType": "security-unlock",
    "status": {"value": "FAILURE", "error": {"status": "500", "type": "VEHICLE", "code": "UNLOCK_FAILED",
                                               "title": "Unlock Failed", "detail": "The vehicle was unable to unlock its doors."}},
    "executionMode": "sync"}}}


@pytest.mark.parametrize(
    ("method", "args", "path", "body"),
    [
        ("lock_doors", (), "security/lock", None),
        ("unlock_doors", (), "security/unlock", None),
        ("start_charge", (), "charge/start", None),
        ("stop_charge", (), "charge/stop", None),
        ("set_charge_limit", (80,), "charge/set-limit", {"data": {"attributes": {"percent": 80}}}),
    ],
)
@respx.mock
async def test_command_urls_headers_bodies(method, args, path, body):
    _mock_token()
    route = respx.post(f"{V3}/vehicles/{VEH}/commands/{path}").mock(
        return_value=httpx.Response(200, json=_ok(path.replace("/", "-"))))
    client = SmartcarApiClient(API_CLIENT_ID, "s", user_id=USER)
    await getattr(client, method)(VEH, *args)
    req = route.calls.last.request
    assert req.headers["sc-user-id"] == USER
    assert req.headers["authorization"] == "Bearer tok"
    sent = json.loads(req.content) if req.content else None
    assert sent == body


@respx.mock
async def test_command_202_failure_is_raised():
    _mock_token()
    respx.post(f"{V3}/vehicles/{VEH}/commands/security/unlock").mock(return_value=httpx.Response(202, json=FAILURE_202))
    with pytest.raises(SmartcarCommandError, match="Unlock Failed: The vehicle was unable"):
        await SmartcarApiClient(API_CLIENT_ID, "s", user_id=USER).unlock_doors(VEH)


@respx.mock
async def test_command_http_error_carries_smartcar_text():
    _mock_token()
    respx.post(f"{V3}/vehicles/{VEH}/commands/security/unlock").mock(return_value=httpx.Response(
        409, json={"errors": [{"status": "409", "type": "CONNECTED_SERVICES_ACCOUNT", "code": "SUBSCRIPTION",
                               "title": "subscription", "detail": "The vehicle's connected services subscription is inactive."}]}))
    with pytest.raises(SmartcarCommandError, match=r"HTTP 409\) - subscription: The vehicle's connected services"):
        await SmartcarApiClient(API_CLIENT_ID, "s", user_id=USER).unlock_doors(VEH)


async def test_climate_has_no_smartcar_command():
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(500))
        with pytest.raises(SmartcarCommandError, match="no climate command"):
            await SmartcarApiClient(API_CLIENT_ID, "s", user_id=USER).start_climate(VEH)
        assert not router.calls


@respx.mock
async def test_lock_entity_service_calls(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER,
                            data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER})
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=LIVE_SNAPSHOT))
    unlock = respx.post(f"{V3}/vehicles/{VEH}/commands/security/unlock").mock(
        side_effect=[httpx.Response(200, json=_ok("security-unlock")), httpx.Response(202, json=FAILURE_202)])
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = [e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == "lock"][0]
    assert hass.states.get(lock).state == "locked"

    await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
    assert unlock.call_count == 1
    assert hass.states.get(lock).state == "unlocked"

    with pytest.raises(HomeAssistantError, match="Unlock failed: .*Unlock Failed"):
        await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
