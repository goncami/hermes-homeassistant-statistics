"""Authentication stays on the configured endpoint; error text stays private."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from agent.secret_scope import set_secret_scope, reset_secret_scope
from homeassistant_statistics import statistics_tools as tools


@asynccontextmanager
async def configured_server(app):
    async with TestServer(app) as server:
        scope = set_secret_scope({
            "HASS_URL": str(server.make_url("")).rstrip("/"),
            "HASS_TOKEN": "synthetic-test-token",
        })
        try:
            yield server
        finally:
            reset_secret_scope(scope)


def recorder_endpoint(auth_received, *, command_error=None):
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth_received.append(await ws.receive_json())
        await ws.send_json({"type": "auth_ok"})
        command = await ws.receive_json()
        await ws.send_json({
            "id": command["id"], "type": "result", "success": command_error is None,
            "result": [], "error": command_error,
        })
        await ws.close()
        return ws
    return endpoint


@pytest.mark.asyncio
async def test_direct_endpoint_authenticates():
    auth = []
    app = web.Application()
    app.router.add_get("/api/websocket", recorder_endpoint(auth))
    async with configured_server(app):
        assert await tools._ws_command({"type": "recorder/list_statistic_ids"}) == []
    assert auth == [{"type": "auth", "access_token": "synthetic-test-token"}]


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_rejected_before_target_connection():
    auth, requests = [], []
    target = web.Application()
    endpoint = recorder_endpoint(auth)
    async def target_endpoint(request):
        requests.append(True)
        return await endpoint(request)
    target.router.add_get("/api/websocket", target_endpoint)
    async with TestServer(target) as destination:
        source = web.Application()
        async def redirect(request):
            raise web.HTTPFound(str(destination.make_url("/api/websocket")))
        source.router.add_get("/api/websocket", redirect)
        async with configured_server(source):
            with pytest.raises(RuntimeError, match="redirect"):
                await tools._ws_command({"type": "recorder/list_statistic_ids"})
    assert requests == []
    assert auth == []


@pytest.mark.asyncio
async def test_same_origin_redirect_requires_configuring_final_url():
    auth = []
    app = web.Application()
    async def redirect(request):
        raise web.HTTPFound("/final")
    app.router.add_get("/api/websocket", redirect)
    app.router.add_get("/final", recorder_endpoint(auth))
    async with configured_server(app):
        with pytest.raises(RuntimeError, match="redirect"):
            await tools._ws_command({"type": "recorder/list_statistic_ids"})
    assert auth == []


@pytest.mark.asyncio
async def test_https_downgrade_redirect_is_rejected_before_target_connection():
    import ssl
    import trustme
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(server_tls)
    client_tls = ssl.create_default_context()
    ca.configure_trust(client_tls)
    hits, auth = [], []
    target = web.Application()
    endpoint = recorder_endpoint(auth)
    async def target_endpoint(request):
        hits.append(True)
        return await endpoint(request)
    target.router.add_get("/api/websocket", target_endpoint)
    async with TestServer(target) as destination:
        app = web.Application()
        async def redirect(request):
            raise web.HTTPFound(str(destination.make_url("/api/websocket")))
        app.router.add_get("/api/websocket", redirect)
        server = TestServer(app, scheme="https")
        await server.start_server(ssl=server_tls)
        scope = set_secret_scope({"HASS_URL": str(server.make_url("")).rstrip("/"), "HASS_TOKEN": "synthetic-test-token"})
        real_session = aiohttp.ClientSession
        def session(*args, **kwargs):
            return real_session(*args, connector=aiohttp.TCPConnector(ssl=client_tls), **kwargs)
        try:
            with patch.object(aiohttp, "ClientSession", side_effect=session):
                with pytest.raises(RuntimeError, match="redirect"):
                    await tools._ws_command({"type": "recorder/list_statistic_ids"})
        finally:
            reset_secret_scope(scope)
            await server.close()
    assert hits == []
    assert auth == []


@pytest.mark.parametrize("url", [
    "ha.example.test/?key=synthetic-url-secret",
    "ftp://ha.example.test",
    "https://",
    "https://operator:synthetic-url-secret@ha.example.test",
    "https://ha.example.test/?key=synthetic-url-secret",
    "https://ha.example.test/#synthetic-url-secret",
    "https://ha.example.test:invalid",
    "https://ha.example.test:99999",
    "https://ha.example.test/\nsecret",
])
def test_invalid_base_url_fails_before_network_without_disclosure(url, caplog):
    scope = set_secret_scope({"HASS_URL": url, "HASS_TOKEN": "synthetic-test-token"})
    try:
        with patch.object(aiohttp, "ClientSession", side_effect=AssertionError("network must not start")) as client, caplog.at_level(logging.ERROR):
            result = json.loads(tools._handle_list_statistics({}))
        assert "error" in result
        client.assert_not_called()
        assert "synthetic-url-secret" not in json.dumps(result) + caplog.text
        assert "ha.example.test" not in json.dumps(result) + caplog.text
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("url,expected", [
    ("http://homeassistant.local:8123", "http://homeassistant.local:8123"),
    ("https://ha.example.test/ha/", "https://ha.example.test/ha"),
    ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
    ("http://[::1]:8123/ha", "http://[::1]:8123/ha"),
])
def test_valid_base_urls_preserve_private_hosts_and_proxy_paths(url, expected):
    scope = set_secret_scope({"HASS_URL": url, "HASS_TOKEN": "synthetic-test-token"})
    try:
        assert tools._get_config() == (expected, "synthetic-test-token")
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("token", ["", "   "])
def test_missing_token_fails_before_network(token):
    scope = set_secret_scope({"HASS_URL": "https://ha.example.test", "HASS_TOKEN": token})
    try:
        with patch.object(aiohttp, "ClientSession", side_effect=AssertionError("network must not start")) as client:
            result = json.loads(tools._handle_list_statistics({}))
        assert "error" in result
        client.assert_not_called()
        assert not tools._check_ha_available()
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("exception,category", [
    (aiohttp.ClientConnectionError("synthetic-sensitive-detail"), "connect"),
    (TimeoutError("synthetic-sensitive-detail"), "timed out"),
    (RuntimeError("synthetic-sensitive-detail"), "failed"),
])
def test_transport_errors_are_categorized_without_raw_exception(exception, category, caplog):
    async def failing():
        raise exception
    with caplog.at_level(logging.ERROR):
        result = json.loads(tools._dispatch(failing(), "ha_list_statistics", "Failed to list statistics"))
    assert category in result["error"].lower()
    assert "synthetic-sensitive-detail" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_remote_command_error_never_enters_tool_result_or_logs(caplog):
    auth = []
    app = web.Application()
    app.router.add_get("/api/websocket", recorder_endpoint(auth, command_error={
        "code": "unknown_command",
        "message": "synthetic-test-token https://private.example.test/?key=synthetic-sensitive-detail",
    }))
    async with configured_server(app), asyncio.timeout(3):
        with caplog.at_level(logging.ERROR):
            result = json.loads(await asyncio.to_thread(tools._handle_list_statistics, {}))
    assert "Unknown command" in result["error"]
    for value in ("synthetic-test-token", "private.example.test", "synthetic-sensitive-detail"):
        assert value not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_unneeded_response_cookies_are_not_stored():
    auth, clients = [], []
    async def cookie_endpoint(request):
        # A real hostname makes aiohttp's normal CookieJar accept the response cookie.
        response = web.WebSocketResponse()
        response.set_cookie("unused", "synthetic-cookie")
        await response.prepare(request)
        await response.send_json({"type": "auth_required"})
        auth.append(await response.receive_json())
        await response.send_json({"type": "auth_ok"})
        command = await response.receive_json()
        await response.send_json({"id": command["id"], "type": "result", "success": True, "result": []})
        await response.close()
        return response
    app = web.Application()
    app.router.add_get("/api/websocket", cookie_endpoint)
    real_session = aiohttp.ClientSession
    def session(*args, **kwargs):
        client = real_session(*args, **kwargs)
        clients.append(client)
        return client
    async with TestServer(app) as server:
        scope = set_secret_scope({
            "HASS_URL": str(server.make_url("").with_host("localhost")).rstrip("/"),
            "HASS_TOKEN": "synthetic-test-token",
        })
        try:
            with patch.object(aiohttp, "ClientSession", side_effect=session):
                assert await tools._ws_command({"type": "recorder/list_statistic_ids"}) == []
        finally:
            reset_secret_scope(scope)
    assert len(auth) == 1
    assert len(clients[0].cookie_jar) == 0
