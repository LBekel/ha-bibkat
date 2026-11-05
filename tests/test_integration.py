"""Exercise real HA setup, entity lifecycle and calendar semantics."""

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from homeassistant.components.calendar import DATA_COMPONENT
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Context
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.bibkat.api import (
    BibkatAuthError,
    BibkatError,
    BibkatRateLimitError,
    BibkatRenewalError,
    Loan,
    RenewalResult,
)
from custom_components.bibkat.calendar import BibkatCalendar
from custom_components.bibkat.coordinator import BibkatCoordinator


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    return {
        key: registry.async_get_entity_id(platform, "bibkat", f"{entry.entry_id}_{key}")
        for platform, key in [
            ("sensor", "loans"),
            ("sensor", "loan_list"),
            ("sensor", "overdue"),
            ("sensor", "next_due"),
            ("calendar", "returns"),
        ]
    }


async def test_setup_shared_fetch_and_readonly_calendar(
    hass, entry, mock_client, freezer
):
    freezer.move_to("2026-11-15T12:00:00+00:00")
    entities = await setup(hass, entry)
    mock_client.async_get_loans.assert_awaited_once()
    assert hass.states.get(entities["loans"]).state == "3"
    assert hass.states.get(entities["loan_list"]).state == "3"
    assert hass.states.get(entities["overdue"]).state == "0"
    assert hass.states.get(entities["next_due"]).state == "2026-11-15"
    assert hass.states.get(entities["returns"]).state == "on"
    attrs = hass.states.get(entities["loans"]).attributes
    assert len(attrs["loans"]) == 3
    assert "synthetic-password" not in str(attrs)
    assert "000123" not in str(attrs)
    assert hass.states.get(entities["loan_list"]).attributes["loans"] == attrs["loans"]
    assert (
        hass.states.get(entities["loan_list"]).attributes["icon"]
        == "mdi:format-list-bulleted"
    )
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entities["returns"],
            "start_date_time": "2026-11-15T00:00:00+00:00",
            "end_date_time": "2026-11-16T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    assert len(result[entities["returns"]]["events"]) == 2
    assert hass.states.get(entities["returns"]).attributes["supported_features"] == 0
    mock_client.async_get_loans.assert_awaited_once()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entities["loans"]).state == STATE_UNAVAILABLE


async def test_midnight_recomputes_without_network_or_false_recovery(
    hass, entry, mock_client, freezer
):
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    freezer.move_to("2026-11-15T22:59:50+00:00")
    entities = await setup(hass, entry)
    assert hass.states.get(entities["overdue"]).state == "0"
    freezer.move_to("2026-11-15T23:00:01+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entities["overdue"]).state == "2"
    mock_client.async_get_loans.assert_awaited_once()
    # A failed update remains unavailable across subsequent local-day changes.
    mock_client.async_get_loans.side_effect = BibkatError("safe error")
    await entry.runtime_data.async_refresh()
    calendar = hass.data[DATA_COMPONENT].get_entity(entities["returns"])
    with pytest.raises(HomeAssistantError):
        await calendar.async_get_events(
            hass, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
        )
    for entity_id in entities.values():
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    calendar._async_day_changed(dt_util.now())
    assert hass.states.get(entities["returns"]).state == STATE_UNAVAILABLE


async def test_empty_account_and_manual_update(hass, entry, mock_client):
    assert await async_setup_component(hass, "homeassistant", {})
    mock_client.async_get_loans.return_value = ()
    entities = await setup(hass, entry)
    assert hass.states.get(entities["loans"]).state == "0"
    assert hass.states.get(entities["loan_list"]).state == "0"
    assert hass.states.get(entities["loan_list"]).attributes["loans"] == []
    assert hass.states.get(entities["overdue"]).state == "0"
    assert hass.states.get(entities["next_due"]).state == "unknown"
    assert hass.states.get(entities["returns"]).state == "off"
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": entities["loans"]},
        blocking=True,
    )
    assert mock_client.async_get_loans.await_count == 2


async def test_calendar_timezones_boundaries_and_dst(hass, entry, loans, freezer):
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    coordinator = BibkatCoordinator(hass, entry, AsyncMock())
    coordinator.async_set_updated_data(loans)
    calendar = BibkatCalendar(coordinator)
    events = await calendar.async_get_events(
        hass,
        datetime(2026, 11, 14, 23, tzinfo=UTC),
        datetime(2026, 11, 15, 0, tzinfo=UTC),
    )
    assert len(events) == 2
    assert events[0].start.isoformat() == "2026-11-15"
    assert events[0].end.isoformat() == "2026-11-16"
    assert (
        await calendar.async_get_events(
            hass,
            datetime(2026, 11, 15, 23, tzinfo=UTC),
            datetime(2026, 11, 16, 0, tzinfo=UTC),
        )
        == []
    )
    freezer.move_to("2026-11-15T22:59:59+00:00")
    assert calendar.event.summary == "Example One"
    freezer.move_to("2026-11-15T23:00:00+00:00")
    assert calendar.event.summary == "Example Three"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BibkatAuthError("safe"), ConfigEntryAuthFailed),
        (BibkatError("safe"), UpdateFailed),
        (BibkatRateLimitError(7200), UpdateFailed),
    ],
)
async def test_coordinator_errors(hass, entry, error, expected):
    client = AsyncMock()
    client.async_get_loans.side_effect = error
    coordinator = BibkatCoordinator(hass, entry, client)
    with pytest.raises(expected) as caught:
        await coordinator._async_update_data()
    if isinstance(error, BibkatRateLimitError):
        assert caught.value.retry_after == 7200


async def test_failed_initial_fetch_exposes_no_empty_sensors(hass, entry, mock_client):
    mock_client.async_get_loans.side_effect = BibkatError("safe")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert not [
        item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    ]


async def test_auth_failure_starts_reauthentication(hass, entry, mock_client):
    entities = await setup(hass, entry)
    mock_client.async_get_loans.side_effect = BibkatAuthError("safe")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entities["loans"]).state == STATE_UNAVAILABLE
    flows = hass.config_entries.flow.async_progress_by_handler("bibkat")
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_recovery_can_report_genuinely_empty_account(hass, entry, mock_client):
    entities = await setup(hass, entry)
    mock_client.async_get_loans.side_effect = BibkatError("safe")
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entities["loans"]).state == STATE_UNAVAILABLE
    mock_client.async_get_loans.side_effect = None
    mock_client.async_get_loans.return_value = ()
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entities["loans"]).state == "0"
    assert hass.states.get(entities["loan_list"]).state == "0"
    assert hass.states.get(entities["loan_list"]).attributes["loans"] == []
    assert hass.states.get(entities["returns"]).state == "off"


async def test_two_accounts_have_separate_entities_and_devices(
    hass, entry, mock_client
):
    first = await setup(hass, entry)
    second_entry = MockConfigEntry(
        domain="bibkat",
        title="Second account",
        unique_id="second-synthetic-id",
        data={**entry.data, "username": "000456", "device_id": "second-stable-device"},
    )
    second_entry.add_to_hass(hass)
    second = await setup(hass, second_entry)
    assert set(first.values()).isdisjoint(second.values())
    registry = er.async_get(hass)
    assert (
        registry.async_get(first["loans"]).device_id
        != registry.async_get(second["loans"]).device_id
    )
    assert mock_client.async_get_loans.await_count == 2
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get_entity_id("sensor", "bibkat", f"{entry.entry_id}_loans")
        == first["loans"]
    )


async def test_dst_all_day_event_uses_local_midnights(hass, entry):
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    coordinator = BibkatCoordinator(hass, entry, AsyncMock())
    coordinator.async_set_updated_data(
        (Loan("100", "Example Book", "", date(2026, 10, 25), None),)
    )
    calendar = BibkatCalendar(coordinator)
    assert (
        await calendar.async_get_events(
            hass,
            datetime(2026, 10, 24, 21, tzinfo=UTC),
            datetime(2026, 10, 24, 22, tzinfo=UTC),
        )
        == []
    )
    assert (
        len(
            await calendar.async_get_events(
                hass,
                datetime(2026, 10, 25, 22, tzinfo=UTC),
                datetime(2026, 10, 25, 23, tzinfo=UTC),
            )
        )
        == 1
    )
    assert (
        await calendar.async_get_events(
            hass,
            datetime(2026, 10, 25, 23, tzinfo=UTC),
            datetime(2026, 10, 26, 0, tzinfo=UTC),
        )
        == []
    )


async def test_loan_list_preserves_long_titles_and_every_due_date(
    hass, entry, mock_client
):
    title = "Long example title " * 30
    loans = tuple(
        Loan(str(index), f"{title}{index}", "Example author", date(2026, 11, 15), None)
        for index in range(40)
    )
    mock_client.async_get_loans.return_value = loans
    entities = await setup(hass, entry)
    state = hass.states.get(entities["loan_list"])
    assert state.state == "40"
    assert len(state.state) <= 255
    assert state.attributes["loans"] == [
        {
            "medium_id": loan.medium_id,
            "title": loan.title,
            "author": loan.author,
            "due_date": "2026-11-15",
            "is_renewable": False,
            "is_pending": False,
        }
        for loan in loans
    ]
    assert len(state.attributes["loans"][0]["title"]) > 255
    assert (
        state.attributes["loans"]
        == hass.states.get(entities["loans"]).attributes["loans"]
    )
    mock_client.async_get_loans.assert_awaited_once()


async def test_documented_markdown_card_shows_titles_dates_and_unavailable(
    hass, entry, mock_client
):
    entities = await setup(hass, entry)
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    card = yaml.safe_load(readme.split("```yaml\n", 1)[1].split("```", 1)[0])
    template = Template(
        card["content"].replace(
            "sensor.your_account_loan_list", entities["loan_list"]
        ),
        hass,
    )
    rendered = template.async_render()
    assert "Example One" in rendered
    assert "Example Two" in rendered
    assert "Example Three" in rendered
    assert rendered.count("2026-11-15") == 2
    assert "2026-11-29" in rendered
    mock_client.async_get_loans.return_value = ()
    await entry.runtime_data.async_refresh()
    assert "No items currently borrowed." in template.async_render()
    mock_client.async_get_loans.side_effect = BibkatError("safe")
    await entry.runtime_data.async_refresh()
    rendered = template.async_render()
    assert "Loans are currently unavailable." in rendered
    assert "No items currently borrowed." not in rendered


async def test_renew_action_returns_new_date_and_updates_all_entities(
    hass, entry, mock_client
):
    entities = await setup(hass, entry)
    updated = Loan("100", "Example One", "Author One", date(2026, 12, 1), None)
    mock_client.async_renew = AsyncMock(
        return_value=RenewalResult(updated, date(2026, 11, 15), (updated,))
    )
    result = await hass.services.async_call(
        "bibkat",
        "renew",
        {
            "config_entry_id": entry.entry_id,
            "medium_id": "100",
            "expected_due_date": "2026-11-15",
        },
        blocking=True,
        return_response=True,
    )
    assert result == {
        "medium_id": "100",
        "title": "Example One",
        "previous_due_date": "2026-11-15",
        "due_date": "2026-12-01",
    }
    mock_client.async_renew.assert_awaited_once_with("100", date(2026, 11, 15))
    assert hass.states.get(entities["next_due"]).state == "2026-12-01"
    assert (
        hass.states.get(entities["loan_list"]).attributes["loans"][0]["due_date"]
        == "2026-12-01"
    )
    calendar = hass.data[DATA_COMPONENT].get_entity(entities["returns"])
    assert calendar._events()[0].start == date(2026, 12, 1)


@pytest.mark.parametrize("with_data", [True, False])
async def test_renew_action_surfaces_uncertainty_without_empty_success(
    hass, entry, mock_client, loans, with_data
):
    entities = await setup(hass, entry)
    mock_client.async_renew = AsyncMock(
        side_effect=BibkatRenewalError(
            "renewal_unconfirmed", loans if with_data else None
        )
    )
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(
            "bibkat",
            "renew",
            {
                "config_entry_id": entry.entry_id,
                "medium_id": "100",
                "expected_due_date": "2026-11-15",
            },
            blocking=True,
            return_response=True,
        )
    assert caught.value.translation_key == "renewal_unconfirmed"
    assert hass.states.get(entities["loans"]).state == (
        "3" if with_data else STATE_UNAVAILABLE
    )
    mock_client.async_renew.assert_awaited_once()


async def test_renew_requires_loaded_matching_account(hass, entry, mock_client):
    await setup(hass, entry)
    for entry_id in ("missing-account", entry.entry_id):
        if entry_id == entry.entry_id:
            await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(HomeAssistantError) as caught:
            await hass.services.async_call(
                "bibkat",
                "renew",
                {
                    "config_entry_id": entry_id,
                    "medium_id": "100",
                    "expected_due_date": "2026-11-15",
                },
                blocking=True,
            )
        assert caught.value.translation_key == "account_not_loaded"
    mock_client.async_renew.assert_not_called()


async def test_renew_checks_admin_permission(
    hass, entry, mock_client, hass_read_only_user
):
    await setup(hass, entry)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "bibkat",
            "renew",
            {
                "config_entry_id": entry.entry_id,
                "medium_id": "100",
                "expected_due_date": "2026-11-15",
            },
            context=Context(user_id=hass_read_only_user.id),
            blocking=True,
        )
    mock_client.async_renew.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        {"medium_id": "100"},
        {"medium_id": "all", "expected_due_date": "2026-11-15"},
        {"medium_id": ["100", "101"], "expected_due_date": "2026-11-15"},
        {"medium_id": "100", "expected_due_date": "not-a-date"},
    ],
)
async def test_renew_rejects_missing_or_invalid_fields(hass, entry, mock_client, data):
    import probatio

    await setup(hass, entry)
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            "bibkat",
            "renew",
            {"config_entry_id": entry.entry_id, **data},
            blocking=True,
        )
    mock_client.async_renew.assert_not_called()


async def test_renew_schema_and_translated_action_description(hass, entry, mock_client):
    from homeassistant.helpers.service import async_get_all_descriptions

    await setup(hass, entry)
    descriptions = await async_get_all_descriptions(hass)
    assert set(descriptions["bibkat"]["renew"]["fields"]) == {
        "config_entry_id",
        "medium_id",
        "expected_due_date",
    }
    assert descriptions["bibkat"]["renew"]["response"]["optional"]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (BibkatAuthError("safe"), "invalid_auth"),
        (BibkatRateLimitError(3600), "rate_limited"),
        (BibkatError("safe"), "cannot_update"),
    ],
)
async def test_preflight_failure_surfaces_as_service_error(
    hass, entry, mock_client, error, key
):
    await setup(hass, entry)
    mock_client.async_renew.side_effect = error
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(
            "bibkat",
            "renew",
            {
                "config_entry_id": entry.entry_id,
                "medium_id": "100",
                "expected_due_date": "2026-11-15",
            },
            blocking=True,
        )
    assert caught.value.translation_key == key
    mock_client.async_renew.assert_awaited_once()
    await hass.async_block_till_done()
    if key == "invalid_auth":
        assert any(
            flow["context"]["source"] == "reauth"
            for flow in hass.config_entries.flow.async_progress_by_handler("bibkat")
        )
