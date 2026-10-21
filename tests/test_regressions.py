"""Contracts for standalone registration, profile isolation and malformed calls."""
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from agent.secret_scope import set_secret_scope, reset_secret_scope
from homeassistant_statistics import register
from homeassistant_statistics import statistics_tools as tools

BASE = {"statistic_ids": ["sensor.energy"], "start_time": "2026-01-01T00:00:00Z"}

@pytest.mark.parametrize("extra", [
    {"period": []}, {"period": {}}, {"types": [{}]}, {"types": [[]]},
    {"types": []}, {"types": "sum"},
    {"start_time": "2026-01-01T00:00:00"},
    {"end_time": "2026-01-01T00:00:00Z"},
    {"statistic_ids": [None]}, {"statistic_ids": ["x" * 256]},
    {"statistic_ids": ["sensor.\x00energy"]},
])
def test_bad_inputs_fail_before_network(extra):
    with patch.object(tools, "_ws_command", new_callable=AsyncMock) as ws:
        assert "error" in json.loads(tools._handle_get_statistics(BASE | extra))
        ws.assert_not_awaited()


def test_registration_matches_manifest():
    import yaml
    from pathlib import Path
    ctx = MagicMock()
    register(ctx)
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())
    calls = ctx.register_tool.call_args_list
    assert {c.kwargs["name"] for c in calls} == set(manifest["provides_tools"])
    assert len(calls) == 2
    assert all(c.kwargs["toolset"] == "homeassistant-statistics" for c in calls)
    assert all(c.kwargs["check_fn"] is tools._check_ha_available for c in calls)


@pytest.mark.asyncio
async def test_active_profile_propagates_into_sync_handler_thread():
    async def command(message):
        assert tools._get_config() == ("https://ha.example.test", "test-only")
        return []
    token = set_secret_scope({"HASS_URL": "https://ha.example.test", "HASS_TOKEN": "test-only"})
    try:
        with patch.object(tools, "_ws_command", side_effect=command):
            assert json.loads(tools._handle_list_statistics({}))["result"]["count"] == 0
    finally:
        reset_secret_scope(token)
    assert not tools._check_ha_available()


@pytest.mark.parametrize("allow,deny,identifier,blocked", [
    ("custom:*", "", "custom:energy", False),
    ("sensor.*", "", "custom:energy", True),
    ("custom:*", "custom:energy", "custom:energy", True),
    ("sensor.office_*", "", "sensor.office_energy", False),
])
def test_external_statistics_and_deny_precedence(allow, deny, identifier, blocked):
    token = set_secret_scope({"HASS_ENTITY_ALLOWLIST": allow, "HASS_ENTITY_DENYLIST": deny})
    try:
        assert bool(tools._statistic_filter_violation(identifier)) == blocked
    finally:
        reset_secret_scope(token)


@pytest.mark.asyncio
async def test_websocket_receive_has_total_deadline(monkeypatch):
    import aiohttp
    ws = MagicMock()
    ws.__aenter__ = AsyncMock(return_value=ws)
    ws.__aexit__ = AsyncMock(return_value=False)
    async def stalled():
        await asyncio.sleep(60)
    ws.receive_json = stalled
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.ws_connect.return_value = ws
    monkeypatch.setattr(aiohttp, "ClientSession", MagicMock(return_value=session))
    with patch.object(tools, "_get_config", return_value=("https://ha.example.test", "test-only")):
        with pytest.raises(TimeoutError):
            await tools._ws_command({"type": "recorder/list_statistic_ids"}, timeout=0.01)
    assert tools._REGISTRY_MAX_MSG == 64 * 1024 * 1024


def test_discovery_and_query_use_same_profile_filters():
    token = set_secret_scope({"HASS_ENTITY_ALLOWLIST": "sensor.*", "HASS_ENTITY_DENYLIST": "sensor.secret*"})
    try:
        with patch.object(tools, "_ws_command", new_callable=AsyncMock) as ws:
            ws.return_value = [{"statistic_id": "sensor.energy"}, {"statistic_id": "sensor.secret_meter"}]
            rows = json.loads(tools._handle_list_statistics({}))["result"]["statistics"]
            assert rows == [{"statistic_id": "sensor.energy"}]
            ws.reset_mock()
            assert "error" in json.loads(tools._handle_get_statistics(BASE | {"statistic_ids": ["sensor.secret_meter"]}))
            ws.assert_not_awaited()
    finally:
        reset_secret_scope(token)
