# Dependency compatibility — release blocked

Checked on 2026-10-21. The plugin candidate requires `aiohttp>=3.14.4,<4`.
Versions through 3.14.3 have known client resource-consumption issues; see
[the upstream advisory](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-2g87-pp6c-29x5).
The bound intentionally prevents falling back to a known vulnerable version.

The current `NousResearch/hermes-agent` main pyproject was read again during
this step. Its content matches Git blob
`897a5e028990a8d31d6c089db317e830936c62c2`, also present at the reviewed core
commit `278aeffe16945e7b1405e3c471ad08553c059237`. It still declares
`aiohttp==3.14.3` in messaging and other extras.

These requirements have no common version. A pip resolution dry run with both
requirements reproduced `ResolutionImpossible`; no installation was performed.
This is a focused constraint check, not a full Hermes PM resolution.

## What has passed

- The TDD security-floor tests reject 3.9.0, 3.14.2 and 3.14.3 while allowing
  3.14.4 and 3.14.5 and retaining the <4 boundary.
- A real loopback WebSocket regression showed the old cookie jar storing a
  response cookie. With DummyCookieJar authentication still works and no cookie
  is stored. This does not replace aiohttp's parser fixes.
- Four new failures were recorded before these changes; the complete suite then
  passed: **69 tests on aiohttp 3.14.4** in an isolated development environment.
- pip-audit 2.10.1 found no known advisories in the nine installed runtime packages
  checked against PyPI on this date. Development and Hermes-wide dependencies
  are outside that inventory; absence of known advisories is not a guarantee.

## What remains blocked

No compatible Hermes release is claimed. `requires_hermes >=0.21.5` remains the
inherited API floor; it has not been raised to an invented release. The candidate
must not be published or submitted to the catalog until a compatible core exists
and `hermes plugins validate … --install-deps` succeeds using Hermes PM.

Do not bypass Hermes constraints, replace its packages with pip, vendor a second
aiohttp, or downgrade the security floor to make the solver pass. No upstream
files or user Hermes environment have been modified. A core dependency update
would require separate scope/approval; no issue or PR has been created here.
