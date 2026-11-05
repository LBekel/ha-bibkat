"""Exercise wire behavior with fake responses, not real accounts."""

import asyncio
import base64
import json
from copy import deepcopy
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.bibkat.api import (
    API_BASE_URL,
    MAX_RESPONSE_BYTES,
    BibkatAuthError,
    BibkatClient,
    BibkatConnectionError,
    BibkatError,
    BibkatRateLimitError,
    BibkatRenewalError,
    Tokens,
    _retry_after,
    _token_expiration,
    parse_loans,
)

AUTH = {
    "meta": {"success": True},
    "authentication": {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
    },
}
READER = {
    "meta": {"success": True},
    "reader": {
        "email": "private-reader-field",
        "lendings": [
            {
                "start": "2026-10-18",
                "end": "2026-11-15",
                "medium": {
                    "id": 100,
                    "title": "Example Book",
                    "author": "Example Author",
                },
            }
        ],
    },
}
EMPTY = {"meta": {"success": True}, "reader": {"lendings": []}}


def response(
    body=None, *, status=200, content_type="application/json", headers=None, raw=None
):
    result = MagicMock()
    result.status = status
    result.content_type = content_type
    result.headers = headers or {}
    wire = raw if raw is not None else json.dumps(body).encode()

    async def chunks(_size):
        for offset in range(0, len(wire), 7):
            yield wire[offset : offset + 7]

    result.content.iter_chunked.side_effect = chunks
    context = MagicMock()
    context.__aenter__.return_value = result
    return context


def client_with(*responses):
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.side_effect = responses
    client = BibkatClient(
        session, "BGX610102", "000123", "synthetic-password", "stable-device"
    )
    return client, session


async def test_login_reader_and_token_reuse():
    client, session = client_with(response(AUTH), response(READER), response(EMPTY))
    loans = await client.async_get_loans()
    assert loans[0].due_date == date(2026, 11, 15)
    assert "private-reader-field" not in repr(loans)
    assert await client.async_get_loans() == ()
    assert session.request.call_count == 3
    login, reader, again = session.request.call_args_list
    assert login.args == ("POST", f"{API_BASE_URL}/catalogs/BGX610102/auth/")
    assert login.kwargs["json"]["device_id"] == "stable-device"
    assert login.kwargs["json"]["grant_type"] == "password"
    assert reader.args == ("GET", f"{API_BASE_URL}/catalogs/BGX610102/reader/")
    assert reader.kwargs["headers"]["Authorization"] == "Bearer synthetic-access"
    assert again.kwargs["headers"]["Authorization"] == "Bearer synthetic-access"
    for request in (login, reader, again):
        assert request.kwargs["allow_redirects"] is False
        assert request.kwargs["timeout"].total == 20
    assert "synthetic" not in repr(Tokens("synthetic-access", "synthetic-refresh"))


async def test_expired_existing_session_refreshes_once_with_same_device():
    replacement = {
        "meta": {"success": True},
        "authentication": {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        },
    }
    client, session = client_with(
        response(AUTH),
        response(EMPTY),
        response(status=401),
        response(replacement),
        response(READER),
    )
    await client.async_get_loans()
    assert len(await client.async_get_loans()) == 1
    assert session.request.call_count == 5
    refresh = session.request.call_args_list[3]
    assert refresh.kwargs["json"] == {
        "action": "auth",
        "grant_type": "refresh_token",
        "username": "000123",
        "refresh_token": "synthetic-refresh",
        "device_name": "HomeAssistant",
        "device_id": "stable-device",
        "device_platform": "linux",
    }
    assert "password" not in refresh.kwargs["json"]
    assert (
        session.request.call_args_list[4].kwargs["headers"]["Authorization"]
        == "Bearer new-access"
    )


async def test_rejected_refresh_never_retries_password():
    client, session = client_with(
        response(AUTH), response(EMPTY), response(status=401), response(status=403)
    )
    await client.async_get_loans()
    with pytest.raises(BibkatAuthError):
        await client.async_get_loans()
    assert session.request.call_count == 4


async def test_rejected_new_session_does_not_refresh():
    client, session = client_with(response(AUTH), response(status=401))
    with pytest.raises(BibkatAuthError):
        await client.async_get_loans()
    assert session.request.call_count == 2


async def test_second_reader_rejection_ends_recovery():
    client, session = client_with(
        response(AUTH),
        response(EMPTY),
        response(status=401),
        response(AUTH),
        response(status=401),
    )
    await client.async_get_loans()
    with pytest.raises(BibkatAuthError):
        await client.async_get_loans()
    assert session.request.call_count == 5


async def test_concurrent_reads_share_login():
    client, session = client_with(response(AUTH), response(EMPTY), response(EMPTY))
    await asyncio.gather(client.async_get_loans(), client.async_get_loans())
    assert [call.args[0] for call in session.request.call_args_list] == [
        "POST",
        "GET",
        "GET",
    ]


@pytest.mark.parametrize("status", [301, 302, 307, 500])
async def test_errors_and_redirects_do_not_retry_or_leak(status):
    client, session = client_with(response({"secret": "do-not-display"}, status=status))
    with pytest.raises(BibkatError) as caught:
        await client.async_get_loans()
    assert "do-not-display" not in str(caught.value)
    assert session.request.call_count == 1


async def test_rate_limit_is_exposed_without_retry():
    client, session = client_with(response(status=429, headers={"Retry-After": "7200"}))
    with pytest.raises(BibkatRateLimitError) as caught:
        await client.async_get_loans()
    assert caught.value.retry_after == 7200
    assert session.request.call_count == 1


def test_retry_after_date_and_safe_default(freezer):
    freezer.move_to(datetime(2026, 11, 1, 12, tzinfo=UTC))
    assert _retry_after("Sun, 01 Nov 2026 13:00:00 GMT") == 3600
    assert _retry_after("invalid") == 21600
    assert _retry_after(None) == 21600
    assert _retry_after("-1") == 60


@pytest.mark.parametrize(
    "error", [TimeoutError(), aiohttp.ClientConnectionError("secret")]
)
async def test_connection_failures_are_safe(error):
    client, _ = client_with(error)
    with pytest.raises(BibkatConnectionError) as caught:
        await client.async_get_loans()
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"meta": []},
        {"meta": {"success": True}, "authentication": {}},
        {
            "meta": {"success": True},
            "authentication": {"access_token": "bad\n", "refresh_token": "ok"},
        },
    ],
)
async def test_bad_auth_responses_fail(payload):
    client, _ = client_with(response(payload))
    with pytest.raises(BibkatError):
        await client.async_get_loans()


@pytest.mark.parametrize("success", [False, 1, "true", None])
async def test_meta_success_must_be_boolean_true(success):
    client, _ = client_with(response({"meta": {"success": success}}))
    with pytest.raises(BibkatAuthError):
        await client.async_get_loans()


async def test_oversized_and_non_json_responses_fail():
    for reply in (
        response(raw=b"x" * (MAX_RESPONSE_BYTES + 1)),
        response(raw=b"not-json"),
        response(AUTH, content_type="text/html"),
    ):
        client, _ = client_with(reply)
        with pytest.raises(BibkatError):
            await client.async_get_loans()


@pytest.mark.parametrize("reader", [None, {}, {"lendings": None}, {"lendings": [None]}])
def test_missing_loans_is_not_empty(reader):
    with pytest.raises(BibkatError):
        parse_loans({"reader": reader})


@pytest.mark.parametrize(
    "end", ["2026-02-30", "20261115", "2026-11-15T00:00:00", None, "9999-12-31"]
)
def test_bad_due_dates_fail(end):
    payload = {
        "reader": {"lendings": [{"medium": {"id": 1, "title": "Book"}, "end": end}]}
    }
    with pytest.raises(BibkatError):
        parse_loans(payload)


async def test_library_search_uses_public_search_parameter():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = response(
        {
            "meta": {"success": True},
            "catalogs": [
                {
                    "bibid": "BGX610102",
                    "name": "Example Library",
                    "city": "Example City",
                }
            ],
        }
    )
    catalogs = await BibkatClient.search_catalogs(session, " Baccum ")
    assert catalogs[0].catalog_id == "BGX610102"
    request = session.request.call_args
    assert request.args == ("GET", f"{API_BASE_URL}/catalogs/")
    assert request.kwargs["params"] == {"search": "Baccum"}
    assert "Authorization" not in request.kwargs["headers"]


async def test_unsuccessful_reader_does_not_trigger_login_loop():
    client, session = client_with(
        response(AUTH), response({"meta": {"success": False}})
    )
    with pytest.raises(BibkatError):
        await client.async_get_loans()
    assert session.request.call_count == 2


def jwt_with_expiry(expiry):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


async def test_known_jwt_expiry_refreshes_before_reader(freezer):
    freezer.move_to("2026-11-01T12:00:00+00:00")
    expires = datetime(2026, 11, 1, 13, tzinfo=UTC).timestamp()
    auth = {
        "meta": {"success": True},
        "authentication": {
            "access_token": jwt_with_expiry(expires),
            "refresh_token": "refresh",
        },
    }
    client, session = client_with(
        response(auth), response(EMPTY), response(AUTH), response(EMPTY)
    )
    await client.async_get_loans()
    freezer.move_to("2026-11-01T18:00:00+00:00")
    await client.async_get_loans()
    assert [call.args[0] for call in session.request.call_args_list] == [
        "POST",
        "GET",
        "POST",
        "GET",
    ]
    assert (
        session.request.call_args_list[2].kwargs["json"]["grant_type"]
        == "refresh_token"
    )


@pytest.mark.parametrize("expiry", [True, "123", -1, float("inf"), 10**400])
def test_invalid_jwt_expiry_fails_without_disclosure(expiry):
    with pytest.raises(BibkatError):
        _token_expiration(jwt_with_expiry(expiry))


def renewable_reader(*, end="2026-11-15", renewable=True, pending=False):
    data = deepcopy(READER)
    lending = data["reader"]["lendings"][0]
    lending.update(end=end, is_renewable=renewable, is_pending=pending)
    return data


def renewal_receipt(*, end="2026-11-29"):
    return {
        "meta": {"success": True},
        "receipt": {
            "errors": [],
            "lendings": renewable_reader(end=end)["reader"]["lendings"],
        },
    }


async def test_renew_sends_bare_integer_list_and_verifies_new_date():
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(renewal_receipt()),
        response(renewable_reader(end="2026-11-29")),
    )
    result = await client.async_renew("100", date(2026, 11, 15))
    assert result.previous_due_date == date(2026, 11, 15)
    assert result.loan.due_date == date(2026, 11, 29)
    assert result.loans == (result.loan,)
    post = session.request.call_args_list[2]
    assert post.args == ("POST", f"{API_BASE_URL}/catalogs/BGX610102/action/renew/")
    assert post.kwargs["json"] == [100]
    assert post.kwargs["headers"]["Authorization"] == "Bearer synthetic-access"
    assert post.kwargs["allow_redirects"] is False
    assert [call.args[0] for call in session.request.call_args_list] == [
        "POST",
        "GET",
        "POST",
        "GET",
    ]


@pytest.mark.parametrize(
    "medium_id", ["", "all", "100/../101", "0", "-1", "100.0", "0100", 100, True]
)
async def test_invalid_renew_id_never_logs_in(medium_id):
    client, session = client_with()
    with pytest.raises(BibkatRenewalError, match="invalid_renewal_input"):
        await client.async_renew(medium_id, date(2026, 11, 15))
    session.request.assert_not_called()


@pytest.mark.parametrize(
    ("reader", "code"),
    [
        (EMPTY, "loan_not_found"),
        (renewable_reader(renewable=False), "not_renewable"),
        (renewable_reader(pending=True), "not_renewable"),
        (READER, "not_renewable"),
        (renewable_reader(end="2026-11-29"), "due_date_changed"),
    ],
)
async def test_renew_preconditions_never_send_write(reader, code):
    client, session = client_with(response(AUTH), response(reader))
    with pytest.raises(BibkatRenewalError, match=code) as caught:
        await client.async_renew("100", date(2026, 11, 15))
    assert caught.value.loans is not None
    assert session.request.call_count == 2


@pytest.mark.parametrize("flag", ["true", 1, [], {}])
def test_invalid_renewable_flag_is_not_truthy(flag):
    with pytest.raises(BibkatError):
        parse_loans(renewable_reader(renewable=flag))


@pytest.mark.parametrize(
    "reply",
    [
        response(status=401),
        response(status=403),
        response(status=429),
        response(status=500),
        response(status=302),
        response(raw=b"not json"),
        TimeoutError(),
        aiohttp.ClientConnectionError("private-server-data"),
    ],
)
async def test_renew_errors_never_replay_write_or_report_success(reply):
    client, session = client_with(response(AUTH), response(renewable_reader()), reply)
    with pytest.raises(BibkatRenewalError, match="renewal_unconfirmed") as caught:
        await client.async_renew("100", date(2026, 11, 15))
    assert caught.value.loans is None
    assert "private-server-data" not in str(caught.value)
    assert session.request.call_count == 3


@pytest.mark.parametrize(
    "receipt",
    [
        {"meta": {"success": True}},
        {"meta": {"success": True}, "receipt": {"errors": ["private refusal"]}},
        {"meta": {"success": True}, "receipt": {"errors": "invalid", "lendings": []}},
        {"meta": {"success": True}, "receipt": {"lendings": []}},
        renewal_receipt(end="2026-11-15"),
    ],
)
async def test_receipt_must_confirm_requested_medium(receipt):
    current = renewable_reader(end="2026-11-29")
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(receipt),
        response(current),
    )
    with pytest.raises(BibkatRenewalError, match="renewal_unconfirmed") as caught:
        await client.async_renew("100", date(2026, 11, 15))
    assert caught.value.loans == parse_loans(current)
    assert "private refusal" not in str(caught.value)
    assert session.request.call_count == 4


@pytest.mark.parametrize(
    "reader", [EMPTY, renewable_reader(), renewable_reader(end="2026-11-28")]
)
async def test_reader_must_agree_with_receipt(reader):
    client, _ = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(renewal_receipt()),
        response(reader),
    )
    with pytest.raises(BibkatRenewalError, match="renewal_unconfirmed"):
        await client.async_renew("100", date(2026, 11, 15))


async def test_followup_network_failure_is_not_success():
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(renewal_receipt()),
        TimeoutError(),
    )
    with pytest.raises(BibkatRenewalError, match="renewal_unconfirmed"):
        await client.async_renew("100", date(2026, 11, 15))
    assert session.request.call_count == 4


async def test_concurrent_duplicate_requests_only_renew_once():
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(renewal_receipt()),
        response(renewable_reader(end="2026-11-29")),
        response(renewable_reader(end="2026-11-29")),
    )
    results = await asyncio.gather(
        client.async_renew("100", date(2026, 11, 15)),
        client.async_renew("100", date(2026, 11, 15)),
        return_exceptions=True,
    )
    assert results[0].loan.due_date == date(2026, 11, 29)
    assert isinstance(results[1], BibkatRenewalError)
    assert results[1].code == "due_date_changed"
    assert (
        sum(
            call.args[1].endswith("/action/renew/")
            for call in session.request.call_args_list
        )
        == 1
    )


async def test_expired_session_is_refreshed_before_not_after_write(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(renewal_receipt()),
        response(renewable_reader(end="2026-11-29")),
    )
    client._tokens = Tokens("expired-access", "existing-refresh", 1)
    await client.async_renew("100", date(2026, 11, 15))
    assert (
        session.request.call_args_list[0].kwargs["json"]["grant_type"]
        == "refresh_token"
    )
    assert session.request.call_args_list[2].kwargs["json"] == [100]


async def test_duplicate_medium_in_account_is_rejected():
    reader = renewable_reader()
    reader["reader"]["lendings"] *= 2
    client, session = client_with(response(AUTH), response(reader))
    with pytest.raises(BibkatRenewalError, match="loan_not_found"):
        await client.async_renew("100", date(2026, 11, 15))
    assert session.request.call_count == 2


async def test_rate_limit_on_renew_blocks_followup_reads_and_manual_retries():
    client, session = client_with(
        response(AUTH),
        response(renewable_reader()),
        response(status=429, headers={"Retry-After": "7200"}),
    )
    with pytest.raises(BibkatRenewalError, match="renewal_unconfirmed"):
        await client.async_renew("100", date(2026, 11, 15))
    with pytest.raises(BibkatRateLimitError):
        await client.async_get_loans()
    with pytest.raises(BibkatRateLimitError):
        await client.async_renew("100", date(2026, 11, 15))
    assert session.request.call_count == 3
