"""Stable account sensors for current loans and their due dates."""

from datetime import date
from typing import Any, Literal

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
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
    async_add_entities(
        [
            BibkatLoansSensor(entry.runtime_data),
            BibkatLoansSensor(entry.runtime_data, "loan_list"),
            BibkatOverdueCount(entry.runtime_data),
            BibkatNextDue(entry.runtime_data),
        ]
    )


class BibkatLoansSensor(BibkatEntity, SensorEntity):
    """Keep full loan lists in attributes rather than a length-limited state."""

    _attr_icon = "mdi:bookshelf"
    _unrecorded_attributes = frozenset({"loans"})

    def __init__(
        self,
        coordinator: BibkatCoordinator,
        key: Literal["loans", "loan_list"] = "loans",
    ) -> None:
        super().__init__(coordinator, key)
        if key == "loan_list":
            self._attr_icon = "mdi:format-list-bulleted"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "loans": [
                {
                    "medium_id": loan.medium_id,
                    "title": loan.title,
                    "author": loan.author,
                    "due_date": loan.due_date.isoformat(),
                    "is_renewable": loan.is_renewable,
                    "is_pending": loan.is_pending,
                }
                for loan in self.coordinator.data
            ]
        }


class BibkatOverdueCount(BibkatEntity, SensorEntity):
    """A due date is inclusive in the Home Assistant local timezone."""

    _attr_icon = "mdi:book-alert"

    def __init__(self, coordinator: BibkatCoordinator) -> None:
        super().__init__(coordinator, "overdue")

    @property
    def native_value(self) -> int:
        today = dt_util.now().date()
        return sum(loan.due_date < today for loan in self.coordinator.data)


class BibkatNextDue(BibkatEntity, SensorEntity):
    """Earliest outstanding due date, including overdue loans."""

    _attr_device_class = SensorDeviceClass.DATE

    def __init__(self, coordinator: BibkatCoordinator) -> None:
        super().__init__(coordinator, "next_due")

    @property
    def native_value(self) -> date | None:
        return min((loan.due_date for loan in self.coordinator.data), default=None)
