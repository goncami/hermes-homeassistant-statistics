# Origin and attribution

This project derives selected code and tests from the MIT-licensed Home Assistant
plugin in NousResearch/hermes-official-plugins. Its LICENSE is reproduced here
with the original Nous Research copyright and permission notice intact.

Source: https://github.com/NousResearch/hermes-official-plugins/pull/126

- Base: `1690b63a4790cb821fb6a319ba524815d4e09411`.
- Original implementation by **goncami**:
  `aad83dca65c8f9ca865fcf39c09be9535f53221b` — two statistics tools, schemas,
  Recorder WebSocket exchange, input validation and statistics/protocol tests.
- Subsequent review improvements by **teknium1 (Teknium)**:
  `3a3d2c3b57442a527c04bd8092aa4497541abc0b` — apply existing operator allow/deny
  patterns to statistics discovery and direct queries, three filter regressions,
  use the existing 64 MiB cap in the new exchange, and update the protocol test.
- Existing shared helpers (operator matching, scoped configuration, JSON envelopes,
  sync/async dispatch and context propagation) originate in the official plugin.
  The 64 MiB constant already existed for registry retrieval; teknium1 reused it
  for statistics rather than introducing that constant.

Standalone extraction: goncami's project.
Only the statistics capabilities and required helpers were retained. New work:
independent manifest/entry point/toolset; no adapter dependency; validation of
non-string period/type values; an explicit whole-exchange WebSocket deadline;
standalone regression tests and documentation. Subsequent local hardening adds
redirect rejection, structural URL checks and fixed public error categories,
covered by loopback HTTP(S) regressions.

This extraction does not imply that either original author has reviewed or
endorsed the standalone candidate. No author email addresses or private data are
needed to preserve this attribution. Consult GitHub for original commit history.
