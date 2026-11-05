"""Small asynchronous client for the observed Bibkat v5 API."""

import asyncio
import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

API_BASE_URL = "https://eopac.net/api/bibkat/v5"
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class BibkatError(Exception):
    """A safe-to-display API error, never containing server response content."""


class BibkatAuthError(BibkatError):
    """The credentials or session were rejected."""


class BibkatConnectionError(BibkatError):
    """The API could not be reached."""


class BibkatRateLimitError(BibkatError):
    """The API requested a pause."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Bibkat rate limit reached")
        self.retry_after = retry_after


class BibkatRenewalError(BibkatError):
    """A renewal was refused locally or could not be confirmed."""

    def __init__(self, code: str, loans: tuple["Loan", ...] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.loans = loans


@dataclass(frozen=True)
class Catalog:
    """Public library metadata."""

    catalog_id: str
    name: str
    city: str


@dataclass(frozen=True)
class Loan:
    """Only the loan fields required by Home Assistant."""

    medium_id: str
    title: str
    author: str
    due_date: date
    start_date: date | None
    is_renewable: bool = False
    is_pending: bool = False


@dataclass(frozen=True)
class RenewalResult:
    """Only report success after receipt and fresh reader data agree."""

    loan: Loan
    previous_due_date: date
    loans: tuple[Loan, ...]


@dataclass(frozen=True)
class Tokens:
    """Ephemeral credentials, excluded from representations."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float | None = field(default=None, repr=False)


def _token_expiration(token: str) -> float | None:
    """Read JWT expiry only as a refresh hint, never as proof of authorization."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (ValueError, UnicodeError, binascii.Error):
        raise BibkatError("Invalid Bibkat token metadata") from None
    if not isinstance(claims, dict):
        raise BibkatError("Invalid Bibkat token metadata")
    expiry = claims.get("exp")
    if expiry is None:
        return None
    if type(expiry) not in (int, float) or not 0 < expiry <= 253402300799:
        raise BibkatError("Invalid Bibkat token expiry")
    return float(expiry)


def _text(value: Any, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise BibkatError("Invalid text field in Bibkat response")
    return " ".join(value.split())


def _date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BibkatError("Invalid loan date in Bibkat response")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise BibkatError("Invalid loan date in Bibkat response") from None
    if result == date.max:
        raise BibkatError("Unsupported loan date in Bibkat response")
    return result


def _flag(value: Any) -> bool:
    if value is None:
        return False
    if type(value) is not bool:
        raise BibkatError("Invalid lending flag in Bibkat response")
    return value


def _retry_after(value: str | None) -> float:
    if value:
        try:
            return max(60, int(value))
        except ValueError:
            try:
                until = parsedate_to_datetime(value)
                if until.tzinfo is not None:
                    return max(60, (until - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 6 * 60 * 60


def parse_loans(data: dict[str, Any]) -> tuple[Loan, ...]:
    """Reject incomplete responses rather than reporting zero loans."""
    reader = data.get("reader")
    if not isinstance(reader, dict) or not isinstance(reader.get("lendings"), list):
        raise BibkatError("Missing loan list in Bibkat response")
    loans = []
    for lending in reader["lendings"]:
        if not isinstance(lending, dict) or not isinstance(lending.get("medium"), dict):
            raise BibkatError("Invalid loan in Bibkat response")
        medium = lending["medium"]
        medium_id = medium.get("id")
        if type(medium_id) not in (int, str) or not str(medium_id).strip():
            raise BibkatError("Missing medium identifier in Bibkat response")
        loans.append(
            Loan(
                medium_id=str(medium_id),
                title=_text(medium.get("title")),
                author=_text(medium.get("author"), optional=True),
                due_date=_date(lending.get("end")),
                start_date=_date(lending["start"])
                if lending.get("start") is not None
                else None,
                is_renewable=_flag(lending.get("is_renewable")),
                is_pending=_flag(lending.get("is_pending")),
            )
        )
    return tuple(
        sorted(loans, key=lambda loan: (loan.due_date, loan.title, loan.medium_id))
    )


class BibkatClient:
    """Client with read-only polling and explicitly requested single renewals."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        catalog_id: str,
        username: str,
        password: str,
        device_id: str,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", catalog_id):
            raise BibkatError("Invalid catalog identifier")
        if not username.strip() or not password or not device_id.strip():
            raise BibkatAuthError("Missing Bibkat credentials")
        self._session = session
        self._catalog_id = catalog_id
        self._username = username
        self._password = password
        self._device_id = device_id
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()
        self._retry_not_before = 0.0

    @staticmethod
    async def _request(
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | list[int] | None = None,
        token: str | None = None,
        params: dict[str, str] | None = None,
        authenticating: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-Bibkat/0.1",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with session.request(
                method,
                f"{API_BASE_URL}{path}",
                json=payload,
                params=params,
                headers=headers,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise BibkatAuthError("Bibkat authorization rejected")
                if response.status == 429:
                    raise BibkatRateLimitError(
                        _retry_after(response.headers.get("Retry-After"))
                    )
                if response.status != 200:
                    raise BibkatError(f"Bibkat returned HTTP {response.status}")
                if response.content_type != "application/json":
                    raise BibkatError("Bibkat did not return JSON")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise BibkatError("Bibkat response exceeded size limit")
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise BibkatConnectionError("Cannot connect to Bibkat") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BibkatError("Invalid JSON from Bibkat") from None
        if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
            raise BibkatError("Missing metadata in Bibkat response")
        if data["meta"].get("success") is not True:
            if authenticating:
                raise BibkatAuthError("Bibkat authentication rejected")
            raise BibkatError("Bibkat reported an unsuccessful request")
        return data

    @classmethod
    async def search_catalogs(
        cls, session: aiohttp.ClientSession, query: str
    ) -> tuple[Catalog, ...]:
        """Search public libraries without attaching account credentials."""
        if not query.strip():
            raise BibkatError("Empty library search")
        data = await cls._request(
            session, "GET", "/catalogs/", params={"search": query.strip()}
        )
        if not isinstance(data.get("catalogs"), list):
            raise BibkatError("Missing catalogs in Bibkat response")
        catalogs = []
        for item in data["catalogs"]:
            if not isinstance(item, dict):
                raise BibkatError("Invalid catalog in Bibkat response")
            catalog_id = _text(item.get("bibid"))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", catalog_id):
                raise BibkatError("Invalid catalog identifier in Bibkat response")
            catalogs.append(
                Catalog(
                    catalog_id,
                    _text(item.get("name")),
                    _text(item.get("city"), optional=True),
                )
            )
        return tuple(catalogs)

    async def _authenticate(self, *, refresh: bool = False) -> None:
        payload = {
            "action": "auth",
            "grant_type": "refresh_token" if refresh else "password",
            "username": self._username,
            "device_name": "HomeAssistant",
            "device_id": self._device_id,
            "device_platform": "linux",
        }
        if refresh:
            if self._tokens is None:
                raise BibkatAuthError("No Bibkat session to refresh")
            payload["refresh_token"] = self._tokens.refresh_token
        else:
            payload["password"] = self._password
        data = await self._account_request(
            "POST",
            f"/catalogs/{self._catalog_id}/auth/",
            payload=payload,
            authenticating=True,
        )
        auth = data.get("authentication")
        if not isinstance(auth, dict):
            raise BibkatError("Missing authentication in Bibkat response")
        for key in ("access_token", "refresh_token"):
            token = auth.get(key)
            if (
                not isinstance(token, str)
                or not token
                or not all(33 <= ord(c) <= 126 for c in token)
            ):
                raise BibkatError("Invalid Bibkat authentication token")
        self._tokens = Tokens(
            auth["access_token"],
            auth["refresh_token"],
            _token_expiration(auth["access_token"]),
        )

    async def _account_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | list[int] | None = None,
        token: str | None = None,
        authenticating: bool = False,
    ) -> dict[str, Any]:
        remaining = self._retry_not_before - time.monotonic()
        if remaining > 0:
            raise BibkatRateLimitError(remaining)
        try:
            return await self._request(
                self._session,
                method,
                path,
                payload=payload,
                token=token,
                authenticating=authenticating,
            )
        except BibkatRateLimitError as err:
            self._retry_not_before = time.monotonic() + err.retry_after
            raise

    async def async_get_loans(self) -> tuple[Loan, ...]:
        """Reuse a session, with at most one refresh and one reader retry."""
        async with self._lock:
            return await self._get_loans_locked()

    async def _get_loans_locked(self) -> tuple[Loan, ...]:
        had_session = self._tokens is not None
        refreshed = False
        if not had_session:
            await self._authenticate()
        elif (
            self._tokens is not None
            and self._tokens.expires_at is not None
            and self._tokens.expires_at <= time.time() + 60
        ):
            await self._authenticate(refresh=True)
            refreshed = True
        try:
            return await self._read_loans()
        except BibkatAuthError:
            if not had_session or refreshed:
                raise
        # Do not re-submit the password automatically after a rejected refresh.
        await self._authenticate(refresh=True)
        return await self._read_loans()

    async def async_renew(
        self, medium_id: str, expected_due_date: date
    ) -> RenewalResult:
        """Check the current loan, submit once, and verify the resulting date.

        The app posts a bare JSON list of integer medium IDs, not an object.
        Never replay this non-idempotent POST after any transport/auth error.
        """
        if (
            not isinstance(medium_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", medium_id)
            or type(expected_due_date) is not date
        ):
            raise BibkatRenewalError("invalid_renewal_input")
        async with self._lock:
            before = await self._get_loans_locked()
            matches = [loan for loan in before if loan.medium_id == medium_id]
            if len(matches) != 1:
                raise BibkatRenewalError("loan_not_found", before)
            previous = matches[0]
            if previous.due_date != expected_due_date:
                raise BibkatRenewalError("due_date_changed", before)
            if not previous.is_renewable or previous.is_pending:
                raise BibkatRenewalError("not_renewable", before)
            assert self._tokens is not None
            try:
                data = await self._account_request(
                    "POST",
                    f"/catalogs/{self._catalog_id}/action/renew/",
                    payload=[int(medium_id)],
                    token=self._tokens.access_token,
                )
            except BibkatError:
                # The server may have applied the write even if its reply was lost.
                raise BibkatRenewalError("renewal_unconfirmed") from None

            try:
                current = await self._get_loans_locked()
            except BibkatError:
                raise BibkatRenewalError("renewal_unconfirmed") from None
            receipt = data.get("receipt")
            if not isinstance(receipt, dict):
                raise BibkatRenewalError("renewal_unconfirmed", current)
            errors = receipt.get("errors")
            if errors is not None and (
                not isinstance(errors, list)
                or not all(isinstance(error, str) for error in errors)
                or errors
            ):
                raise BibkatRenewalError("renewal_unconfirmed", current)
            try:
                received = parse_loans(
                    {"reader": {"lendings": receipt.get("lendings")}}
                )
            except BibkatError:
                raise BibkatRenewalError("renewal_unconfirmed", current) from None
            updated = [loan for loan in current if loan.medium_id == medium_id]
            confirmed = [loan for loan in received if loan.medium_id == medium_id]
            if (
                len(updated) != 1
                or len(confirmed) != 1
                or updated[0].due_date <= previous.due_date
                or updated[0].due_date != confirmed[0].due_date
            ):
                raise BibkatRenewalError("renewal_unconfirmed", current)
            return RenewalResult(updated[0], previous.due_date, current)

    async def _read_loans(self) -> tuple[Loan, ...]:
        if self._tokens is None:
            raise BibkatAuthError("No Bibkat session")
        data = await self._account_request(
            "GET",
            f"/catalogs/{self._catalog_id}/reader/",
            token=self._tokens.access_token,
        )
        return parse_loans(data)
