"""Tests for the patched nissan_na config flow and Connections API parsing.

Smartcar responses follow the shapes in Smartcar's v3 docs
(api-reference/list-connections, getting-started/how-to/api-authentication).
"""

from unittest.mock import patch

import httpx
import pytest
import respx
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nissan_na.const import DOMAIN
from custom_components.nissan_na.nissan_api import SmartcarApiClient

TOKEN_URL = "https://iam.smartcar.com/oauth2/token"
V3 = "https://vehicle.api.smartcar.com/v3"
API_CLIENT_ID = "client_01TESTTESTTEST"
APP_ID_UUID = "5f1c2a9e-1111-4222-8333-944455556666"
USER = "1e258add-fb21-4293-b9ab-cf8a61f7a880"
OTHER_USER = "99999999-fb21-4293-b9ab-cf8a61f7a880"
VEH = "a6a425d6-c661-4b21-af7a-d6b75f80f3b0"
VIN = "1N4AZ1CV0RC000000"


def _conn(vehicle_id=VEH, user_id=USER, user_in_attrs=True, simulated=False):
    attrs = {
        "permissions": ["read_vehicle_info", "read_vin"],
        "vehicle": {
            "make": "NISSAN",
            "model": "LEAF",
            "year": 2024,
            "mode": "live",
            "powertrainType": "BEV",
        },
    }
    if simulated:
        attrs["vehicle"] = {
            "make": "SIMULATED",
            "model": "BEV",
            "year": 2026,
            "mode": "simulated",
            "powertrainType": "BEV",
        }
    rel = {
        "vehicle": {"data": {"id": vehicle_id, "type": "vehicle"}},
        "user": {"data": {"id": user_id, "type": "user"}},
    }
    if user_in_attrs:
        attrs["user"] = {"id": user_id}
    return {
        "id": f"conn-{vehicle_id[:4]}-{user_id[:4]}",
        "type": "connection",
        "attributes": attrs,
        "relationships": rel,
        "meta": {"createdAt": "2026-10-23T16:00:00Z"},
        "links": {"self": "/connections/x"},
    }


def _page(items, total=None, number=1, size=50):
    return {
        "data": items,
        "meta": {
            "pageNumber": number,
            "pageSize": size,
            "totalCount": len(items) if total is None else total,
            "orderBy": "createdAt",
            "orderDirection": "DESC",
        },
        "links": {"self": "", "first": "", "last": "", "next": None, "prev": None},
    }


def _mock_token(ok=True):
    if ok:
        return respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600},
            )
        )
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )


def _mock_connections(all_items, filtered_items=None):
    """Unfiltered call returns all_items; filter[userId] call returns filtered."""

    def handler(request: httpx.Request):
        uid = request.url.params.get("filter[userId]")
        if uid is None:
            return httpx.Response(200, json=_page(all_items))
        items = filtered_items if filtered_items is not None else [
            c for c in all_items if c["attributes"].get("user", {}).get("id") == uid
        ]
        return httpx.Response(200, json=_page(items))

    return respx.get(f"{V3}/connections").mock(side_effect=handler)


def _mock_vin():
    return respx.get(f"{V3}/vehicles/{VEH}/vin").mock(
        return_value=httpx.Response(
            200, json={"data": {"type": "vin", "attributes": {"vin": VIN}}}
        )
    )


@pytest.fixture
def no_setup():
    with patch(
        "custom_components.nissan_na.async_setup_entry", return_value=True
    ) as m:
        yield m


# ---------------------------------------------------------------- API unit tests


def test_parse_connection_doc_shape():
    vid, uid, vattrs = SmartcarApiClient._parse_connection(_conn())
    assert (vid, uid, vattrs["make"]) == (VEH, USER, "NISSAN")


def test_parse_connection_user_from_relationships():
    vid, uid, _ = SmartcarApiClient._parse_connection(_conn(user_in_attrs=False))
    assert (vid, uid) == (VEH, USER)


@respx.mock
async def test_connections_pagination_and_filter():
    _mock_token()
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        page = int(request.url.params["page[number]"])
        items = [_conn(vehicle_id=f"{page:08d}-0000-0000-0000-000000000000")]
        return httpx.Response(200, json=_page(items, total=2, number=page, size=1))

    respx.get(f"{V3}/connections").mock(side_effect=handler)
    client = SmartcarApiClient(API_CLIENT_ID, "secret", user_id=USER)
    conns = await client._get_connections(USER)
    assert len(conns) == 2
    assert [c["page[number]"] for c in calls] == ["1", "2"]
    assert all(c["filter[userId]"] == USER for c in calls)


@respx.mock
async def test_vehicle_list_skips_other_users_and_dupes():
    _mock_token()
    _mock_vin()
    # Server ignores the filter (defensive): returns another user's car + a dupe
    _mock_connections(
        [_conn(), _conn(), _conn(vehicle_id="other-veh", user_id=OTHER_USER)],
        filtered_items=[
            _conn(),
            _conn(),
            _conn(vehicle_id="other-veh", user_id=OTHER_USER),
        ],
    )
    client = SmartcarApiClient(API_CLIENT_ID, "secret", user_id=USER)
    vehicles = await client.get_vehicle_list()
    assert [(v.id, v.vin, v.make, v.model, v.year) for v in vehicles] == [
        (VEH, VIN, "NISSAN", "LEAF", 2024)
    ]


# ---------------------------------------------------------------- config flow


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


@respx.mock
async def test_user_flow_discovers_user_id(hass: HomeAssistant, no_setup):
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": f"  {API_CLIENT_ID} ", "client_secret": "secret"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"] == {
        "client_id": API_CLIENT_ID,
        "client_secret": "secret",
        "user_id": USER,
    }
    assert result["result"].unique_id == USER
    no_setup.assert_called_once()


@respx.mock
async def test_user_flow_pasted_user_id_skips_discovery(hass, no_setup):
    _mock_token()
    route = _mock_connections([_conn()])
    _mock_vin()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": API_CLIENT_ID, "client_secret": "secret", "user_id": USER},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # only the filtered call, no discovery call
    assert all("filter[userId]" in c.request.url.params for c in route.calls)


@pytest.mark.parametrize(
    ("client_id", "error_key", "error_value"),
    [
        (APP_ID_UUID, "client_id", "application_id_not_api_client_id"),
        (API_CLIENT_ID, "base", "invalid_auth"),
    ],
)
@respx.mock
async def test_user_flow_bad_credentials(hass, no_setup, client_id, error_key, error_value):
    _mock_token(ok=False)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": client_id, "client_secret": "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {error_key: error_value}


@respx.mock
async def test_user_flow_uuid_client_id_allowed_if_token_works(hass, no_setup):
    """Don't block a UUID client ID that Smartcar actually accepts."""
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": APP_ID_UUID, "client_secret": "s"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("conns", "errors"),
    [
        ([], {"base": "no_connections"}),
        (
            [_conn(), _conn(vehicle_id="v2", user_id=OTHER_USER)],
            {"user_id": "multiple_users"},
        ),
    ],
)
@respx.mock
async def test_user_flow_discovery_errors(hass, no_setup, conns, errors):
    _mock_token()
    _mock_connections(conns)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": API_CLIENT_ID, "client_secret": "s"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


@respx.mock
async def test_user_flow_ignores_simulator_connection(hass, no_setup):
    """Real account: one live Ariya user plus a Vehicle Simulator user."""
    _mock_token()
    _mock_connections(
        [_conn(vehicle_id="sim-veh", user_id=OTHER_USER, simulated=True), _conn()]
    )
    _mock_vin()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": API_CLIENT_ID, "client_secret": "s"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"]["user_id"] == USER


@respx.mock
async def test_discovery_falls_back_to_simulator_only():
    _mock_token()
    _mock_connections([_conn(vehicle_id="sim-veh", user_id=OTHER_USER, simulated=True)])
    client = SmartcarApiClient(API_CLIENT_ID, "s")
    assert await client.discover_user_ids() == [OTHER_USER]


@respx.mock
async def test_user_flow_connections_http_error(hass, no_setup):
    _mock_token()
    respx.get(f"{V3}/connections").mock(return_value=httpx.Response(403, json={}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": API_CLIENT_ID, "client_secret": "s"}
    )
    assert result["errors"] == {"user_id": "user_id_lookup_failed"}


@respx.mock
async def test_user_flow_no_vehicles(hass, no_setup):
    _mock_token()
    _mock_connections([_conn()], filtered_items=[])
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": API_CLIENT_ID, "client_secret": "s"}
    )
    assert result["errors"] == {"base": "no_vehicles"}


@respx.mock
async def test_user_flow_already_configured(hass, no_setup):
    MockConfigEntry(domain=DOMAIN, unique_id=USER, data={"user_id": USER}).add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": API_CLIENT_ID, "client_secret": "s"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@respx.mock
async def test_reauth_updates_entry(hass, no_setup):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER,
        data={
            "client_id": API_CLIENT_ID,
            "client_secret": "old",
            "user_id": USER,
            "webhook_id": "keepme",
        },
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": API_CLIENT_ID, "client_secret": "new", "user_id": USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["client_secret"] == "new"
    assert entry.data["webhook_id"] == "keepme"  # data_updates merges


def test_translations_cover_flow_keys():
    import json
    import pathlib
    import re

    base = pathlib.Path(__file__).parents[2] / "custom_components" / "nissan_na"
    en = json.loads((base / "translations" / "en.json").read_text())
    src = (base / "config_flow.py").read_text()
    for key in set(re.findall(r'errors\[[^\]]+\] = "([a-z_]+)"', src)):
        assert key in en["config"]["error"], key
    for step in ("user", "reauth_confirm"):
        assert set(en["config"]["step"][step]["data"]) == {
            "client_id",
            "client_secret",
            "user_id",
        }
    assert "unit_system" in en["options"]["step"]
    assert "unit_system" in en["options"]["step"]["init"]["menu_options"]


# ---------------------------------------------------------------- setup


@respx.mock
async def test_setup_entry_with_api_credentials(hass, caplog):
    """A new-style entry loads without Application Credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER},
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    # Everything else on the vehicle (signals, attributes): not available
    respx.route(host="vehicle.api.smartcar.com").mock(
        return_value=httpx.Response(404, json={"errors": []})
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED, caplog.text
    assert "OAuth implementation not found" not in caplog.text
    assert f"vehicle {VIN}" not in caplog.text  # API calls use the vehicle ID
    assert entry.data["webhook_id"]  # setup generates one
    from homeassistant.helpers import device_registry as dr

    devs = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [(d.name, d.identifiers) for d in devs] == [
        ("2024 NISSAN LEAF", {(DOMAIN, VIN)})
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------- options persistence


async def test_options_unit_system_and_webhook_keep_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.nissan_na.async_setup_entry", return_value=True), \
         patch("custom_components.nissan_na.async_unload_entry", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Units -> imperial
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "unit_system"})
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"unit_system": "imperial"})
        await hass.async_block_till_done()
        assert r["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options.get("unit_system") == "imperial"

        # Saving the management token must not wipe unit_system
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "webhook_config"})
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"management_token": "tok"})
        await hass.async_block_till_done()
        assert entry.data["management_token"] == "tok"
        assert entry.options.get("unit_system") == "imperial"
