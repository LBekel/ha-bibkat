# Bibkat for Home Assistant

Unofficial Home Assistant integration for Bibkat library accounts.
Displays current loans and due dates as sensors and a calendar, and provides
a manually triggered action to renew a single item.

This project is independent of Bibkat and the library software provider.
It uses an undocumented API; changes to the service may break the integration.
Compatibility with all libraries is not guaranteed.

> **Renewals: experimental.** The request and response format was reconstructed
> from the Android client and tested with synthetic responses, but has not yet
> been verified by performing a real renewal.
> Calling `bibkat.renew` changes the library account.

## Features

- UI-based setup with library search and reader account credentials.
- Multiple accounts, for example one entry per family member.
- Four sensors per account: borrowed items, loan list, overdue items, and
  next due date.
- One calendar per account with an all-day event for each borrowed item.
- Shared updates every six hours.
- Overdue status recalculated at midnight in the Home Assistant time zone.
- UI-based reauthentication if account access is rejected.
- A `bibkat.renew` action to explicitly renew a single item.

Setup and periodic polling never renew items. The integration does not create
automations or support reservations, account editing, or loan history queries.

## Requirements

- **Home Assistant 2026.10.4 or later.**
- An active reader account at a library available through Bibkat search.
- Internet access to `https://eopac.net`.

Library search, login, loan retrieval, and token refresh have been verified
with the Baccum library. Other libraries may have different access requirements.
Only use the integration for accounts you are authorized to access, and follow
your library's terms.

## Installation

### Via Marketplace (aka HACS)

Once published at `https://github.com/lbekel/ha-bibkat`:

1. Open **Custom repositories** in Marketplace (aka HACS).
2. Add `https://github.com/lbekel/ha-bibkat` with the type **Integration**.
3. Download **Bibkat** and restart Home Assistant.
4. Start setup under **Settings > Devices & services > Add integration > Bibkat**.

The repository is added as a custom repository; inclusion in the default
Marketplace (aka HACS) catalog is not assumed.

### Manual installation

1. Download and extract the source code for the desired version.
2. Copy the `custom_components\bibkat` folder and its contents to the same
   location within your Home Assistant configuration directory.
   The folder must contain `manifest.json` directly.
3. Restart Home Assistant and add **Bibkat** as described above.

Only the integration folder is needed on Home Assistant, not the tests or
development files. No YAML configuration or `.env` file is required.

### Setting up an account

Find and select your library, then enter your reader number, password, and an
account label. Run setup again to add more accounts. The account label helps
distinguish devices and entities.

### Updating and removing

Download updates through Marketplace (aka HACS) or replace the integration
folder with the new version, then restart Home Assistant. Existing accounts
do not need to be set up again.

To remove the integration, first delete the Bibkat configuration entries under
**Devices & services**, then remove the integration in Marketplace (aka HACS)
or delete its folder and restart Home Assistant. Remove your own dashboard
cards and automations separately as needed.

## Sensors and calendar

| Entity | State / content |
|---|---|
| Borrowed items | Number of current loans; details in the `loans` attribute |
| Loan list | Number of current loans; full list in the `loans` attribute |
| Overdue items | Number of items whose due date has passed |
| Next due date | Earliest due date across all current loans, including overdue ones |
| Due date calendar | One all-day event per loan; items due on the same day remain separate events |

The loan period includes the due date. With no loans, the count sensors show `0`
and the date sensor has no value. Network or API errors result in `unavailable`,
not an empty loan list. The calendar only shows current loans, not a persistent
history.

The `loans` attribute contains the following fields for each item:

| Field | Meaning |
|---|---|
| `title` | Title |
| `author` | Author, if available |
| `due_date` | Due date in `YYYY-MM-DD` format |
| `medium_id` | Item ID as a string; not an ISBN or list position |
| `is_renewable` | Whether the API reports that the item can be renewed |
| `is_pending` | Pending status reported by the API; renewal is blocked when `true` |

The full list is stored in the attribute, not in the sensor state, which is
limited to 255 characters. Both loan lists use the same data without additional
API requests.

### Dashboard example

Add a **Markdown card** and replace `sensor.your_account_loan_list` with the
actual entity ID:

```yaml
type: markdown
title: Borrowed items
content: |-
  {% set entity = 'sensor.your_account_loan_list' %}
  {% if not has_value(entity) %}
  Loans are currently unavailable.
  {% else %}
  {% for loan in state_attr(entity, 'loans') or [] %}
  - **{{ loan.title }}** - Due: {{ as_datetime(loan.due_date).strftime('%Y-%m-%d') }}
  {% else %}
  No items currently borrowed.
  {% endfor %}
  {% endif %}
```

You can also display the due date calendar in a standard calendar card.

### Updates and reminders

New loans and items renewed outside the integration appear after the next
successful update. To refresh sooner, call `homeassistant.update_entity` for
a Bibkat sensor. Do not force repeated manual updates when rate-limited.

You can create your own Home Assistant reminder automations using calendar
triggers with a negative offset. All-day events start at local midnight;
choose the notification time accordingly. Use `mode: queued` or `parallel`
when multiple events occur at once. For a combined notification at a fixed
time, a time-triggered automation can evaluate the `loans` attribute.

## Renewing a single item

**This experimental action makes a real change to the library account.**
It has not yet been verified with a real renewal.

1. Find the item's `medium_id` and `due_date` in the desired account's
   `loans` attribute.
2. Under **Tools > Actions** or **Developer tools > Actions**, select
   **Bibkat: Renew medium**.
3. Select the reader account, enter the item ID as a string and its current
   due date, and only then choose **Perform action**.

Example with placeholders, not a preconfigured automation:

```yaml
action: bibkat.renew
data:
  config_entry_id: "YOUR_BIBKAT_CONFIG_ENTRY_ID"
  medium_id: "123456"
  expected_due_date: "2026-11-15"
```

The action is restricted to administrators. Explicitly configured Home Assistant
automations can also call it. Do not configure loops or automatic retries for
this action.

The account is fetched again before writing: exactly one matching loan must
exist, the item must be renewable, and it must not be marked as pending.
Missing renewability information is not treated as permission. The due date
must still match `expected_due_date`. If it has already changed, the action
aborts without sending a write request. This protects against repeated calls
with stale data, but does not replace a server-side idempotency guarantee.
The library ultimately decides whether renewal is allowed.

The client sends exactly one renewal POST without automatic retries. Success
is only reported when the receipt and a fresh loan fetch show the same, later
due date for the item. The action then updates the sensors and calendar and
returns `medium_id`, `title`, `previous_due_date`, and `due_date`. In your own
automation, you can capture the response using `response_variable`.

If **renewal could not be confirmed**, the change may already have taken place,
for example after a timeout. **Check Bibkat first; do not blindly repeat the
action.** Without fresh loan data, the entities are temporarily marked as
unavailable. Available fresh data is displayed even if the action could not
confirm success.

## Privacy and credentials

Home Assistant stores the reader number and password in its local configuration,
not in an encrypted password vault. Protect your configuration and backups.
The integration does not read `.env` files.

Access and refresh tokens remain in memory; a new login is performed after a
restart. A device UUID remains constant per configuration entry, including
after reauthentication. Credentials are only sent to the fixed HTTPS Bibkat API;
redirects are not followed.

The `loans` sensor attribute is excluded from Recorder storage but remains
available to dashboards and templates. Calendar states may contain book titles.
If you do not want them in Home Assistant history, exclude the relevant Bibkat
calendars in your Recorder configuration.

## Troubleshooting and feedback

| Problem | Suggested action |
|---|---|
| Login rejected | Check your reader number and password with the library; use the reauthentication flow offered by Home Assistant |
| Entities unavailable | Check network access and service availability; this does not mean there are no loans |
| Too many requests / HTTP 429 | Wait for the specified delay; without a valid `Retry-After`, the client waits six hours |
| Renewal unavailable | Check the current item ID, due date, and renewability; check Bibkat for possible changes first |

Once the repository is published, report bugs through
[GitHub Issues](https://github.com/lbekel/ha-bibkat/issues).
Include the integration version, Home Assistant version, steps to reproduce,
and a sanitized error message.
**Do not publish passwords, reader numbers, tokens, raw responses, private loan
lists, or unredacted screenshots.**

## Development

The entire integration is located in `custom_components\bibkat`.
The tests in `tests` use synthetic accounts and loans; they require neither
real credentials nor a connection to the Bibkat API.

In a separate **Linux environment with Python 3.14.2 or later**:

```text
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-ha-test.txt
python -m pytest -q
python -m ruff check custom_components tests
```

On Windows, use WSL; Home Assistant itself is not supported on Windows.
Prefer creating the test environment in the native Linux filesystem.
The test dependency set pins Home Assistant to 2026.10.4.
The GitHub Actions workflow runs tests, Ruff, Hassfest, and Marketplace (aka HACS)
validation.

The undocumented API base URL is `https://eopac.net/api/bibkat/v5`.
Endpoints used:

| Purpose | Request |
|---|---|
| Library search | `GET /catalogs/?search=...` |
| Login / token refresh | `POST /catalogs/{bibid}/auth/` |
| Current loans | `GET /catalogs/{bibid}/reader/` |
| Single-item renewal | `POST /catalogs/{bibid}/action/renew/` with a JSON list such as `[123456]` |

The renewal format is based on Android client 3.8.2
(`MediumRenewalButtonController` > `MediumActionRepo.renew` >
`MediumActionApi.renew`). The receipt contains `meta` and `receipt` with
`errors` and `lendings`. This is not an official server specification.
APK files, private analysis results, and credentials do not belong in the repository.

## License

The source code is licensed under the [MIT License](LICENSE).
Bibkat names and trademarks and third-party app files are not covered by this license.
