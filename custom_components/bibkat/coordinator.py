"""One shared reader request per Bibkat account."""

import asyncio
import logging
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BibkatAuthError,
    BibkatClient,
    BibkatError,
    BibkatRateLimitError,
    BibkatRenewalError,
    Loan,
    RenewalResult,
)
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class BibkatCoordinator(DataUpdateCoordinator[tuple[Loan, ...]]):
    """Fetch account loans while preserving HA error and retry semantics."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry[Any], client: BibkatClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self._operation_lock = asyncio.Lock()

    async def _async_update_data(self) -> tuple[Loan, ...]:
        async with self._operation_lock:
            return await self._fetch_loans()

    async def async_renew(
        self, medium_id: str, expected_due_date: date
    ) -> RenewalResult:
        """Serialize state writes against polling, including error recovery."""
        async with self._operation_lock:
            try:
                result = await self.client.async_renew(medium_id, expected_due_date)
            except BibkatRenewalError as err:
                if err.loans is not None:
                    self.async_set_updated_data(err.loans)
                else:
                    self.async_set_update_error(err)
                raise
            except BibkatError as err:
                self.async_set_update_error(err)
                raise
            self.async_set_updated_data(result.loans)
            return result

    async def _fetch_loans(self) -> tuple[Loan, ...]:
        try:
            return await self.client.async_get_loans()
        except BibkatAuthError:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from None
        except BibkatRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=err.retry_after,
            ) from None
        except BibkatError:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="cannot_update"
            ) from None
