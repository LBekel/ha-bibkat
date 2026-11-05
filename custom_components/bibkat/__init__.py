"""Set up Bibkat accounts and the explicit renewal action."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import BibkatClient
from .const import CONF_CATALOG_ID, CONF_DEVICE_ID
from .coordinator import BibkatCoordinator
from .services import async_setup_services

type BibkatConfigEntry = ConfigEntry[BibkatCoordinator]

PLATFORMS = (Platform.SENSOR, Platform.CALENDAR)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BibkatConfigEntry) -> bool:
    """Load an account and fetch data before exposing entities."""
    client = BibkatClient(
        async_get_clientsession(hass),
        entry.data[CONF_CATALOG_ID],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_DEVICE_ID],
    )
    coordinator = BibkatCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BibkatConfigEntry) -> bool:
    """Unload entities, coordinator listeners and scheduled updates."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
