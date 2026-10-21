# Home Assistant Statistics for Hermes

A community plugin with two read-only Home Assistant Recorder tools:

- `ha_list_statistics`: discover statistic IDs, optionally filtered by `search`.
- `ha_get_statistics`: request aggregated values over a timezone-aware time range.

The toolset is `homeassistant-statistics`. It can run alongside the official
Home Assistant plugin or on its own; it does not import that plugin, register a
platform, subscribe to events or call Home Assistant services.

## Status

Version 0.1.0 is published as an independent community plugin. It is not yet
listed in a Hermes plugin catalog. The hardening includes redirect rejection,
fixed public errors, a patched dependency floor, discarded cookies,
query/result budgets, per-call filters and bounded WebSocket closure. The
91-test suite passes in the isolated development environment. The plugin has
also been installed and exercised against a live Home Assistant instance; see
[Compatibility](#compatibility) before installing it on another Hermes host.

## Compatibility

Tested on 2026-10-21 with Hermes core revision
`c7ed47f18b1e7d3116c1740993f904e8a54e5acc`, which pins `aiohttp==3.14.5`.
This plugin requires `aiohttp>=3.14.4,<4` for
[client security fixes](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-2g87-pp6c-29x5).
Hermes PM resolved the combined dependencies, the plugin validator passed, and
both tools were exercised through Open WebUI against a live Home Assistant
instance. The 91 tests also pass on Python 3.14.5 with aiohttp 3.14.4 and
3.14.5.

Hermes `>=0.21.5` is the inherited API floor, not a general installation
guarantee: an installation needs a Hermes build compatible with this aiohttp
range. In particular, the checked stable v0.22.4 pin to aiohttp 3.14.3 conflicts
with this plugin. Do not downgrade aiohttp or override Hermes's pins to work
around the conflict.

## Requirements and configuration

Python 3.11+, Hermes >=0.21.5, `aiohttp>=3.14.4,<4`, and a Home Assistant instance
with Recorder statistics and a Long-Lived Access Token. The exact Hermes revision
validated is listed above; other Python and Hermes versions have not been tested.

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
dependency-resolution check passed on the validated core revision; see
[Compatibility](#compatibility).

## License and origin

MIT; [LICENSE](LICENSE) retains the original Nous Research copyright and
permission notice.

Code and tests come from the Home Assistant plugin in
NousResearch/hermes-official-plugins and [PR #126](https://github.com/NousResearch/hermes-official-plugins/pull/126),
based on commit `1690b63a4790cb821fb6a319ba524815d4e09411`:

- **goncami**, commit `aad83dca65c8f9ca865fcf39c09be9535f53221b`: the two
  statistics tools, schemas, Recorder WebSocket calls, input validation and
  initial tests.
- **teknium1 (Teknium)**, commit `3a3d2c3b57442a527c04bd8092aa4497541abc0b`:
  allow/deny filtering, three filter tests and reuse of the existing 64 MiB
  receive cap for statistics, with the corresponding protocol test update.
- The configuration, pattern matching, JSON response and sync/async helpers
  come from the official plugin. The 64 MiB constant predates the statistics PR.

This standalone project retains the statistics tools and adds its own manifest,
registration, regression tests, transport protection, query/result limits and
performance changes. The original filter precedence and transport cap remain.
This is an independent community plugin, not an official Nous Research or
Home Assistant product; it does not imply endorsement by the original authors.
