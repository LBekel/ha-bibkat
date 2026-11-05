"""Explicit single-loan renewal action; never called by polling."""

import probatio as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .api import BibkatAuthError, BibkatError, BibkatRateLimitError, BibkatRenewalError
from .const import DOMAIN
from .coordinator import BibkatCoordinator

SERVICE_RENEW = "renew"
RENEW_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("medium_id"): vol.All(str, vol.Match(r"^[1-9][0-9]{0,15}$")),
        vol.Required("expected_due_date"): cv.date,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register once, independently of individual account load/unload."""

    async def async_renew(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
            or not isinstance(entry.runtime_data, BibkatCoordinator)
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="account_not_loaded"
            )
        try:
            result = await entry.runtime_data.async_renew(
                call.data["medium_id"], call.data["expected_due_date"]
            )
        except BibkatRenewalError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=err.code
            ) from None
        except BibkatAuthError:
            entry.async_start_reauth(hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from None
        except BibkatRateLimitError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="rate_limited"
            ) from None
        except BibkatError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_update"
            ) from None
        return {
            "medium_id": result.loan.medium_id,
            "title": result.loan.title,
            "previous_due_date": result.previous_due_date.isoformat(),
            "due_date": result.loan.due_date.isoformat(),
        }

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RENEW,
        async_renew,
        schema=RENEW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
