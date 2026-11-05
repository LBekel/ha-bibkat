"""Read-only, all-day events for each outstanding loan."""

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import BibkatConfigEntry
from .coordinator import BibkatCoordinator
from .entity import BibkatEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BibkatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([BibkatCalendar(entry.runtime_data)])


class BibkatCalendar(BibkatEntity, CalendarEntity):
    """Display only existing loans; never modify loans or reservations."""

    _attr_supported_features = 0

    def __init__(self, coordinator: BibkatCoordinator) -> None:
        super().__init__(coordinator, "returns")

    def _events(self) -> list[CalendarEvent]:
        return [
            CalendarEvent(
                summary=loan.title,
                start=loan.due_date,
                end=loan.due_date + timedelta(days=1),
                description=loan.author or None,
            )
            for loan in self.coordinator.data
        ]

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        return next((event for event in self._events() if event.end > today), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if not self.available:
            raise HomeAssistantError("Bibkat calendar is unavailable")
        # Compare local midnights to instants, not truncated query dates.
        return [
            event
            for event in self._events()
            if dt_util.start_of_local_day(event.end) > start_date
            and dt_util.start_of_local_day(event.start) < end_date
        ]
