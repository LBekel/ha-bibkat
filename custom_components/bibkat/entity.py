"""Common account identity and local-day updates."""

from datetime import datetime

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CATALOG_NAME, DOMAIN
from .coordinator import BibkatCoordinator


class BibkatEntity(CoordinatorEntity[BibkatCoordinator]):
    """Account-scoped entity with stable IDs and no username attributes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BibkatCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        assert entry is not None
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Bibkat",
            model=entry.data[CONF_CATALOG_NAME],
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._async_day_changed,
                hour=0,
                minute=0,
                second=0,
            )
        )

    @callback
    def _async_day_changed(self, now: datetime) -> None:
        # Recompute date-based states without polling or resetting a failed update.
        self.async_write_ha_state()
