"""Read-only Recorder statistics. Derived from official-plugins PR #126; see NOTICE.md."""
import asyncio
import fnmatch
import json
import logging
from typing import Any, Dict, List, Optional
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)
TOOLSET = "homeassistant-statistics"


def tool_error(message: str) -> str:
    """Tool error envelope (same JSON shape as core's ``tools.registry.tool_error``)."""
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def url_scheme_problem(url: str) -> Optional[str]:
    """Why ``url`` can't be the Home Assistant base URL, or None when it can.

    Without a scheme every request URL becomes ``host/api/...``, which aiohttp rejects with only that
    string as the reason, so the misconfiguration would surface as an endless series of opaque errors.
    """
    if url.startswith(("http://", "https://")):
        return None
    return (
        f"Home Assistant URL {url!r} has no http:// or https:// scheme. Set HASS_URL "
        "to the full URL, e.g. https://ha.example.com or "
        "http://homeassistant.local:8123")


_HASS_ENTITY_ALLOWLIST: str = ""


_HASS_ENTITY_DENYLIST: str = ""


def _get_config():
    """Return the active profile's Home Assistant URL and token; raise ValueError on a URL without a scheme."""
    hass_url = (get_secret("HASS_URL", "http://homeassistant.local:8123") or "").rstrip("/")
    if problem := url_scheme_problem(hass_url):
        raise ValueError(problem)
    return hass_url, (get_secret("HASS_TOKEN", "") or "").strip()


def _get_entity_filter_config():
    """Return active-profile allow/deny patterns; deny is the final veto."""
    allow = _HASS_ENTITY_ALLOWLIST or (get_secret("HASS_ENTITY_ALLOWLIST", "") or "")
    deny = _HASS_ENTITY_DENYLIST or (get_secret("HASS_ENTITY_DENYLIST", "") or "")
    return _parse_entity_filter_patterns(allow), _parse_entity_filter_patterns(deny)


def _parse_entity_filter_patterns(value: str) -> List[str]:
    """Split a comma-separated env value into trimmed, non-empty patterns."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _entity_matches(entity_id: str, patterns: List[str]) -> bool:
    """Return True if ``entity_id`` matches any operator pattern.

    Matching is deliberately forgiving so an operator can write
    ``office_thermostat_*`` (the way the device family is usually
    abbreviated) and have it cover ``office_thermostat.0_external_temperature``
    (a real HA entity id whose separator after the device name is a dot):

    - exact match
    - prefix match, treating ``.`` and ``_`` as equivalent at the boundary
      (``office_thermostat.0_x`` matches ``office_thermostat_*``)
    - shell-glob match (``*.valve*``, ``climate.office`` etc.)
    """
    for pat in patterns:
        if not pat:
            continue
        if entity_id == pat or fnmatch.fnmatch(entity_id, pat):
            return True
        # Family-prefix match. A pattern that ends in ``*`` or a separator
        # (``office_thermostat_*``, ``office_thermostat.``) is a family
        # prefix: it should cover ``office_thermostat.0_x`` by treating the
        # separator after the matched base (``.`` or ``_``) as
        # interchangeable, or the id to end exactly at the base. A bare id
        # like ``climate.office`` without a trailing separator is only an
        # exact/glob match, so it does NOT swallow ``climate.office_zombie``.
        if pat[-1] in "*._":
            base = pat.rstrip("*._")
            if base and entity_id.startswith(base) and (
                len(entity_id) == len(base) or entity_id[len(base)] in "._"
            ):
                return True
    return False


def _operator_filter_violation(entity_id: str) -> Optional[str]:
    """Return why the active operator filters exclude an ID, or None."""
    allow, deny = _get_entity_filter_config()
    if deny and _entity_matches(entity_id, deny):
        return "excluded by HASS_ENTITY_DENYLIST"
    if allow and not _entity_matches(entity_id, allow):
        return "not in HASS_ENTITY_ALLOWLIST"
    return None


_REGISTRY_MAX_MSG = 64 * 1024 * 1024


def _run_async(coro):
    """Run a coroutine from a sync handler; hops to a thread if a loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():  # already inside a loop: asyncio.run() needs its own thread
        import concurrent.futures
        import contextvars
        # Carry the caller's Context: the coroutine reads HASS_* via get_secret, which is profile-scoped.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


def _dispatch(coro, log_name: str, fail_msg: str) -> str:
    """Run ``coro`` and wrap as ``{"result": ...}``; on error log and return tool_error."""
    try:
        return json.dumps({"result": _run_async(coro)})
    except Exception as e:
        logger.error("%s error: %s", log_name, e)
        return tool_error(f"{fail_msg}: {e}")


def _check_ha_available() -> bool:
    """Tool is only available when HASS_TOKEN is set."""
    return bool(get_secret("HASS_TOKEN"))


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

    async with asyncio.timeout(timeout):
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            # recorder/list_statistic_ids on a big install exceeds aiohttp's 4 MiB default frame.
            async with session.ws_connect(ws_url, max_msg_size=_REGISTRY_MAX_MSG) as ws:
                hello = await ws.receive_json()
                if hello.get('type') != 'auth_required':
                    raise RuntimeError(
                        'Unexpected Home Assistant WebSocket greeting: '
                        + str(hello.get('type'))
                    )
    
                await ws.send_json({
                    'type': 'auth',
                    'access_token': token,
                })
    
                auth = await ws.receive_json()
                if auth.get('type') != 'auth_ok':
                    raise RuntimeError(
                        'Home Assistant WebSocket authentication failed: '
                        + str(auth.get('type'))
                    )
    
                command = dict(message)
                command['id'] = 1
                await ws.send_json(command)
    
                response = await ws.receive_json()
    
                if response.get('id') != 1:
                    raise RuntimeError(
                        'Unexpected Home Assistant WebSocket response id'
                    )
    
                if response.get('type') != 'result':
                    raise RuntimeError(
                        'Unexpected Home Assistant WebSocket response: '
                        + str(response.get('type'))
                    )
    
                if not response.get('success'):
                    error = response.get('error') or {}
                    detail = (
                        error.get('message')
                        or error.get('code')
                        or 'unknown error'
                    )
                    raise RuntimeError(
                        'Home Assistant WebSocket command failed: ' + detail
                    )
    
                return response.get('result')


def _statistic_filter_violation(statistic_id: str) -> Optional[str]:
    """Why the operator allow/deny lists hide this statistic, or ``None``.

    Recorder statistics of an entity use the entity id (``sensor.house_energy``), so they get the
    same check as ha_get_state. External statistics (``domain:name``) are matched as plain ids:
    the denylist can veto them, and with an allowlist they must be listed explicitly.
    """
    return _operator_filter_violation(statistic_id.lower())


async def _async_list_statistics(search=None):
    result = await _ws_command({
        'type': 'recorder/list_statistic_ids',
    })

    statistics = result or []

    # Operator allow/deny lists govern statistics too: an entity's long-term statistics share its id.
    allow, deny = _get_entity_filter_config()
    if allow or deny:
        statistics = [
            item for item in statistics
            if _statistic_filter_violation(str(item.get('statistic_id', ''))) is None
        ]

    if search:
        needle = str(search).strip().lower()
        statistics = [
            item for item in statistics
            if needle in str(item.get('statistic_id', '')).lower()
        ]

    return {
        'count': len(statistics),
        'statistics': statistics,
    }


def _handle_list_statistics(args: dict, **kw) -> str:
    search = args.get('search')
    return _dispatch(
        _async_list_statistics(search),
        'ha_list_statistics',
        'Failed to list Home Assistant statistics',
    )


async def _async_get_statistics(
    statistic_ids,
    start_time,
    end_time,
    period,
    types,
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

    return await _ws_command(message)


def _handle_get_statistics(args: dict, **kw) -> str:
    statistic_ids = args.get('statistic_ids')

    if not isinstance(statistic_ids, list) or not statistic_ids:
        return tool_error(
            'Missing or invalid required parameter: '
            'statistic_ids must be a non-empty list'
        )

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
        violation = _statistic_filter_violation(statistic_id)
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

    return _dispatch(
        _async_get_statistics(
            statistic_ids,
            start_time,
            end_time,
            period,
            types,
        ),
        'ha_get_statistics',
        'Failed to get Home Assistant statistics',
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
                "description": "Statistic IDs to query. Use ha_list_statistics to discover available IDs.",
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
    """Register exactly the two statistics tools through the public API."""
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=handler, check_fn=_check_ha_available, emoji="📊")
