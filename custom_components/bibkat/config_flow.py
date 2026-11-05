"""UI setup and password-only reauthentication for Bibkat."""

import hashlib
from typing import Any
from uuid import uuid4

import probatio as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BibkatAuthError,
    BibkatClient,
    BibkatConnectionError,
    BibkatError,
    BibkatRateLimitError,
    Catalog,
)
from .const import CONF_CATALOG_ID, CONF_CATALOG_NAME, CONF_DEVICE_ID, DOMAIN

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


class BibkatConfigFlow(ConfigFlow, domain=DOMAIN):
    """One configuration entry per catalog and reader account."""

    VERSION = 1

    def __init__(self) -> None:
        self._catalogs: dict[str, Catalog] = {}
        self._catalog: Catalog | None = None
        self._device_id = str(uuid4())

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                catalogs = await BibkatClient.search_catalogs(
                    async_get_clientsession(self.hass), user_input["search"]
                )
            except BibkatRateLimitError:
                errors["base"] = "rate_limited"
            except BibkatConnectionError:
                errors["base"] = "cannot_connect"
            except BibkatError:
                errors["base"] = "invalid_response"
            else:
                self._catalogs = {catalog.catalog_id: catalog for catalog in catalogs}
                if self._catalogs:
                    return await self.async_step_catalog()
                errors["base"] = "no_catalogs"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("search", default="Baccum"): str}),
            errors=errors,
        )

    async def async_step_catalog(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self._catalog = self._catalogs.get(user_input[CONF_CATALOG_ID])
            if self._catalog is not None:
                return await self.async_step_account()
            errors["base"] = "invalid_catalog"
        return self.async_show_form(
            step_id="catalog",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CATALOG_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=catalog.catalog_id,
                                    label=f"{catalog.name} ({catalog.city}) [{catalog.catalog_id}]",
                                )
                                for catalog in self._catalogs.values()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def _validate_account(self, data: dict[str, Any]) -> str | None:
        try:
            client = BibkatClient(
                async_get_clientsession(self.hass),
                data[CONF_CATALOG_ID],
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
                data[CONF_DEVICE_ID],
            )
            await client.async_get_loans()
        except BibkatAuthError:
            return "invalid_auth"
        except BibkatRateLimitError:
            return "rate_limited"
        except BibkatConnectionError:
            return "cannot_connect"
        except BibkatError:
            return "invalid_response"
        return None

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._catalog is not None
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            identity = f"{self._catalog.catalog_id}\0{username}".encode()
            await self.async_set_unique_id(hashlib.sha256(identity).hexdigest())
            self._abort_if_unique_id_configured()
            data = {
                CONF_CATALOG_ID: self._catalog.catalog_id,
                CONF_CATALOG_NAME: self._catalog.name,
                CONF_USERNAME: username,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_DEVICE_ID: self._device_id,
            }
            if error := await self._validate_account(data):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or self._catalog.name, data=data
                )
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=self._catalog.name): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if error := await self._validate_account(data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            errors=errors,
        )
