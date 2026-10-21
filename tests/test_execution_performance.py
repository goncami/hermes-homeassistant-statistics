"""Filter consistency, WebSocket cleanup and Hermes worker dispatch."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from agent.secret_scope import set_secret_scope, reset_secret_scope
from tools.registry import ToolRegistry
from homeassistant_statistics import register
from homeassistant_statistics import statistics_tools as tools

BASE = {"statistic_ids": ["sensor.a", "sensor.b"],
        "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-02T00:00:00Z"}


def changing_filters(first, later):
    state = iter([first])
    return lambda: next(state, later)


def test_discovery_uses_one_consistent_filter_snapshot_per_call(monkeypatch):
    monkeypatch.setattr(tools, "_get_entity_filter_config", changing_filters(
        (["sensor.a"], []), (["sensor.b"], [])))
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=[
        {"statistic_id": "sensor.a"}, {"statistic_id": "sensor.b"},
    ]):
        first = json.loads(tools._handle_list_statistics({}))
        second = json.loads(tools._handle_list_statistics({}))
    assert first["result"]["statistics"] == [{"statistic_id": "sensor.a"}]
    assert second["result"]["statistics"] == [{"statistic_id": "sensor.b"}]


def test_query_uses_one_snapshot_but_observes_changes_on_next_call(monkeypatch):
    monkeypatch.setattr(tools, "_get_entity_filter_config", changing_filters(
        (["sensor.a", "sensor.b"], []), (["sensor.a"], [])))
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value={}) as ws:
        assert "result" in json.loads(tools._handle_get_statistics(BASE))
        assert "error" in json.loads(tools._handle_get_statistics(BASE))
        ws.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_result_does_not_wait_for_an_unresponsive_close():
    release = asyncio.Event()
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        command = await ws.receive_json()
        await ws.send_json({"id": command["id"], "type": "result", "success": True, "result": []})
        await release.wait()  # deliberately do not read or acknowledge the client's close
        await ws.close()
        return ws
    app = web.Application()
    app.router.add_get("/api/websocket", endpoint)
    async with TestServer(app) as server:
        scope = set_secret_scope({"HASS_URL": str(server.make_url("")).rstrip("/"),
                                  "HASS_TOKEN": "synthetic-test-token"})
        try:
            result = await asyncio.wait_for(tools._ws_command(
                {"type": "recorder/list_statistic_ids"}, timeout=3), timeout=2)
            assert result == []
        finally:
            release.set()
            reset_secret_scope(scope)


@pytest.mark.asyncio
async def test_cancelling_websocket_releases_client_session():
    started, release = asyncio.Event(), asyncio.Event()
    clients = []
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        await ws.receive_json()
        started.set()
        await release.wait()
        await ws.close()
        return ws
    app = web.Application()
    app.router.add_get("/api/websocket", endpoint)
    original_session = aiohttp.ClientSession
    def session(*args, **kwargs):
        client = original_session(*args, **kwargs)
        clients.append(client)
        return client
    async with TestServer(app) as server:
        scope = set_secret_scope({"HASS_URL": str(server.make_url("")).rstrip("/"),
                                  "HASS_TOKEN": "synthetic-test-token"})
        task = None
        try:
            with patch.object(aiohttp, "ClientSession", side_effect=session):
                task = asyncio.create_task(tools._ws_command({"type": "recorder/list_statistic_ids"}))
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            assert len(clients) == 1 and clients[0].closed
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            reset_secret_scope(scope)


@pytest.mark.asyncio
async def test_real_hermes_registry_worker_preserves_profile_without_freezing_loop():
    registry = ToolRegistry()
    register(SimpleNamespace(register_tool=registry.register))
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    import threading
    worker_release = threading.Event()
    async def command(message):
        assert tools._get_config() == ("https://ha.example.test", "synthetic-profile-token")
        loop.call_soon_threadsafe(started.set)
        await asyncio.to_thread(worker_release.wait)
        return []
    scope = set_secret_scope({"HASS_URL": "https://ha.example.test", "HASS_TOKEN": "synthetic-profile-token"})
    try:
        with patch.object(tools, "_ws_command", side_effect=command):
            task = asyncio.create_task(asyncio.to_thread(registry.dispatch, "ha_list_statistics", {}))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                # The caller loop has resumed while the worker is still waiting.
                assert not task.done()
            finally:
                worker_release.set()
            assert json.loads(await asyncio.wait_for(task, timeout=2))["result"]["count"] == 0
    finally:
        worker_release.set()
        reset_secret_scope(scope)


@pytest.mark.asyncio
async def test_real_registry_timeout_returns_error_and_closes_session():
    registry = ToolRegistry()
    register(SimpleNamespace(register_tool=registry.register))
    clients = []
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        await ws.receive_json()
        await ws.receive()  # wait for disconnect instead of returning a result
        return ws
    original_session = aiohttp.ClientSession
    def session(*args, **kwargs):
        client = original_session(*args, **kwargs)
        clients.append(client)
        return client
    original_command = tools._ws_command
    async def short_command(message):
        return await original_command(message, timeout=0.03)
    app = web.Application()
    app.router.add_get("/api/websocket", endpoint)
    async with TestServer(app) as server:
        scope = set_secret_scope({"HASS_URL": str(server.make_url("")).rstrip("/"),
                                  "HASS_TOKEN": "synthetic-test-token"})
        try:
            with patch.object(aiohttp, "ClientSession", side_effect=session), \
                 patch.object(tools, "_ws_command", side_effect=short_command):
                response = await asyncio.wait_for(asyncio.to_thread(
                    registry.dispatch, "ha_list_statistics", {}), timeout=2)
            assert "timed out" in json.loads(response)["error"]
            assert len(clients) == 1 and clients[0].closed
        finally:
            reset_secret_scope(scope)
