# Changelog

All notable changes to this project will be documented in this file.

## [0.21.9.4] - 2026-11-01 (fork: amaisano)

### Fixed

- **Blocking call at startup.** Every Smartcar request built its own `httpx.AsyncClient`, which loads the CA bundle
  inside the event loop, so HA logged "Detected blocking call to load_verify_locations ... by custom integration
  'nissan_na'" on each start. The client now uses Home Assistant's shared httpx client (`get_async_client`); the
  190 s command timeout moved to the request.

## [0.21.9.3] - 2026-10-30 (fork: amaisano)

### Fixed

- **OEM outages no longer blank the entities.** When Smartcar's latest read of a signal fails upstream (Nissan), the
  `/signals` entry has status `ERROR`/`UPSTREAM` but still carries the last good body (see `meta.retrievedAt`). Those
  bodies were dropped, so a restart during a Nissan outage left battery, lock and the rest `unknown`. They are now
  used; permission and compatibility errors are still ignored.

## [0.21.9.2] - 2026-10-30 (fork: amaisano)

### Fixed

- **Hourly poll** made one `/signals` call per sensor (it called each sensor's `async_update()`), plus the loop's own
  call, and threw the loop's result away. It also logged an error every hour for each sensor disabled in the registry
  (HA never adds those, so their `hass` is None), and the refreshed values were never written to the state machine.
  Now each vehicle is fetched once and the result is pushed to its live sensors, which write their state.
- **`nissan_na.refresh_status`** fetched the status and discarded it; it now updates the sensors.
- **Options > Refresh sensors** fetches once per vehicle instead of once per sensor.

## [0.21.9.1] - 2026-10-28 (fork: amaisano)

Fixes on top of upstream v0.21.9. Without them no data reached the entities.

### Fixed

- **Config flow:** asks for the Smartcar API Credentials client ID and secret and discovers the user ID through
  `GET /v3/connections` (JSON:API `data[]`, `filter[userId]`, pagination), because HA's OAuth callback drops
  Smartcar's `user_id` (#39). Vehicle Simulator connections are ignored. Legacy OAuth entries still load.
  `translations/en.json` was missing.
- **Signals and webhooks:** v3 signal bodies are `{"value": ...}` (SoC, range and isLocked were read as None). The v4
  webhook parser dropped every successful signal (they carry no `status`) and keyed data by signal code instead of the
  nested shape the entities read. Closure, connectivity, charge-limit and amperage mappings were missing.
- **Thread safety:** all six entity webhook handlers lacked `@callback`, so `async_write_ha_state` ran from a worker
  thread (RuntimeError on HA 2026.x).
- **device_tracker** asked for the status of the VIN instead of the vehicle ID.
- **Binary sensors** are created from signal codes (they never were), start `unknown` instead of `off`, are seeded from
  the REST snapshot at setup, and accept `closure-doors` under `values` (live payloads) as well as `doors`.
- **Lock-class binary sensors** are inverted to match HA (on = unlocked). After an unlock, null per-door/trunk
  `isLocked` values are filled from the car-level lock.
- **Options flow** no longer wipes `entry.options` on save. **Rebuild Sensors / Re-load Entities** no longer delete
  sensors or build phantom ones; both reload the entry.
- **Commands** use v3 `POST /v3/vehicles/{id}/commands/...` (lock, unlock, charge start/stop, set-limit) with a 190 s
  timeout for 202 keep-alive results. Failures raise `HomeAssistantError` with Smartcar's error text. Climate
  start/stop says Smartcar v3 has no climate command instead of returning 404.

### Added

- `state_class` on numeric sensors (odometer: `total_increasing`) for long-term statistics.
- `tests/integration`: 40 tests that run the integration in a real hass (pytest-homeassistant-custom-component +
  respx). Upstream unit tests updated for the fixed behavior.

## [Unreleased]

### Added

#### Comprehensive Sensor Coverage

- **31 Sensors** (expanded from 7) covering:
  - **Battery**: Percentage, Range, Capacity, Low Voltage Battery
  - **Charging**: State, Voltage, Current, Power, Time to Complete, Max Current, Plugged In Status
  - **Fuel**: Amount, Percentage, Range
  - **Tires**: Pressure for all 4 wheels (FL, FR, RL, RR)
  - **Vehicle Status**: Current Gear, Oil Life, Software Version
  - **Location**: Latitude, Longitude
  - **Connectivity**: Online, Asleep, Digital Key Paired
  - **Other**: Surveillance, Battery Heater, Charge Limit, Odometer

#### Binary Sensors (NEW)

- **25 Binary Sensors** covering:
  - **Doors**: All 4 doors (open + lock status)
  - **Windows**: All 4 windows (open status)
  - **Trunks**: Front/Rear trunks (open + lock status)
  - **Connectivity**: Online, Asleep, Digital Key Paired
  - **Charging**: Cable plugged, Fast charger connected
  - **Other**: Sunroof, Engine cover, Battery heater, Surveillance

#### Vehicle Control (NEW)

- **Charging Switch**: Start/stop charging remotely
- **Charge Limit Number**: Set target charge percentage (0-100%)

#### Intelligent Signal Validation

- **Dynamic entity creation** based on vehicle capabilities using Smartcar signals API
- **Boot behavior**: Only adds new sensors, never removes existing ones for stability
- **Rebuild Sensors option**: Manual validation and cleanup via configuration menu
  - Removes sensors no longer supported by vehicle
  - Adds newly available sensors
  - Validates all sensors against current vehicle capabilities
  - Shows summary of changes (added/removed/total)
- Entities validated at setup using signals API
- Clean sensor management without breaking existing automations

#### Enhanced Configuration

- **Entity Reload Option**: Discover and load new sensors after upgrades
- **Signal Discovery**: Automatic detection of available vehicle features
- **Diagnostics Support**: View webhook URL and integration status
- **Entity Descriptions**: Comprehensive strings.json with all entity descriptions

#### Webhook Enhancements

- Real-time state updates for all entity types
- Nested data structure parsing for complex signals
- Async dispatcher pattern for efficient updates
- Comprehensive webhook logging for troubleshooting

#### Developer Features

- Complete Home Assistant device classes and icons
- Unique ID persistence for all entities
- Device registry integration
- Backward compatibility maintained
- Zero breaking changes

### Improved

- **Sensor state management** using proper `SensorEntity` base class
- **Device tracker** now supports real-time webhook location updates
- **Automatic token refresh** - eliminates most manual re-authorization needs
- **Comprehensive debug logging** throughout webhook lifecycle
- **Error handling** with graceful fallbacks at all levels
- **OAuth permissions** expanded to 32 comprehensive permissions

### Technical

- Total entity count: **58** (from 10, +480% increase)
- Signal coverage: **31/51** Smartcar signals (61%)
- Code additions: ~1,200 lines
- New platforms: binary_sensor, switch, number
- Python requirement: 3.14+
- Home Assistant: 2024.1+
  - Added read_charge_locations, read_charge_records for EV charging history
  - Added control_navigation for sending destinations to vehicle
  - Added control_trunk for remote trunk/frunk control
  - Total of 32 permissions requested to maximize feature availability
- **Climate control platform**
  - Supports start/stop HVAC system via climate entity
  - Uses direct Smartcar REST API calls (not available in Python SDK)
  - Includes HEAT, COOL, AUTO, and OFF modes
  - Temperature control not supported (API limitation)

### Fixed

- **Sensor state unavailability** - Sensors now properly expose state through SensorEntity
  - Fixed Entity → SensorEntity inheritance
  - Proper state property naming (native_value vs state)
  - Handles nested API responses with metadata
- **Device tracker unavailability** - Device tracker now receives webhook updates
  - Subscribes to webhook signals like sensors
  - Proper state management through TrackerEntity
  - Location updates trigger through webhook data
- **Vehicle object cache** - Cache now cleared on token operations
  - Ensures vehicle objects always have fresh authentication credentials
  - Prevents authentication failures from stale cached vehicles
- **Reauth flow creates duplicate entries** - Fixed reauth flow to update existing entry instead of creating new one
- **Authentication error handling** - Integration now automatically attempts token refresh before triggering reauth
  - Catches authentication errors during setup and periodic updates
  - Attempts automatic token refresh first
  - Only prompts for re-authorization if refresh fails
- **Webhook import naming conflict** - Renamed `webhook` import to `ha_webhook` to avoid collision
- **OptionsFlow config_entry property issue** - Removed custom `__init__` that was conflicting with base class
- **Missing auth_implementation handling** - Added defensive checks for missing OAuth implementation
- **Data structure access in platforms** - Fixed all platforms (sensor, lock, climate, device_tracker) to properly access client from dict structure
- **Service handler data access** - Fixed service handlers to get client from `hass.data` on each call instead of using closure
- **Periodic update timer cleanup** - Added proper cancellation of periodic update listener in `async_unload_entry` to prevent lingering timers
- **Periodic update data access** - Fixed periodic update function to get client from `hass.data` instead of closure
- Non-blocking async operations using asyncio.to_thread() for all Smartcar SDK calls
- **Enhanced documentation**
  - Comprehensive single README with all documentation consolidated
  - Complete entity reference with descriptions and requirements
  - Detailed webhook setup guide with troubleshooting
  - OAuth permissions reference with vehicle compatibility
  - Vehicle compatibility information and limitations
  - FAQ section addressing common questions
  - Automation examples for common use cases
  - Clear notes about feature availability by vehicle year/model
  - Troubleshooting guide for missing entities and common issues

### Changed

- **Python 3.14**: Minimum required Python version raised from 3.12 to 3.14.
  Updated `Pipfile`, `pyproject.toml` (Black `target-version`), `manifest.json`
  (`requirements_python`), and CI workflow (`pr-validation.yml`).
- **BREAKING**: Migrated to OAuth2 Application Credentials flow
  - Now uses Home Assistant's built-in OAuth2 framework
  - Requires adding application credentials before integration setup
  - Eliminates manual redirect URI configuration
  - Must use `https://my.home-assistant.io/redirect/oauth` as redirect URI
- Integration `iot_class` changed from `cloud_polling` to `cloud_push` (when webhooks configured)
- Improved OAuth state management and reliability
- Enhanced reauth support for expired credentials
- Fixed blocking I/O operations that could cause event loop warnings
- Documentation now clearly explains that entity availability depends on vehicle capabilities

### Migration Guide

**For Existing Users:**

1. Note your current Smartcar Client ID and Client Secret
2. Remove existing integration instance
3. Update Smartcar app redirect URI to: `https://my.home-assistant.io/redirect/oauth`
4. Add Application Credentials in Home Assistant (Settings > Application Credentials)
5. Re-add the integration using the new OAuth2 flow
6. (Optional) Configure webhooks by adding Management Token in integration options

## [0.0.1] - 2026-01-02

### Initial Release

- Initial release of the Nissan North America Home Assistant integration.
- Support for vehicle sensors, lock, climate, and device tracker.
- Service calls for lock/unlock, engine start/stop, find vehicle, and refresh status.
- HACS support and user-friendly configuration.
- Automated versioning and changelog workflows.
- Comprehensive test coverage and documentation.
