"""Only synthetic fixtures; never load the developer's .env."""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bibkat.api import Loan


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow the local integration in the isolated HA test instance."""
    yield


@pytest.fixture
def loans():
    return (
        Loan(
            "100", "Example One", "Author One", date(2026, 11, 15), date(2026, 10, 18)
        ),
        Loan("101", "Example Two", "", date(2026, 11, 15), None),
        Loan("102", "Example Three", "Author Three", date(2026, 11, 29), None),
    )


@pytest.fixture
def entry(hass):
    result = MockConfigEntry(
        domain="bibkat",
        title="Example account",
        unique_id="synthetic-account-id",
        data={
            "catalog_id": "BGX610102",
            "catalog_name": "Example library",
            "username": "000123",
            "password": "synthetic-password",
            "device_id": "synthetic-stable-device",
        },
    )
    result.add_to_hass(hass)
    return result


@pytest.fixture
def mock_client(loans):
    with patch("custom_components.bibkat.BibkatClient", autospec=True) as factory:
        client = factory.return_value
        client.async_get_loans = AsyncMock(return_value=loans)
        yield client
