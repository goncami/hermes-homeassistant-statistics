"""Read-only Recorder statistics. See README.md for origin and attribution."""
import asyncio
import fnmatch
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)
TOOLSET = "homeassistant-statistics"


def tool_error(message: str) -> str:
    """Use the same error format as Hermes's tools.registry.tool_error."""
    return json.dumps({"error": str(message)}, ensure_ascii=False)


class _PublicHAError(RuntimeError):
    """Safe to show in tool results and logs. Do not pass server error text."""


@dataclass(frozen=True)
class _StatisticsLimits:
    max_ids: int = 100
    max_rows: int = 10_000
    max_bytes: int = 1_048_576


_DEFAULT_LIMITS = _StatisticsLimits()


def _get_statistics_limits() -> _StatisticsLimits:
    values = {}
    for field, default in (("ids", _DEFAULT_LIMITS.max_ids), ("rows", _DEFAULT_LIMITS.max_rows),
                           ("bytes", _DEFAULT_LIMITS.max_bytes)):
        name = "HASS_STATISTICS_MAX_" + field.upper()
        try:
            value = int(get_secret(name, str(default)))
        except (TypeError, ValueError):
            raise _PublicHAError(f"{name} must be a positive integer") from None
        if value <= 0:
            raise _PublicHAError(f"{name} must be a positive integer")
        values["max_" + field] = value
    return _StatisticsLimits(**values)


def _bounded_result_json(result, max_bytes: int) -> str:
    # ASCII encoding lets us count bytes without building the whole response first.
    chunks = []
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=True).iterencode({"result": result}):
        size += len(chunk)
        if size > max_bytes:
            raise _PublicHAError(
                f"Statistics result exceeds the {max_bytes}-byte output budget; "
                "narrow the search, use fewer IDs or split the time range"
            )
        chunks.append(chunk)
    return "".join(chunks)


def _check_query_budget(ids, start, end, period, limits):
    if len(ids) > limits.max_ids:
        raise _PublicHAError(
            f"Too many statistic IDs (limit {limits.max_ids}); split the request"
        )
    # Short periods account for daylight saving time; the extra row covers partial bins.
    seconds = {"5minute": 300, "hour": 3600, "day": 23 * 3600,
               "week": 167 * 3600, "month": 28 * 86400 - 3600,
               "year": 365 * 86400 - 3600}[period]
    end = end if end is not None else datetime.now(timezone.utc)
    duration = max(0, (end - start).total_seconds())
    estimated_rows = len(ids) * (math.ceil(duration / seconds) + 1)
    if estimated_rows > limits.max_rows:
        raise _PublicHAError(
            f"Query may exceed the {limits.max_rows}-row budget; use fewer IDs, "
            "split the time range or select a coarser period"
        )


def _check_row_budget(count, max_rows):
    if count > max_rows:
        raise _PublicHAError(
            f"Statistics result exceeds the {max_rows}-row budget; narrow the search, "
            "use fewer IDs or split the time range; no partial data was returned"
        )


def url_scheme_problem(url: str) -> Optional[str]:
    """Check HASS_URL without including its value in errors."""
    problem = (
        "Invalid HASS_URL: use an absolute http:// or https:// URL with a host, "
        "without credentials, query, fragment or whitespace"
    )
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return problem
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0):
            return problem
    except ValueError:
        return problem
    return None


_HASS_ENTITY_ALLOWLIST: str = ""


_HASS_ENTITY_DENYLIST: str = ""


def _get_config():
    """Return the active profile's validated base URL and nonempty token."""
    hass_url = (get_secret("HASS_URL", "http://homeassistant.local:8123") or "").rstrip("/")
    if problem := url_scheme_problem(hass_url):
        raise _PublicHAError(problem)
    token = (get_secret("HASS_TOKEN", "") or "").strip()
    if not token:
        raise _PublicHAError("HASS_TOKEN is not configured")
    return hass_url, token


def _get_entity_filter_config():
    """Read this profile's filters. Deny takes precedence over allow."""
    allow = _HASS_ENTITY_ALLOWLIST or (get_secret("HASS_ENTITY_ALLOWLIST", "") or "")
    deny = _HASS_ENTITY_DENYLIST or (get_secret("HASS_ENTITY_DENYLIST", "") or "")
    return _parse_entity_filter_patterns(allow), _parse_entity_filter_patterns(deny)


def _parse_entity_filter_patterns(value: str) -> List[str]:
    """Split a comma-separated env value into trimmed, non-empty patterns."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _entity_matches(entity_id: str, patterns: List[str]) -> bool:
    """Match exact IDs, shell globs or family prefixes.

    Family prefixes accept either separator: office_thermostat_* matches
    office_thermostat.0_x. A bare climate.office only matches that exact ID.
    """
    for pat in patterns:
        if not pat:
            continue
        if entity_id == pat or fnmatch.fnmatch(entity_id, pat):
            return True
        # Only patterns ending in *, . or _ can match a family.
        if pat[-1] in "*._":
            base = pat.rstrip("*._")
            if base and entity_id.startswith(base) and (
                len(entity_id) == len(base) or entity_id[len(base)] in "._"
            ):
                return True
    return False


def _operator_filter_violation(entity_id: str, filters=None) -> Optional[str]:
    """Return why the active operator filters exclude an ID, or None."""
    allow, deny = _get_entity_filter_config() if filters is None else filters
    if deny and _entity_matches(entity_id, deny):
        return "excluded by HASS_ENTITY_DENYLIST"
    if allow and not _entity_matches(entity_id, allow):
        return "not in HASS_ENTITY_ALLOWLIST"
    return None


_REGISTRY_MAX_MSG = 64 * 1024 * 1024


def _run_async(coro):
    """Run a coroutine from a synchronous handler, waiting for its result."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():  # already inside a loop: asyncio.run() needs its own thread
        import concurrent.futures
        import contextvars
        # Keep the active profile's secrets available in the worker thread.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


def _dispatch(coro, log_name: str, fail_msg: str, max_bytes: int = _DEFAULT_LIMITS.max_bytes) -> str:
    """Return the result as JSON, or a public error message if the call fails."""
    try:
        return _bounded_result_json(_run_async(coro), max_bytes)
    except Exception as exc:
        import aiohttp
        # Raw exceptions may contain credentials or private URLs.
        if isinstance(exc, _PublicHAError):
            detail = str(exc)
        elif isinstance(exc, TimeoutError):
            detail = "Home Assistant request timed out"
        elif isinstance(exc, aiohttp.ClientError):
            detail = "Unable to connect to Home Assistant; check URL, network and TLS"
        else:
            detail = "Home Assistant request failed"
        logger.error("%s: %s", log_name, detail)
        return tool_error(f"{fail_msg}: {detail}")


def _check_ha_available() -> bool:
    """Tool is only available when HASS_TOKEN is set."""
    return bool((get_secret("HASS_TOKEN", "") or "").strip())


_ALLOWED_STAT_PERIODS = {'5minute', 'hour', 'day', 'week', 'month', 'year'}


_ALLOWED_STAT_TYPES = {'mean', 'min', 'max', 'state', 'sum', 'change'}


def _parse_iso_datetime(value: str, parameter: str):
    from datetime import datetime

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing required parameter: {parameter}')

    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(
            f'Invalid {parameter}: expected an ISO-8601 datetime'
        )

    if parsed.tzinfo is None:
        raise ValueError(
            f'Invalid {parameter}: timezone offset is required'
        )

    return parsed


async def _ws_command(message: dict, timeout: int = 30):
    import aiohttp

    base_url, token = _get_config()

    if base_url.startswith('https://'):
        ws_url = 'wss://' + base_url[len('https://'):]
    elif base_url.startswith('http://'):
        ws_url = 'ws://' + base_url[len('http://'):]
    else:
        raise RuntimeError('HASS_URL must start with http:// or https://')

    ws_url = ws_url.rstrip('/') + '/api/websocket'
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async def reject_redirect(session, context, params):
        # Reject redirects before we can send the token to a different endpoint.
        raise _PublicHAError("Home Assistant redirects are not allowed; configure the final HASS_URL")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    async with asyncio.timeout(timeout):
        # HA uses token authentication, so we don't need cookies.
        async with aiohttp.ClientSession(
            timeout=client_timeout, trace_configs=[trace], cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            # Large statistic lists can exceed aiohttp's default 4 MiB message limit.
            async with session.ws_connect(
                ws_url, max_msg_size=_REGISTRY_MAX_MSG,
                timeout=aiohttp.ClientWSTimeout(ws_close=min(1.0, timeout)),
            ) as ws:
                hello = await ws.receive_json()
                if hello.get('type') != 'auth_required':
                    raise _PublicHAError('Unexpected Home Assistant WebSocket greeting')

                await ws.send_json({
                    'type': 'auth',
                    'access_token': token,
                })

                auth = await ws.receive_json()
                if auth.get('type') != 'auth_ok':
                    raise _PublicHAError('Home Assistant WebSocket authentication failed')

                command = dict(message)
                command['id'] = 1
                await ws.send_json(command)

                response = await ws.receive_json()

                if response.get('id') != 1:
                    raise _PublicHAError('Unexpected Home Assistant WebSocket response id')

                if response.get('type') != 'result':
                    raise _PublicHAError('Unexpected Home Assistant WebSocket response')

                if not response.get('success'):
                    error = response.get('error') or {}
                    detail = (
                        'Unknown command' if isinstance(error, dict)
                        and error.get('code') == 'unknown_command'
                        else 'Recorder rejected the statistics request'
                    )
                    raise _PublicHAError('Home Assistant WebSocket command failed: ' + detail)

                return response.get('result')


def _statistic_filter_violation(statistic_id: str, filters=None) -> Optional[str]:
    """Return the filter rule that excludes this statistic, or None.

    Entity statistics use the entity ID. External IDs such as domain:name
    follow the same allow/deny patterns.
    """
    return _operator_filter_violation(statistic_id.lower(), filters)


async def _async_list_statistics(search=None, max_rows=_DEFAULT_LIMITS.max_rows):
    filters = _get_entity_filter_config()
    result = await _ws_command({
        'type': 'recorder/list_statistic_ids',
    })

    statistics = result if result is not None else []
    if not isinstance(statistics, list):
        raise _PublicHAError("Unexpected Recorder statistics list format")

    allow, deny = filters
    if allow or deny:
        statistics = [
            item for item in statistics
            if _statistic_filter_violation(str(item.get('statistic_id', '')), filters) is None
        ]

    if search:
        needle = str(search).strip().lower()
        statistics = [
            item for item in statistics
            if needle in str(item.get('statistic_id', '')).lower()
        ]

    _check_row_budget(len(statistics), max_rows)
    return {
        'count': len(statistics),
        'statistics': statistics,
    }


def _handle_list_statistics(args: dict, **kw) -> str:
    search = args.get('search')
    try:
        limits = _get_statistics_limits()
    except _PublicHAError as exc:
        return tool_error(str(exc))
    return _dispatch(
        _async_list_statistics(search, limits.max_rows),
        'ha_list_statistics',
        'Failed to list Home Assistant statistics',
        max_bytes=limits.max_bytes,
    )


async def _async_get_statistics(
    statistic_ids,
    start_time,
    end_time,
    period,
    types,
    max_rows=_DEFAULT_LIMITS.max_rows,
):
    message = {
        'type': 'recorder/statistics_during_period',
        'start_time': start_time,
        'statistic_ids': statistic_ids,
        'period': period,
        'types': types,
    }

    if end_time:
        message['end_time'] = end_time

    result = await _ws_command(message)
    if not isinstance(result, dict) or any(not isinstance(rows, list) for rows in result.values()):
        raise _PublicHAError("Unexpected Recorder statistics series format")
    _check_row_budget(sum(len(rows) for rows in result.values()), max_rows)
    return result


def _handle_get_statistics(args: dict, **kw) -> str:
    statistic_ids = args.get('statistic_ids')

    if not isinstance(statistic_ids, list) or not statistic_ids:
        return tool_error(
            'Missing or invalid required parameter: '
            'statistic_ids must be a non-empty list'
        )

    filters = _get_entity_filter_config()
    for statistic_id in statistic_ids:
        if (
            not isinstance(statistic_id, str)
            or not statistic_id
            or len(statistic_id) > 255
            or any(char.isspace() or ord(char) < 32 for char in statistic_id)
        ):
            return tool_error(
                f'Invalid statistic_id format: {statistic_id!r}'
            )
        violation = _statistic_filter_violation(statistic_id, filters)
        if violation:
            return tool_error(
                f'{statistic_id} is excluded by operator config ({violation}). '
                'Use ha_list_statistics to find statistics the operator permits.'
            )

    start_time = args.get('start_time')
    end_time = args.get('end_time')

    try:
        parsed_start = _parse_iso_datetime(start_time, 'start_time')
        parsed_end = (
            _parse_iso_datetime(end_time, 'end_time')
            if end_time is not None
            else None
        )
    except ValueError as exc:
        return tool_error(str(exc))

    if parsed_end is not None and parsed_end <= parsed_start:
        return tool_error('end_time must be later than start_time')

    period = args.get('period', 'day')
    if not isinstance(period, str) or period not in _ALLOWED_STAT_PERIODS:
        return tool_error(
            'Invalid period. Allowed values: '
            + ', '.join(sorted(_ALLOWED_STAT_PERIODS))
        )

    types = args.get(
        'types',
        ['mean', 'min', 'max', 'state', 'sum', 'change'],
    )

    if not isinstance(types, list) or not types:
        return tool_error('types must be a non-empty list')

    invalid_types = [
        value for value in types
        if not isinstance(value, str) or value not in _ALLOWED_STAT_TYPES
    ]

    if invalid_types:
        return tool_error(
            'Invalid statistics types: '
            + ', '.join(map(str, invalid_types))
        )

    statistic_ids = list(dict.fromkeys(statistic_ids))
    types = list(dict.fromkeys(types))
    try:
        limits = _get_statistics_limits()
        _check_query_budget(statistic_ids, parsed_start, parsed_end, period, limits)
    except _PublicHAError as exc:
        return tool_error(str(exc))

    return _dispatch(
        _async_get_statistics(
            statistic_ids,
            start_time,
            end_time,
            period,
            types,
            limits.max_rows,
        ),
        'ha_get_statistics',
        'Failed to get Home Assistant statistics',
        max_bytes=limits.max_bytes,
    )


HA_LIST_STATISTICS_SCHEMA: Dict[str, Any] = {
    "name": "ha_list_statistics",
    "description": "List Home Assistant long-term statistics available from Recorder. Use this to discover statistic IDs before querying historical data.",
    "parameters": {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": "Optional case-insensitive text used to filter statistic IDs.",
            },
        },
        "required": [],
    },
}


HA_GET_STATISTICS_SCHEMA: Dict[str, Any] = {
    "name": "ha_get_statistics",
    "description": "Get Home Assistant long-term historical statistics from Recorder for one or more statistic IDs.",
    "parameters": {
        "type": "object",
        "properties": {
            "statistic_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Statistic IDs to query (default budget: 100 unique IDs). Use ha_list_statistics to discover available IDs.",
            },
            "start_time": {
                "type": "string",
                "description": "ISO-8601 start timestamp including timezone.",
            },
            "end_time": {
                "type": "string",
                "description": "Optional ISO-8601 end timestamp including timezone.",
            },
            "period": {
                "type": "string",
                "enum": ["5minute", "hour", "day", "week", "month", "year"],
                "description": "Aggregation period. Defaults to day.",
            },
            "types": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["mean", "min", "max", "state", "sum", "change"],
                },
                "minItems": 1,
                "description": "Statistic values to return. For energy, change and sum are commonly useful.",
            },
        },
        "required": ["statistic_ids", "start_time"],
    },
}


TOOLS = ((HA_LIST_STATISTICS_SCHEMA, _handle_list_statistics),
         (HA_GET_STATISTICS_SCHEMA, _handle_get_statistics))


def register_tools(ctx) -> None:
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=handler, check_fn=_check_ha_available, emoji="📊")
