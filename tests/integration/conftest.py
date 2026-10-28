"""Fixtures for the Home Assistant integration tests (real hass, respx).

pytest-homeassistant-custom-component registers itself as a pytest plugin,
so it is not listed in pytest_plugins here (not allowed below the top-level
conftest).
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield
