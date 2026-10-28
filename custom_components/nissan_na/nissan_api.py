"""
Smartcar API client for Nissan vehicles.

Uses the OAuth 2.0 client-credentials grant for all API calls after the
initial Smartcar Connect flow.  No per-user refresh tokens are stored —
a short-lived application token is fetched automatically as needed.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

_LOGGER = logging.getLogger(__name__)


def _namedtuple_to_dict(obj: Any) -> Dict[str, Any]:
    """Convert a namedtuple (or any object with __dict__) to a dictionary."""
    if isinstance(obj, dict):
        return obj
    elif hasattr(obj, "_asdict"):
        result = obj._asdict()
        return {
            k: _namedtuple_to_dict(v) if hasattr(v, "_asdict") else v
            for k, v in result.items()
        }
    else:
        return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_")}


class Vehicle(BaseModel):
    """Model representing a Nissan vehicle."""

    vin: str
    id: str
    model: Optional[str] = None
    year: Optional[int] = None
    make: Optional[str] = None


class SmartcarCommandError(Exception):
    """A Smartcar v3 command was rejected or failed (message is user-facing)."""


class SmartcarApiClient:
    """Client for Nissan vehicles via Smartcar API.

    Authentication uses the client-credentials grant: the user completes
    Smartcar Connect once (to associate their vehicle), we capture their
    user_id, then all subsequent API calls use short-lived app-level tokens
    that are fetched and cached automatically — no user interaction needed.
    """

    _IAM_TOKEN_URL = "https://iam.smartcar.com/oauth2/token"
    _USER_URL = "https://api.smartcar.com/v2.0/user"
    _V3_BASE = "https://vehicle.api.smartcar.com/v3"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_id: Optional[str] = None,
        test_mode: bool = False,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.test_mode = test_mode
        # Client-credentials token cache
        self._cc_token: Optional[str] = None
        self._cc_token_expires_at: float = 0.0
        # Vehicle / signal caches
        self._vehicle_model_cache: Dict[str, Any] = {}
        self._vehicle_signals_cache: Dict[str, List[str]] = {}
        self._permission_denied_signals: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Token management (client-credentials)
    # ------------------------------------------------------------------

    async def _get_access_token(self) -> str:
        """Return a valid client-credentials access token, fetching one if expired."""
        if self._cc_token and time.monotonic() < self._cc_token_expires_at:
            return self._cc_token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._IAM_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Client-credentials token request failed"
                    f" ({resp.status_code}): {resp.text}"
                )
            data = resp.json()

        token: str = data["access_token"]
        self._cc_token = token
        # Expire 60 s early to avoid edge-case expiry mid-request
        self._cc_token_expires_at = time.monotonic() + data.get("expires_in", 3600) - 60
        _LOGGER.debug("Obtained new client-credentials access token")
        return token

    async def _api_headers(self) -> Dict[str, str]:
        """Return auth headers for Smartcar v3 API calls."""
        token = await self._get_access_token()
        headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        if self.user_id:
            headers["sc-user-id"] = self.user_id
        return headers

    # ------------------------------------------------------------------
    # Generic v3 HTTP helpers
    # ------------------------------------------------------------------

    async def _v3_get(self, vehicle_id: str, path: str) -> Dict[str, Any]:
        """GET a v3 vehicle endpoint and return the attributes dict."""
        headers = await self._api_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._V3_BASE}/vehicles/{vehicle_id}/{path}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        node = data.get("data", {})
        if isinstance(node, dict):
            return node.get("attributes", data)
        return data

    async def _v3_post(
        self, vehicle_id: str, path: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST to a v3 vehicle endpoint and return the full JSON response."""
        headers = await self._api_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._V3_BASE}/vehicles/{vehicle_id}/{path}",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    # Commands may run ~175 s; Smartcar then answers 202 and streams the result
    _COMMAND_TIMEOUT = httpx.Timeout(190.0, connect=15.0)

    @staticmethod
    def _error_text(err: Dict[str, Any], fallback: str) -> str:
        title = err.get("title") or err.get("code") or fallback
        detail = err.get("detail") or err.get("description") or ""
        return f"{title}: {detail}" if detail else str(title)

    async def _v3_command(
        self, vehicle_id: str, command: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """POST /v3/vehicles/{id}/commands/{command} and return the result.

        Raises SmartcarCommandError with Smartcar's own error text when the
        request is rejected (4xx/5xx) or the command reports FAILURE.
        """
        headers = await self._api_headers()
        url = f"{self._V3_BASE}/vehicles/{vehicle_id}/commands/{command}"
        async with httpx.AsyncClient(timeout=self._COMMAND_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code not in (200, 202):
            errors = data.get("errors") or [{}]
            raise SmartcarCommandError(
                f"Smartcar rejected {command} (HTTP {resp.status_code}) - "
                + self._error_text(errors[0], resp.reason_phrase)
            )
        status = ((data.get("data") or {}).get("attributes") or {}).get("status") or {}
        if status.get("value") not in (None, "SUCCESS"):
            raise SmartcarCommandError(
                f"{command} failed - "
                + self._error_text(status.get("error") or {}, str(status.get("value")))
            )
        _LOGGER.debug("Command %s succeeded for vehicle %s", command, vehicle_id)
        return data

    # ------------------------------------------------------------------
    # Vehicle list (Connections API)
    # ------------------------------------------------------------------

    async def async_validate_credentials(self) -> None:
        """Raise if the API client ID / secret cannot obtain a token."""
        self._cc_token = None
        self._cc_token_expires_at = 0.0
        await self._get_access_token()

    @staticmethod
    def _iter_connections(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the list of connection objects from a Connections response.

        Smartcar v3 returns JSON:API (``data: [...]``); the older
        ``connections: [...]`` shape is accepted as a fallback.
        """
        items = data.get("data")
        if isinstance(items, list):
            return items
        legacy = data.get("connections")
        return legacy if isinstance(legacy, list) else []

    @staticmethod
    def _parse_connection(
        conn: Dict[str, Any],
    ) -> tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """Extract (vehicle_id, user_id, vehicle_attributes) from a connection."""
        relationships = conn.get("relationships") or {}
        vehicle_rel = (relationships.get("vehicle") or {}).get("data") or {}
        user_rel = (relationships.get("user") or {}).get("data") or {}
        attrs = conn.get("attributes") or {}
        user = attrs.get("user") or {}
        vehicle_id = vehicle_rel.get("id") or conn.get("vehicleId")
        user_id = user.get("id") or user_rel.get("id") or conn.get("userId")
        vehicle_attrs = attrs.get("vehicle") or {}
        return vehicle_id, user_id, vehicle_attrs

    async def _get_connections(
        self, user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """GET /v3/connections, following page-number pagination."""
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if user_id:
            headers["sc-user-id"] = user_id

        results: List[Dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                params: Dict[str, Any] = {"page[number]": page, "page[size]": 50}
                if user_id:
                    params["filter[userId]"] = user_id
                resp = await client.get(
                    f"{self._V3_BASE}/connections", headers=headers, params=params
                )
                if resp.status_code != 200:
                    raise ValueError(
                        f"Connections API failed ({resp.status_code}): {resp.text}"
                    )
                data = resp.json()
                items = self._iter_connections(data)
                results.extend(items)
                total = (data.get("meta") or {}).get("totalCount")
                if not items or not total or len(results) >= int(total):
                    break
                page += 1
        _LOGGER.debug("Connections API returned %d connection(s)", len(results))
        return results

    async def discover_user_ids(self) -> List[str]:
        """Return the distinct Smartcar user IDs connected to this application.

        Vehicle Simulator connections (``mode: simulated``) are ignored unless
        they are the only ones, so a dashboard test vehicle doesn't make a
        single-car account look like two users.
        """
        live: List[str] = []
        simulated: List[str] = []
        for conn in await self._get_connections():
            _, user_id, vehicle_attrs = self._parse_connection(conn)
            if not user_id:
                continue
            bucket = simulated if vehicle_attrs.get("mode") == "simulated" else live
            if user_id not in bucket:
                bucket.append(user_id)
        if simulated:
            _LOGGER.debug("Simulated-vehicle user IDs: %s", simulated)
        return live or simulated

    async def get_vehicle_list(self) -> List[Vehicle]:
        """Return all vehicles connected for this user via the Connections API."""
        if not self.user_id:
            raise ValueError("user_id is not set.")

        connections = await self._get_connections(self.user_id)
        headers = await self._api_headers()

        vehicles: List[Vehicle] = []
        seen: set[str] = set()
        for conn in connections:
            vehicle_id, conn_user_id, vehicle_attrs = self._parse_connection(conn)
            if not vehicle_id or vehicle_id in seen:
                continue
            if conn_user_id and conn_user_id != self.user_id:
                continue
            seen.add(vehicle_id)
            embedded = vehicle_attrs if vehicle_attrs.get("make") else None
            try:
                vehicle = await self._fetch_vehicle_attributes(
                    vehicle_id, headers, embedded
                )
                vehicles.append(vehicle)
                self._vehicle_model_cache[vehicle_id] = vehicle
            except Exception as err:
                _LOGGER.warning(
                    "Failed to get attributes for vehicle %s: %s", vehicle_id, err
                )

        return vehicles

    async def _fetch_vehicle_attributes(
        self,
        vehicle_id: str,
        headers: Optional[Dict[str, str]] = None,
        embedded: Optional[Dict[str, Any]] = None,
    ) -> Vehicle:
        """Fetch make/model/year and VIN via v3 HTTP calls.

        If `embedded` is provided (from the connections response attributes),
        only a VIN fetch is performed if the VIN is absent from the embedded data.
        """
        if headers is None:
            headers = await self._api_headers()

        # Use embedded vehicle info from connections payload if available
        if embedded:
            make = embedded.get("make")
            model = embedded.get("model")
            year_raw = embedded.get("year")
            vin = embedded.get("vin", "")

            # If VIN is missing, fetch it separately
            if not vin:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self._V3_BASE}/vehicles/{vehicle_id}/vin", headers=headers
                    )
                    vin_data = resp.json() if resp.status_code == 200 else {}
                vin = vin_data.get("data", {}).get("attributes", {}).get("vin", "")

            year = int(year_raw) if year_raw else None
            return Vehicle(id=vehicle_id, vin=vin, make=make, model=model, year=year)

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._V3_BASE}/vehicles/{vehicle_id}", headers=headers
            )
            attrs_data = resp.json() if resp.status_code == 200 else {}

            resp = await client.get(
                f"{self._V3_BASE}/vehicles/{vehicle_id}/vin", headers=headers
            )
            vin_data = resp.json() if resp.status_code == 200 else {}

        attrs = attrs_data.get("data", {}).get("attributes", {})
        vin_attrs = vin_data.get("data", {}).get("attributes", {})
        year = attrs.get("year")

        return Vehicle(
            id=vehicle_id,
            vin=vin_attrs.get("vin", ""),
            make=attrs.get("make"),
            model=attrs.get("model"),
            year=int(year) if year else None,
        )

    # ------------------------------------------------------------------
    # Vehicle info
    # ------------------------------------------------------------------

    async def get_vehicle_info(self, vehicle_id: str) -> Dict[str, Any]:
        """Get vehicle information (make, model, year, VIN)."""
        if vehicle_id in self._vehicle_model_cache:
            v = self._vehicle_model_cache[vehicle_id]
            return {
                "id": vehicle_id,
                "make": v.make,
                "model": v.model,
                "year": v.year,
                "vin": v.vin,
            }
        vehicle = await self._fetch_vehicle_attributes(vehicle_id)
        self._vehicle_model_cache[vehicle_id] = vehicle
        return {
            "id": vehicle_id,
            "make": vehicle.make,
            "model": vehicle.model,
            "year": vehicle.year,
            "vin": vehicle.vin,
        }

    # ------------------------------------------------------------------
    # Read methods (direct v3 HTTP)
    # ------------------------------------------------------------------

    async def get_vehicle_location(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "location")

    async def get_battery_level(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "battery")

    async def get_battery_capacity(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "battery/capacity")

    async def get_charge_status(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "charge")

    async def get_odometer(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "odometer")

    async def get_fuel_level(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "fuel")

    async def get_lock_status(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "security")

    async def get_tire_pressure(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "tires/pressure")

    async def get_engine_oil(self, vehicle_id: str) -> Dict[str, Any]:
        return await self._v3_get(vehicle_id, "engine/oil")

    async def get_permissions(self, vehicle_id: str) -> List[str]:
        data = await self._v3_get(vehicle_id, "permissions")
        return data.get("permissions", [])

    # ------------------------------------------------------------------
    # Vehicle action methods (direct v3 HTTP)
    # ------------------------------------------------------------------

    async def lock_doors(self, vehicle_id: str) -> Dict[str, Any]:
        result = await self._v3_command(vehicle_id, "security/lock")
        _LOGGER.debug("Lock command sent for vehicle %s", vehicle_id)
        return result

    async def unlock_doors(self, vehicle_id: str) -> Dict[str, Any]:
        result = await self._v3_command(vehicle_id, "security/unlock")
        _LOGGER.debug("Unlock command sent for vehicle %s", vehicle_id)
        return result

    async def start_charge(self, vehicle_id: str) -> Dict[str, Any]:
        result = await self._v3_command(vehicle_id, "charge/start")
        _LOGGER.debug("Start charge command sent for vehicle %s", vehicle_id)
        return result

    async def stop_charge(self, vehicle_id: str) -> Dict[str, Any]:
        result = await self._v3_command(vehicle_id, "charge/stop")
        _LOGGER.debug("Stop charge command sent for vehicle %s", vehicle_id)
        return result

    async def set_charge_limit(self, vehicle_id: str, percent: int) -> Dict[str, Any]:
        result = await self._v3_command(
            vehicle_id,
            "charge/set-limit",
            {"data": {"attributes": {"percent": int(percent)}}},
        )
        _LOGGER.debug("Charge limit %s%% sent for vehicle %s", percent, vehicle_id)
        return result

    # ------------------------------------------------------------------
    # Climate control
    # ------------------------------------------------------------------

    _NO_CLIMATE = (
        "Smartcar's v3 API has no climate command, so cabin climate can't be "
        "controlled through Smartcar (use the NissanConnect app)."
    )

    async def start_climate(self, vehicle_id: str) -> Dict[str, Any]:
        raise SmartcarCommandError(self._NO_CLIMATE)

    async def stop_climate(self, vehicle_id: str) -> Dict[str, Any]:
        raise SmartcarCommandError(self._NO_CLIMATE)

    async def get_climate_status(self, vehicle_id: str) -> Dict[str, Any]:
        headers = await self._api_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._V3_BASE}/vehicles/{vehicle_id}/climate",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self, vehicle_id: str) -> bool:
        headers = await self._api_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._V3_BASE}/vehicles/{vehicle_id}", headers=headers
            )
            success = resp.status_code in (200, 204)
        if success:
            self._vehicle_model_cache.pop(vehicle_id, None)
            self._vehicle_signals_cache.pop(vehicle_id, None)
        return success

    # ------------------------------------------------------------------
    # Signals / compatibility
    # ------------------------------------------------------------------

    async def get_compatibility_signals(
        self, make: str, model: str, year: int
    ) -> List[str]:
        """Get supported signal codes from the public compatibility API.
        No auth needed.
        """
        url = "https://compatibility.api.smartcar.com/v3/compatible-vehicles"
        params = {"filter[make]": make.upper(), "filter[region]": "US"}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception as err:
            _LOGGER.debug("Compatibility API error: %s", err)
            return []

        signals: List[str] = []
        for entry in data.get("data", []):
            attrs = entry.get("attributes", {})
            if attrs.get("model", "").lower() != model.lower():
                continue
            years = attrs.get("years", {})
            if not (years.get("start", 0) <= year <= years.get("end", 9999)):
                continue
            for cap in attrs.get("capabilities", []):
                if cap.get("type") == "signal":
                    code = cap.get("capability")
                    if code and code not in signals:
                        signals.append(code)
            if signals:
                _LOGGER.info(
                    "Compatibility API: %d signals for %s %s %d",
                    len(signals),
                    make,
                    model,
                    year,
                )
                return signals

        _LOGGER.debug("No compatibility match for %s %s %d", make, model, year)
        return []

    async def get_vehicle_signals(
        self, vehicle_id: str, vehicle: Any = None
    ) -> List[str]:
        """Return available signal codes for a vehicle (cached after first call)."""
        if vehicle_id in self._vehicle_signals_cache:
            return self._vehicle_signals_cache[vehicle_id]

        _LOGGER.debug("Fetching signals for vehicle %s", vehicle_id)
        headers = await self._api_headers()
        url = f"{self._V3_BASE}/vehicles/{vehicle_id}/signals"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if "data" in data and isinstance(data["data"], list):
                        all_sigs = data["data"]

                        permission_codes = [
                            sig["attributes"]["code"]
                            for sig in all_sigs
                            if isinstance(sig, dict)
                            and "attributes" in sig
                            and "code" in sig.get("attributes", {})
                            and sig["attributes"]
                            .get("status", {})
                            .get("error", {})
                            .get("type")
                            == "PERMISSION"
                        ]
                        if permission_codes:
                            self._permission_denied_signals[vehicle_id] = (
                                permission_codes
                            )
                            _LOGGER.warning(
                                "Vehicle %s: %d signal(s) have PERMISSION errors: %s",
                                vehicle_id,
                                len(permission_codes),
                                permission_codes,
                            )
                        else:
                            self._permission_denied_signals.pop(vehicle_id, None)

                        signals = [
                            sig["attributes"]["code"]
                            for sig in all_sigs
                            if isinstance(sig, dict)
                            and "attributes" in sig
                            and "code" in sig["attributes"]
                            and sig["attributes"].get("status", {}).get("value")
                            == "SUCCESS"
                        ]
                        if signals:
                            _LOGGER.info(
                                "v3 signals endpoint: %d signals for vehicle %s",
                                len(signals),
                                vehicle_id,
                            )
                            self._vehicle_signals_cache[vehicle_id] = signals
                            return signals
                else:
                    _LOGGER.debug(
                        "v3 signals endpoint returned %d for vehicle %s — "
                        "falling back to compatibility API",
                        resp.status_code,
                        vehicle_id,
                    )
        except Exception as err:
            _LOGGER.debug("v3 signals endpoint error for %s: %s", vehicle_id, err)

        # Fall back to compatibility API
        v = vehicle or self._vehicle_model_cache.get(vehicle_id)
        if v and v.make and v.model and v.year:
            signals = await self.get_compatibility_signals(v.make, v.model, v.year)
            if signals:
                self._vehicle_signals_cache[vehicle_id] = signals
                return signals

        _LOGGER.warning(
            "Could not determine signals for vehicle %s — no sensors will be created",
            vehicle_id,
        )
        self._vehicle_signals_cache[vehicle_id] = []
        return []

    def get_permission_denied_signals(self, vehicle_id: str) -> List[str]:
        """Return signal codes that returned PERMISSION errors for the given vehicle."""
        return self._permission_denied_signals.get(vehicle_id, [])

    # ------------------------------------------------------------------
    # Vehicle status (bulk signals call)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_signal_body(sdk_result: dict) -> dict:
        """Extract the signal data body from a v3 SDK get_signal() result."""
        body = sdk_result.get("body", {})
        if not isinstance(body, dict):
            return {}
        data = body.get("data")
        if isinstance(data, dict):
            attrs = data.get("attributes", {})
            if isinstance(attrs, dict) and "body" in attrs:
                return attrs["body"] or {}
        attrs = body.get("attributes", {})
        if isinstance(attrs, dict) and "body" in attrs:
            return attrs["body"] or {}
        return body

    # Smartcar row/column grid -> position names (doors, windows, tires)
    _GRID_POS = {
        (0, 0): "frontLeft",
        (0, 1): "frontRight",
        (1, 0): "backLeft",
        (1, 1): "backRight",
    }

    @staticmethod
    def _val(body: dict, *keys: str) -> Any:
        """Return the first non-None of body[key]..., then body["value"].

        v3 signals mostly use ``{"value": ..., "unit": ...}``; ``is not None``
        keeps legitimate False / 0 readings.
        """
        for key in (*keys, "value"):
            val = body.get(key)
            if val is not None:
                return val
        return None

    @staticmethod
    def _fill_lock_fallback(status: dict) -> None:
        """Use the car-level lock state where a door/trunk reports isLocked null.

        After an unlock, Nissan reports each door's (and the trunk's) isLocked
        as null and only keeps closure-islocked. Without this the per-door lock
        sensors go unknown.
        """
        overall = (status.get("lock") or {}).get("is_locked")
        if overall is None:
            return
        closure = status.get("closure") or {}
        for entry in (closure.get("doors") or {}).values():
            if isinstance(entry, dict) and "isLocked" in entry and entry["isLocked"] is None:
                entry["isLocked"] = overall
        for key in ("frontTrunk", "rearTrunk"):
            entry = closure.get(key)
            if isinstance(entry, dict) and "isLocked" in entry and entry["isLocked"] is None:
                entry["isLocked"] = overall

    @staticmethod
    def _apply_signal_to_status(status: dict, signal_code: str, body: dict) -> None:
        """Map a v3 signal body into the shared status dict.

        Used for both REST signals and webhook VEHICLE_STATE payloads, so the
        shape matches what the entities read (``battery.percentRemaining``,
        ``closure.doors.frontLeft.isOpen``, ``lock.is_locked``, ...).
        """
        if not body:
            return
        _val = SmartcarApiClient._val
        grid = SmartcarApiClient._GRID_POS

        code = signal_code.lower()
        _LOGGER.debug("Applying signal %s body: %s", code, body)

        if code == "odometer-traveleddistance":
            status.setdefault("odometer", {})["distance"] = (
                body.get("traveledDistance")
                or body.get("distance")
                or body.get("value")
            )

        elif code == "internalcombustionengine-fuellevel":
            status.setdefault("fuel", {})["percentRemaining"] = (
                body.get("fuelLevel")
                or body.get("percentRemaining")
                or body.get("value")
            )

        elif code == "internalcombustionengine-amountremaining":
            status.setdefault("fuel", {})["amountRemaining"] = body.get(
                "amountRemaining"
            ) or body.get("value")

        elif code == "internalcombustionengine-range":
            status.setdefault("fuel", {})["range"] = body.get("range") or body.get(
                "value"
            )

        elif code == "location-preciselocation":
            status["location"] = {
                "latitude": body.get("latitude"),
                "longitude": body.get("longitude"),
            }

        elif code == "wheel-tires":
            _ROW_COL_TO_POS = {
                (0, 0): "frontLeft",
                (0, 1): "frontRight",
                (1, 0): "backLeft",
                (1, 1): "backRight",
            }
            tires: dict = {}
            for entry in body.get("values", []):
                if not isinstance(entry, dict):
                    continue
                row = entry.get("row")
                col = entry.get("column")
                pressure = (
                    entry.get("tirePressure")
                    if entry.get("tirePressure") is not None
                    else entry.get("pressure")
                )
                pos = (
                    _ROW_COL_TO_POS.get((row, col))
                    if row is not None and col is not None
                    else None
                )
                if pos and pressure is not None:
                    tires[pos] = {"pressure": pressure}
            if tires:
                status["tires"] = tires

        elif code == "diagnostics-tirepressure":
            status.setdefault("diagnostics", {})["tirePressureWarning"] = body.get(
                "status"
            )

        elif code == "closure-islocked":
            is_locked = _val(body, "isLocked")
            status.setdefault("security", {})["isLocked"] = is_locked
            status["lock"] = {"is_locked": is_locked}

        elif code in ("closure-doors", "closure-windows"):
            kind = "doors" if code == "closure-doors" else "windows"
            # Docs show {"doors": [...]}; the live Ariya sends {"values": [...]}
            entries = body.get(kind) or body.get("values") or []
            status.setdefault("security", {})[kind] = entries
            positions: dict = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                pos = grid.get((entry.get("row"), entry.get("column")))
                if pos:
                    positions[pos] = {
                        k: entry[k] for k in ("isOpen", "isLocked") if k in entry
                    }
            if positions:
                status.setdefault("closure", {})[kind] = positions

        elif code in (
            "closure-reartrunk",
            "closure-fronttrunk",
            "closure-enginecover",
            "closure-sunroof",
        ):
            key = {
                "closure-reartrunk": "rearTrunk",
                "closure-fronttrunk": "frontTrunk",
                "closure-enginecover": "engineCover",
                "closure-sunroof": "sunroof",
            }[code]
            status.setdefault("security", {})[key] = body
            status.setdefault("closure", {})[key] = body

        elif code == "diagnostics-mil":
            status.setdefault("diagnostics", {})["mil"] = body.get("status")

        elif code == "diagnostics-abs":
            status.setdefault("diagnostics", {})["abs"] = body.get("status")

        elif code == "diagnostics-brakefluid":
            status.setdefault("diagnostics", {})["brakeFluid"] = body.get("status")

        elif code == "diagnostics-airbag":
            status.setdefault("diagnostics", {})["airbag"] = body.get("status")

        elif code == "diagnostics-oilpressure":
            status.setdefault("diagnostics", {})["oilPressure"] = body.get("status")

        elif code == "tractionbattery-stateofcharge":
            status.setdefault("battery", {})["percentRemaining"] = _val(
                body, "stateOfCharge", "percentRemaining"
            )

        elif code == "tractionbattery-range":
            status.setdefault("battery", {})["range"] = _val(body, "range")

        elif code == "tractionbattery-nominalcapacity":
            status.setdefault("battery", {})["capacityKwh"] = body.get(
                "capacity"
            ) or body.get("nominalCapacity")

        elif code == "charge-ischarging":
            status.setdefault("charge", {})["isCharging"] = body.get(
                "isCharging"
            ) or body.get("value")

        elif code == "charge-ischargingcableconnected":
            status.setdefault("charge", {})["isPluggedIn"] = body.get(
                "isChargingCableConnected"
            ) or body.get("value")

        elif code == "charge-timetocomplete":
            status.setdefault("charge", {})["timeToComplete"] = body.get(
                "timeToComplete"
            ) or body.get("value")

        elif code == "charge-detailedchargingstatus":
            status.setdefault("charge", {})["state"] = (
                body.get("detailedChargingStatus")
                or body.get("status")
                or body.get("value")
            )

        elif code == "charge-chargetimers":
            status.setdefault("charge", {})["chargeTimers"] = body

        elif code == "charge-amperage":
            status.setdefault("charge", {})["amperage"] = _val(body, "amperage")

        elif code == "charge-amperagemax":
            status.setdefault("charge", {})["amperageMax"] = _val(body, "amperageMax")

        elif code == "charge-chargelimits":
            limit = body.get("activeLimit")
            if limit is None:
                for entry in body.get("values") or []:
                    if isinstance(entry, dict) and entry.get("type") == "GLOBAL":
                        limit = entry.get("limit")
                        break
            if limit is not None:
                status.setdefault("charge", {})["limit"] = limit

        elif code == "charge-isfastchargerpresent":
            status.setdefault("charge", {})["isFastChargerConnected"] = _val(body)

        elif code in (
            "connectivitystatus-isonline",
            "connectivitystatus-isasleep",
            "connectivitystatus-isdigitalkeypaired",
        ):
            key = {
                "connectivitystatus-isonline": "isOnline",
                "connectivitystatus-isasleep": "isAsleep",
                "connectivitystatus-isdigitalkeypaired": "isDigitalKeyPaired",
            }[code]
            status.setdefault("connectivity", {})[key] = _val(body)

        elif code == "surveillance-isenabled":
            status.setdefault("surveillance", {})["isEnabled"] = _val(body)

        elif code == "tractionbattery-isheateractive":
            status.setdefault("tractionBattery", {})["isHeaterActive"] = _val(body)

        elif code == "charge-voltage":
            status.setdefault("charge", {})["voltage"] = body.get(
                "voltage"
            ) or body.get("value")

        elif code == "charge-wattage":
            status.setdefault("charge", {})["wattage"] = body.get(
                "wattage"
            ) or body.get("value")

        elif code == "hvac-cabintargettemperature":
            status.setdefault("hvac", {})["targetTemperature"] = body.get(
                "cabinTargetTemperature"
            ) or body.get("value")

        elif code == "hvac-iscabinhvacactive":
            status.setdefault("hvac", {})["isActive"] = body.get(
                "isCabinHVACActive"
            ) or body.get("value")

        else:
            _LOGGER.debug("No status mapping for signal %s — storing raw body", code)
            status[code] = body

    async def get_vehicle_status(self, vehicle_id: str) -> Dict[str, Any]:
        """Get comprehensive vehicle status via bulk call to v3 signals endpoint."""
        headers = await self._api_headers()
        url = f"{self._V3_BASE}/vehicles/{vehicle_id}/signals"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    _LOGGER.warning(
                        "Signals endpoint returned %d for vehicle %s",
                        resp.status_code,
                        vehicle_id,
                    )
                    return {}
                data = resp.json()
        except Exception as err:
            _LOGGER.error("Failed to fetch vehicle status for %s: %s", vehicle_id, err)
            return {}

        signal_list = data.get("data", [])

        success_codes = [
            sig["attributes"]["code"]
            for sig in signal_list
            if isinstance(sig, dict)
            and "attributes" in sig
            and "code" in sig.get("attributes", {})
            and sig.get("attributes", {}).get("status", {}).get("value") == "SUCCESS"
        ]
        if success_codes:
            self._vehicle_signals_cache[vehicle_id] = success_codes

        permission_codes = [
            sig["attributes"]["code"]
            for sig in signal_list
            if isinstance(sig, dict)
            and "attributes" in sig
            and "code" in sig.get("attributes", {})
            and sig.get("attributes", {}).get("status", {}).get("error", {}).get("type")
            == "PERMISSION"
        ]
        if permission_codes:
            self._permission_denied_signals[vehicle_id] = permission_codes
        else:
            self._permission_denied_signals.pop(vehicle_id, None)

        status: Dict[str, Any] = {}
        for signal in signal_list:
            attrs = signal.get("attributes", {})
            code = attrs.get("code", "")
            if not code:
                continue
            if attrs.get("status", {}).get("value") != "SUCCESS":
                continue
            body = attrs.get("body")
            if body:
                self._apply_signal_to_status(status, code, body)

        self._fill_lock_fallback(status)
        _LOGGER.debug("Vehicle status for %s: %s", vehicle_id, status)
        return status
