# Home Assistant Statistics for Hermes

A community plugin with two read-only Home Assistant Recorder tools:

- `ha_list_statistics`: discover statistic IDs, optionally filtered by `search`.
- `ha_get_statistics`: request aggregated values over a timezone-aware time range.

The toolset is `homeassistant-statistics`. It can run alongside the official
Home Assistant plugin or on its own; it does not import that plugin, register a
platform, subscribe to events or call Home Assistant services.

## Status

Local pre-publication candidate, version 0.1.0. No remote repository or catalog
entry has been created. Installation from GitHub will be documented after approval.
Security remediation is in progress: transport/error hardening is implemented;
patched dependencies and cookie suppression are prepared. Installation compatibility
with Hermes is currently blocked; query/result budgets are implemented and
filter and connection-lifecycle improvements are implemented. See DEPENDENCY_COMPATIBILITY.md before trying to install this candidate.

## Requirements and configuration

Python 3.11+, Hermes >=0.21.5, `aiohttp>=3.14.4,<4`, and a Home Assistant instance
with Recorder statistics and a Long-Lived Access Token. The Hermes floor follows
the source plugin; this candidate has been tested against the core
revision `278aeffe16945e7b1405e3c471ad08553c059237`, not every supported release. This API floor is not a claim
that dependencies resolve with that release: the checked core pins aiohttp 3.14.3,
which conflicts with this candidate's security floor. No compatible Hermes release
has been established yet.

Configure through Hermes's normal profile configuration/secret setup:

| Setting | Purpose |
| --- | --- |
| `HASS_TOKEN` | Required Long-Lived Access Token, resolved by `agent.secret_scope.get_secret` |
| `HASS_URL` | Instance base URL; default `http://homeassistant.local:8123` |
| `HASS_ENTITY_ALLOWLIST` | Optional comma-separated ID patterns; when set, only matches are permitted |
| `HASS_ENTITY_DENYLIST` | Optional comma-separated ID patterns; deny always wins |

Configuration is read at call time from the active Hermes profile. There are no
credential files in this repository. Do not commit tokens or instance-specific URLs.
The same `HASS_*` settings can serve the official plugin and this plugin.

`HASS_URL` must be the final HTTP(S) base URL, with a host and optional proxy path.
Embedded credentials, query strings, fragments and whitespace are rejected before
connecting. Private hosts, IPv6 and local HTTP are supported. All HTTP redirects
are rejected before connecting to their destination; configure the final base URL
instead of relying on a proxy redirect. An empty or whitespace-only token fails
before any connection is attempted.

Filters apply to discovery and direct queries. An excluded ID rejects the entire
query before any WebSocket request. Patterns retain the official plugin's exact,
glob and family-prefix semantics (`.`/`_` boundary equivalence); IDs are lowercased
for matching, so write patterns in lowercase. External IDs such as
`custom:energy` are supported and must also match an active allowlist.
These are plugin filters, not a replacement for Home Assistant access control.
Patterns are read and parsed once per call, then read again on the next call;
there is no shared filter cache across profiles or requests.

After installing locally with `hermes plugins install /absolute/path/to/this/repo`,
use `hermes tools` to enable the toolset for the relevant platform and begin a new
conversation. Tool availability requires `HASS_TOKEN`. Hermes controls whether
plugin tools are exposed directly or deferred through Tool Search.

## Examples

Discover with `ha_list_statistics({"search": "energy"})`, then query an ID returned
by discovery:

```json
{
  "statistic_ids": ["sensor.example_energy"],
  "start_time": "2026-10-01T00:00:00+02:00",
  "end_time": "2026-10-02T00:00:00+02:00",
  "period": "day",
  "types": ["sum", "change"]
}
```

Periods: `5minute`, `hour`, `day` (default), `week`, `month`, `year`.
Types: `mean`, `min`, `max`, `state`, `sum`, `change` (all requested by default).
`end_time` is optional; supplied times require a timezone and end must be after
start. `5minute` queries use Recorder's short-term data and its retention limits;
other periods use long-term aggregates. Available fields depend on the statistic
and Recorder configuration. For interval energy consumption, `change` is normally
more useful than treating the cumulative `sum` as consumption.

Success responses are JSON with a `result` field; failures have an `error` field.
### Resource budgets

The following positive-integer settings are read from the active profile for
each call. Invalid settings fail before connecting:

| Setting | Default | Budget |
| --- | --- | --- |
| `HASS_STATISTICS_MAX_IDS` | 100 | Distinct IDs in one query |
| `HASS_STATISTICS_MAX_ROWS` | 10000 | Estimated query rows, actual rows across all returned series, or matching discovery entries |
| `HASS_STATISTICS_MAX_BYTES` | 1048576 | Bytes of the complete serialized successful tool response, including its JSON envelope |

Duplicate IDs and types are sent once, preserving their original order. A
conservative point estimate uses the requested period, time range and ID count
before contacting Recorder; omitting `end_time` uses the current time for that
estimate. Partial bins and calendar/DST variation are allowed for in the estimate,
so a query close to the budget can be refused even if the actual data is sparse.
Coarse monthly/yearly queries can still cover long histories.

Returned rows and serialized bytes are checked independently. Discovery applies
operator filters and `search` before checking the matching-entry budget. A budget
failure returns an error asking for a narrower search, fewer IDs, a shorter range
or coarser aggregation. No successful partial history is returned and nothing is
silently truncated. Error messages themselves are outside the success-byte cap.

The 64 MiB WebSocket receive cap is retained for large installations. Recorder's
response is still received and decoded before the actual row/output checks;
these budgets do not bound peak JSON-decoding memory to 1 MiB and cannot stop
backend work that has already started. The preflight estimate reduces oversized
normal queries. There is no pagination or automatic background fetching.

## Network and data disclosure

Each call opens an authenticated connection to the configured Home Assistant
`/api/websocket`, executes `recorder/list_statistic_ids` or
`recorder/statistics_during_period`, and closes it. The whole exchange has a
30-second deadline and a 64 MiB maximum received message. HTTPS uses normal TLS
verification. HTTP uses an unencrypted connection, including token authentication;
choose HTTPS where needed. Redirects are rejected even within the same origin.
Errors returned to the model and written to plugin logs use fixed categories;
raw exception text, configured URLs and remote error messages are not reflected.
A DummyCookieJar discards response cookies, which HA token authentication does not
need. After receiving the result, the WebSocket close handshake waits at most one
second (also bounded by the overall request deadline), so a peer that fails to
acknowledge closure does not delay delivery for the library's default ten seconds.

No direct database access, telemetry, file reads of credentials, credential
storage, self-updates, background services or shell commands. The token retains
its Home Assistant privileges; this plugin only exposes the two read operations.
Returned statistics enter the agent's tool results and may reach its configured
model provider. Discovery fetches Recorder metadata before locally applying filters.

## Development and validation

Use a separate test environment with the dependencies from `pyproject.toml`'s
`test` extra and a Hermes checkout on `PYTHONPATH` (Hermes is supplied by the host,
not installed as a plugin dependency):

```sh
PYTHONPATH=/absolute/path/to/hermes-agent python -m pytest tests -q
hermes plugins validate /absolute/path/to/this/repo --install-deps
hermes plugins doctor /absolute/path/to/this/repo --ci
```

Tool callbacks retain Hermes's synchronous worker contract. Async callers should
run synchronous registry dispatch in a worker rather than calling a callback
directly on their event loop. Cancelling a worker Future does not forcibly stop
its thread; the operation's network deadline remains the bound in that case.
Cancelling the native network coroutine releases its client session.

Tests use mocked traffic plus temporary loopback HTTP(S) servers and synthetic
credentials, never a live HA token. `trustme` (test extra only) creates ephemeral
TLS certificates in memory. Integration tests need permission to bind loopback
ports; no test contacts a user's Home Assistant instance.
The original statistics tests are retained with import-path changes. Additional
coverage checks profile propagation, malformed inputs, deadline enforcement,
external statistic filters and exact two-tool registration. The complete Hermes
dependency-resolution gate remains pending; see DEPENDENCY_COMPATIBILITY.md.

## License and origin

MIT; the original Nous Research copyright and permission notice are retained in
LICENSE. Derived from the Home Assistant plugin and PR #126 by goncami, including
teknium1's filtering and WebSocket improvements. See NOTICE.md for exact commits
and attribution. This is an independent community plugin, not an official Nous
Research or Home Assistant product.
