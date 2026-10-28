"""End-to-end: Smartcar v4 webhook -> converter -> entity states.

Payload shapes follow Smartcar docs (webhooks/events/vehicle-state,
integrations/webhooks/callback-verification, api-reference/signals/*).
"""

import hashlib
import hmac
import json

import httpx
import respx
from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nissan_na.const import DOMAIN
from custom_components.nissan_na.nissan_api import SmartcarApiClient
from custom_components.nissan_na.webhook import _parse_smartcar_signals

from .test_flow import API_CLIENT_ID, USER, VEH, VIN, V3, _conn, _mock_connections, _mock_token, _mock_vin

MGMT = "mgmt-token-test"
PERMISSION_CODES = [
    "charge-chargetimers", "charge-detailedchargingstatus", "charge-ischarging",
    "charge-ischargingcableconnected", "charge-timetocomplete", "closure-doors",
    "closure-enginecover", "closure-islocked", "closure-reartrunk", "diagnostics-airbag",
    "diagnostics-brakefluid", "diagnostics-oilpressure", "diagnostics-tirepressure",
    "location-preciselocation", "odometer-traveleddistance", "tractionbattery-range",
    "tractionbattery-stateofcharge", "vehicleidentification-vin", "wheel-tires",
]


def _rest_signals_like_live():
    """The Ariya's real /signals response on 2026-10-23: all errors."""
    err = {"type": "PERMISSION", "code": "UNKNOWN", "detail": "insufficient permissions"}
    data = [
        {"id": c, "type": "signal", "attributes": {"code": c, "status": {"value": "ERROR", "error": err}}}
        for c in PERMISSION_CODES
    ]
    for c in ("internalcombustionengine-amountremaining", "internalcombustionengine-fuellevel", "internalcombustionengine-range"):
        data.append({"id": c, "type": "signal", "attributes": {"code": c, "status": {"value": "ERROR", "error": {"type": "COMPATIBILITY", "code": "VEHICLE_NOT_CAPABLE"}}}})
    return {"data": data, "meta": {"totalCount": len(data)}, "links": {}, "included": []}


def _sig(code, body):
    return {"code": code, "name": code, "group": code.split("-")[0], "body": body,
            "meta": {"oemUpdatedAt": 1731940328000, "retrievedAt": 1731940330000}}


VEHICLE_STATE = {
    "eventId": "e1",
    "eventType": "VEHICLE_STATE",
    "data": {
        "user": {"id": USER},
        "vehicle": {"id": VEH, "make": "NISSAN", "model": "LEAF", "year": 2024, "mode": "live", "powertrainType": "BEV"},
        "triggers": [{"code": "tractionbattery-stateofcharge", "name": "StateOfCharge", "group": "TractionBattery"}],
        "signals": [
            _sig("tractionbattery-stateofcharge", {"unit": "percent", "value": 78}),
            _sig("tractionbattery-range", {"value": 123.45, "type": "DEFAULT", "unit": "km"}),
            _sig("closure-islocked", {"value": True}),
            _sig("closure-doors", {"rowCount": 2, "columnCount": 2, "doors": [
                {"row": 0, "column": 0, "isOpen": True, "isLocked": False},
                {"row": 0, "column": 1, "isOpen": False, "isLocked": True},
                {"row": 1, "column": 0, "isOpen": False, "isLocked": True},
                {"row": 1, "column": 1, "isOpen": False, "isLocked": True}]}),
            _sig("closure-reartrunk", {"isLocked": True, "isOpen": False}),
            _sig("charge-ischargingcableconnected", {"value": True}),
            _sig("charge-ischarging", {"value": False}),
            _sig("charge-chargelimits", {"activeLimit": 80, "values": [{"type": "GLOBAL", "limit": 80}]}),
            _sig("charge-amperagemax", {"value": 48, "unit": "ampere"}),
            _sig("odometer-traveleddistance", {"value": 78432, "unit": "km"}),
            _sig("location-preciselocation", {"latitude": 40.1, "longitude": -75.2, "heading": 45.3, "locationType": "CURRENT"}),
            _sig("connectivitystatus-isonline", {"value": True}),
            {"code": "wheel-tires", "name": "Tires", "group": "Wheel",
             "status": {"value": "ERROR", "error": {"type": "COMPATIBILITY", "code": "VEHICLE_NOT_CAPABLE"}}},
        ],
    },
    "meta": {"version": "4.0", "webhookId": "w1", "deliveryId": "d1", "deliveredAt": "2026-10-23T19:00:00Z"},
}


# ---------------------------------------------------------------- converter units


def test_parse_v4_signals_shape():
    d = _parse_smartcar_signals(VEHICLE_STATE["data"]["signals"])
    assert d["battery"] == {"percentRemaining": 78, "range": 123.45}
    assert d["lock"] == {"is_locked": True}
    assert d["closure"]["doors"]["frontLeft"] == {"isOpen": True, "isLocked": False}
    assert d["closure"]["rearTrunk"] == {"isLocked": True, "isOpen": False}
    assert d["charge"] == {"isPluggedIn": True, "isCharging": False, "limit": 80, "amperageMax": 48}
    assert d["odometer"] == {"distance": 78432}
    assert d["location"] == {"latitude": 40.1, "longitude": -75.2}
    assert d["connectivity"] == {"isOnline": True}
    assert "tires" not in d and "wheel-tires" not in d  # errored signal skipped


def test_rest_and_webhook_share_mapping_false_and_zero_kept():
    st = {}
    SmartcarApiClient._apply_signal_to_status(st, "tractionbattery-stateofcharge", {"value": 0})
    SmartcarApiClient._apply_signal_to_status(st, "closure-islocked", {"value": False})
    assert st["battery"]["percentRemaining"] == 0
    assert st["lock"] == {"is_locked": False}


@respx.mock
async def test_rest_status_maps_success_signals():
    _mock_token()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "a", "type": "signal", "attributes": {"code": "tractionbattery-stateofcharge", "status": {"value": "SUCCESS"}, "body": {"value": 55, "unit": "percent"}}},
        {"id": "b", "type": "signal", "attributes": {"code": "closure-islocked", "status": {"value": "SUCCESS"}, "body": {"value": True}}},
    ]}))
    st = await SmartcarApiClient(API_CLIENT_ID, "s", user_id=USER).get_vehicle_status(VEH)
    assert st["battery"]["percentRemaining"] == 55 and st["lock"]["is_locked"] is True


# ---------------------------------------------------------------- end to end


@respx.mock
async def test_webhook_end_to_end(hass, hass_client_no_auth):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER, "management_token": MGMT},
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=_rest_signals_like_live()))
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED

    reg = er.async_get(hass)
    # keyed by entity_id: a sensor and a binary_sensor may share a unique_id
    ents = {e.entity_id: e.unique_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    ents = {f"{eid}|{uid}": eid for eid, uid in ents.items()}

    def eid(unique_suffix):
        hits = [v for k, v in ents.items() if k.endswith(unique_suffix)]
        assert hits, (unique_suffix, sorted(ents))
        return hits[0]

    client = await hass_client_no_auth()
    url = f"/api/webhook/{entry.data['webhook_id']}"

    # VERIFY challenge
    resp = await client.post(url, json={"eventId": "v", "eventType": "VERIFY", "data": {"challenge": "abc123"}, "meta": {"version": "4.0"}})
    assert resp.status == 200
    expected = hmac.new(MGMT.encode(), b"abc123", hashlib.sha256).hexdigest()
    assert (await resp.json()) == {"challenge": expected}

    # Unsigned VEHICLE_STATE is rejected
    raw = json.dumps(VEHICLE_STATE).encode()
    resp = await client.post(url, data=raw, headers={"Content-Type": "application/json"})
    assert resp.status == 401

    # Signed VEHICLE_STATE updates entities
    sig = hmac.new(MGMT.encode(), raw, hashlib.sha256).hexdigest()
    resp = await client.post(url, data=raw, headers={"Content-Type": "application/json", "SC-Signature": sig})
    assert resp.status == 200
    await hass.async_block_till_done()

    states = {e: hass.states.get(e).state for e in ents.values() if hass.states.get(e)}
    print("\n".join(f"{k} = {v}" for k, v in sorted(states.items())))

    assert hass.states.get(eid("battery.percentRemaining")).state == "78"
    assert float(hass.states.get(eid("battery.range")).state) > 0
    assert hass.states.get(eid("closure.doors.frontLeft.isOpen")).state == "on"
    assert hass.states.get(eid("closure.doors.frontRight.isOpen")).state == "off"
    # lock device class: on = unlocked, off = locked
    assert hass.states.get(eid("closure.rearTrunk.isLocked")).state == "off"
    assert hass.states.get(eid("closure.doors.frontLeft.isLocked")).state == "on"
    assert hass.states.get(eid("closure.doors.frontRight.isLocked")).state == "off"
    assert hass.states.get(eid("charge.isPluggedIn")).state in ("True", "on")
    lock = [e for e in ents.values() if e.startswith("lock.")][0]
    assert hass.states.get(lock).state == "locked"
    tracker = [e for e in ents.values() if e.startswith("device_tracker.")][0]
    t = hass.states.get(tracker)
    assert (t.attributes.get("latitude"), t.attributes.get("longitude")) == (40.1, -75.2)

    assert await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------- REST snapshot shaped like a live 2023 Ariya (values altered)


def _live_ok(code, body):
    return {"id": code, "type": "signal",
            "attributes": {"code": code, "status": {"value": "SUCCESS"}, "body": body},
            "meta": {"ingestedAt": "2026-10-23T18:39:00Z"}}


def _live_err(code, kind):
    return {"id": code, "type": "signal",
            "attributes": {"code": code, "status": {"value": "ERROR", "error": {"type": kind, "code": "X"}}}}


def _grid(key, **flags):
    return {key: [{"row": r, "column": c, **flags} for r in (0, 1) for c in (0, 1)], "rowCount": 2, "columnCount": 2}


LIVE_SNAPSHOT = {"data": [
    _live_ok("wheel-tires", {"values": [{"row": r, "column": c, "tirePressure": 241.31652} for r in (0, 1) for c in (0, 1)], "rowCount": 2, "columnCount": 2, "unit": "kPa"}),
    _live_ok("closure-doors", _grid("values", isOpen=False, isLocked=True)),
    _live_ok("closure-reartrunk", {"isOpen": False, "isLocked": True}),
    _live_ok("diagnostics-airbag", {"status": "OK", "description": None}),
    _live_ok("diagnostics-oilpressure", {"status": "OK", "description": None}),
    _live_ok("charge-detailedchargingstatus", {"value": "NOT_CHARGING"}),
    _live_ok("closure-enginecover", {"isOpen": False}),
    _live_ok("diagnostics-tirepressure", {"status": "OK", "description": None}),
    _live_ok("charge-ischarging", {"value": False}),
    _live_ok("diagnostics-brakefluid", {"status": "OK", "description": None}),
    _live_ok("charge-ischargingcableconnected", {"value": False}),
    _live_ok("tractionbattery-range", {"value": 402.336, "type": "DEFAULT", "additionalValues": [], "unit": "km"}),
    _live_ok("odometer-traveleddistance", {"value": 12345.6789, "unit": "km"}),
    _live_ok("closure-islocked", {"value": True}),
    _live_ok("tractionbattery-stateofcharge", {"value": 80, "unit": "percent"}),
    _live_ok("vehicleidentification-vin", {"value": VIN}),
    _live_ok("charge-timetocomplete", {"value": 90}),
    _live_err("location-preciselocation", "PERMISSION"),
    _live_err("charge-chargetimers", "PERMISSION"),
    *[_live_err(c, "COMPATIBILITY") for c in (
        "internalcombustionengine-amountremaining", "internalcombustionengine-fuellevel",
        "internalcombustionengine-range", "connectivitystatus-isonline", "connectivitystatus-isasleep",
        "connectivitystatus-isdigitalkeypaired", "connectivitysoftware-currentfirmwareversion",
        "vehicleidentification-nickname")],
], "meta": {"totalCount": 27}, "links": {}, "included": []}


def test_live_doors_values_key():
    st = {}
    SmartcarApiClient._apply_signal_to_status(st, "closure-doors", _grid("values", isOpen=False, isLocked=True))
    assert st["closure"]["doors"]["backRight"] == {"isOpen": False, "isLocked": True}


@respx.mock
async def test_restart_with_live_snapshot(hass):
    """What HA builds after a restart, given the Ariya's real snapshot."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER, "management_token": MGMT},
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=LIVE_SNAPSHOT))
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED

    reg = er.async_get(hass)
    # keyed by entity_id: a sensor and a binary_sensor may share a unique_id
    ents = {e.entity_id: e.unique_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    ents = {f"{eid}|{uid}": eid for eid, uid in ents.items()}
    states = {e: hass.states.get(e).state for e in ents.values() if hass.states.get(e)}
    print("\n" + "\n".join(f"{k} = {v}" for k, v in sorted(states.items())))

    def st(suffix, domain=None):
        hits = [v for k, v in ents.items() if k.endswith(suffix) and (domain is None or v.startswith(domain + "."))]
        assert len(hits) == 1, (suffix, hits)
        return hass.states.get(hits[0]).state

    assert st("battery.percentRemaining", "sensor") == "80"
    assert float(st("battery.range")) == 402.336
    assert st("charge.state") == "NOT_CHARGING"
    assert st("tires.frontLeft.pressure") not in ("unknown", "unavailable")
    # binary sensors exist again and are seeded from the snapshot
    assert st("closure.doors.frontLeft.isLocked", "binary_sensor") == "off"  # locked
    assert st("closure.doors.frontLeft.isOpen", "binary_sensor") == "off"
    assert st("closure.rearTrunk.isLocked", "binary_sensor") == "off"  # locked
    assert st("charge.isPluggedIn", "binary_sensor") == "off"
    # no entities for signals the car can't provide
    assert not any(k.endswith("connectivity.isOnline") for k in ents)
    # numeric sensors carry a state_class so they get long-term statistics
    sc = lambda e: hass.states.get(e).attributes.get("state_class")
    assert sc("sensor.2024_nissan_leaf_battery") == "measurement"
    assert sc("sensor.2024_nissan_leaf_tire_pressure_front_left") == "measurement"
    assert sc("sensor.2024_nissan_leaf_odometer") == "total_increasing"
    assert sc("sensor.2024_nissan_leaf_charging_status") is None
    assert sc("sensor.2024_nissan_leaf_location_latitude") is None
    lock = [e for e in ents.values() if e.startswith("lock.")][0]
    assert hass.states.get(lock).state == "locked"
    assert await hass.config_entries.async_unload(entry.entry_id)


@respx.mock
async def test_rebuild_and_reload_options_keep_entities(hass):
    """Upstream rebuild wiped the registry and never re-added sensors."""
    from homeassistant.data_entry_flow import FlowResultType

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER,
        data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER},
    )
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=LIVE_SNAPSHOT))
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reg = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    battery = "sensor.2024_nissan_leaf_battery"
    assert battery in before
    assert hass.states.get(battery).state == "80"

    for step, done in (("rebuild_sensors", "rebuild_complete"), ("reload_entities", "reload_complete")):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": step})
        await hass.async_block_till_done()
        assert r["type"] is FlowResultType.FORM and r["step_id"] == done, r
        after = {e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
        assert after == before, (step, before ^ after)
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert hass.states.get(battery).state == "80", step
    assert await hass.config_entries.async_unload(entry.entry_id)


@respx.mock
async def test_last_parked_location_feeds_tracker(hass):
    """Live Ariya 15:39: location-preciselocation SUCCESS with locationType LAST_PARKED."""
    import copy

    snap = copy.deepcopy(LIVE_SNAPSHOT)
    snap["data"] = [s for s in snap["data"] if s["id"] != "location-preciselocation"] + [
        _live_ok("location-preciselocation", {"latitude": 40.1, "longitude": -75.2, "direction": "N",
                                              "heading": 0, "locationType": "LAST_PARKED"})]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER,
                            data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER})
    entry.add_to_hass(hass)
    _mock_token()
    _mock_connections([_conn()])
    _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=snap))
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ents = [e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)]
    tracker = [e for e in ents if e.startswith("device_tracker.")][0]
    t = hass.states.get(tracker)
    assert (t.attributes.get("latitude"), t.attributes.get("longitude")) == (40.1, -75.2)
    assert hass.states.get("sensor.2024_nissan_leaf_location_latitude").state == "40.1"
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------- live unlock payload (2026-10-25 15:14)

UNLOCKED_DOORS = {"values": [{"row": r, "column": c, "isOpen": False, "isLocked": None} for r in (0, 1) for c in (0, 1)],
                  "rowCount": 2, "columnCount": 2}


def test_null_door_locks_fall_back_to_car_lock():
    sigs = [_sig("closure-doors", UNLOCKED_DOORS), _sig("closure-reartrunk", {"isOpen": False, "isLocked": None}),
            _sig("closure-islocked", {"value": False})]
    d = _parse_smartcar_signals(sigs)
    assert d["closure"]["doors"]["frontLeft"] == {"isOpen": False, "isLocked": False}
    assert d["closure"]["rearTrunk"]["isLocked"] is False
    # order of signals in the payload must not matter
    d2 = _parse_smartcar_signals(list(reversed(sigs)))
    assert d2["closure"]["doors"]["backRight"]["isLocked"] is False


def test_null_door_locks_stay_unknown_without_car_lock():
    d = _parse_smartcar_signals([_sig("closure-doors", UNLOCKED_DOORS)])
    assert d["closure"]["doors"]["frontLeft"]["isLocked"] is None


@respx.mock
async def test_unlock_webhook_shows_doors_unlocked(hass, hass_client_no_auth):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER,
                            data={"client_id": API_CLIENT_ID, "client_secret": "s", "user_id": USER, "management_token": MGMT})
    entry.add_to_hass(hass)
    _mock_token(); _mock_connections([_conn()]); _mock_vin()
    respx.get(f"{V3}/vehicles/{VEH}/signals").mock(return_value=httpx.Response(200, json=LIVE_SNAPSHOT))
    respx.route(host="vehicle.api.smartcar.com").mock(return_value=httpx.Response(404, json={"errors": []}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fl = "binary_sensor.2024_nissan_leaf_front_left_door_lock"
    assert hass.states.get(fl).state == "off"  # locked at startup

    payload = {**VEHICLE_STATE, "data": {**VEHICLE_STATE["data"], "signals": [
        _sig("closure-doors", UNLOCKED_DOORS), _sig("closure-reartrunk", {"isOpen": False, "isLocked": None}),
        _sig("closure-islocked", {"value": False})]}}
    raw = json.dumps(payload).encode()
    sig = hmac.new(MGMT.encode(), raw, hashlib.sha256).hexdigest()
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{entry.data['webhook_id']}", data=raw,
                             headers={"Content-Type": "application/json", "SC-Signature": sig})
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get(fl).state == "on"  # unlocked, not unknown
    assert hass.states.get("binary_sensor.2024_nissan_leaf_rear_trunk_lock").state == "on"
    assert hass.states.get("lock.2024_nissan_leaf_door_lock").state == "unlocked"
    assert await hass.config_entries.async_unload(entry.entry_id)
