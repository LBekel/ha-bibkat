"""UI flows with synthetic accounts and no external authentication."""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bibkat.api import (
    BibkatAuthError,
    BibkatConnectionError,
    BibkatError,
    BibkatRateLimitError,
    Catalog,
)

CATALOG = Catalog("BGX610102", "Example library", "Example city")
ACCOUNT = {
    "name": "Example family account",
    "username": "000123",
    "password": "synthetic-password",
}


@pytest.fixture
def flow_client():
    with patch(
        "custom_components.bibkat.config_flow.BibkatClient", autospec=True
    ) as factory:
        factory.search_catalogs = AsyncMock(return_value=(CATALOG,))
        factory.return_value.async_get_loans = AsyncMock(return_value=())
        yield factory


async def account_step(hass):
    result = await hass.config_entries.flow.async_init(
        "bibkat", context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"search": "Baccum"}
    )
    assert result["step_id"] == "catalog"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"catalog_id": "BGX610102"}
    )
    assert result["step_id"] == "account"
    return result


async def test_full_setup_retains_leading_zeros_and_stable_device_id(hass, flow_client):
    result = await account_step(hass)
    with patch("custom_components.bibkat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT["name"]
    assert result["data"]["username"] == "000123"
    assert result["data"]["device_id"]
    assert result["data"]["catalog_id"] == CATALOG.catalog_id
    assert (
        result["result"].unique_id
        == hashlib.sha256(("BGX610102" + "\0" + "000123").encode()).hexdigest()
    )
    assert "token" not in str(result["data"])
    flow_client.return_value.async_get_loans.assert_awaited_once()
    assert flow_client.call_args.args[-1] == result["data"]["device_id"]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (BibkatAuthError("safe"), "invalid_auth"),
        (BibkatConnectionError("safe"), "cannot_connect"),
        (BibkatRateLimitError(3600), "rate_limited"),
        (BibkatError("safe"), "invalid_response"),
    ],
)
async def test_invalid_account_can_be_corrected_without_new_device(
    hass, flow_client, error, key
):
    flow_client.return_value.async_get_loans.side_effect = error
    result = await account_step(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ACCOUNT)
    assert result["errors"] == {"base": key}
    device_id = flow_client.call_args.args[-1]
    assert "synthetic-password" not in repr(result["data_schema"])
    flow_client.return_value.async_get_loans.side_effect = None
    with patch("custom_components.bibkat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["device_id"] == device_id


async def test_duplicate_account_aborts_before_login(hass, entry, flow_client):
    hass.config_entries.async_update_entry(
        entry,
        unique_id=hashlib.sha256(("BGX610102" + "\0" + "000123").encode()).hexdigest(),
    )
    result = await account_step(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ACCOUNT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    flow_client.return_value.async_get_loans.assert_not_awaited()


async def test_no_libraries_and_connection_error(hass, flow_client):
    flow_client.search_catalogs.return_value = ()
    result = await hass.config_entries.flow.async_init(
        "bibkat", context={"source": SOURCE_USER}, data={"search": "Unknown"}
    )
    assert result["errors"] == {"base": "no_catalogs"}
    flow_client.search_catalogs.side_effect = BibkatConnectionError("safe")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"search": "Example"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauthentication_changes_only_password(hass, entry, flow_client):
    old_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        "bibkat",
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-synthetic-password"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data == {**old_data, "password": "new-synthetic-password"}
    reload.assert_awaited_once_with(entry.entry_id)


async def test_rejected_reauthentication_does_not_overwrite_credentials(
    hass, entry, flow_client
):
    flow_client.return_value.async_get_loans.side_effect = BibkatAuthError("safe")
    old_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        "bibkat",
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "invalid-synthetic-password"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == old_data
