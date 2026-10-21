"""Statistics tests retained from PR #126 (goncami and teknium1)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import homeassistant_statistics.statistics_tools as ha_tool
from homeassistant_statistics.statistics_tools import (
    _handle_list_statistics, _handle_get_statistics, _ws_command,
)

class TestStatisticsHandlers:
    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_list_statistics(self, mock_ws):
        mock_ws.return_value = [
            {
                "statistic_id": "sensor.house_energy",
                "display_unit_of_measurement": "kWh",
                "has_mean": False,
                "has_sum": True,
                "source": "recorder",
                "statistics_unit_of_measurement": "kWh",
                "unit_class": "energy",
            },
            {
                "statistic_id": "sensor.temperature",
                "display_unit_of_measurement": "C",
                "has_mean": True,
                "has_sum": False,
                "source": "recorder",
                "statistics_unit_of_measurement": "C",
                "unit_class": "temperature",
            },
        ]

        result = json.loads(_handle_list_statistics({}))

        assert "error" not in result
        assert result["result"]["count"] == 2
        assert result["result"]["statistics"][0]["statistic_id"] == "sensor.house_energy"

        message = mock_ws.await_args.args[0]
        assert message["type"] == "recorder/list_statistic_ids"

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_list_statistics_search_filter(self, mock_ws):
        mock_ws.return_value = [
            {"statistic_id": "sensor.house_energy"},
            {"statistic_id": "sensor.temperature"},
        ]

        result = json.loads(_handle_list_statistics({"search": "energy"}))

        assert "error" not in result
        assert result["result"]["count"] == 1
        assert result["result"]["statistics"][0]["statistic_id"] == "sensor.house_energy"

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_get_statistics_builds_recorder_command(self, mock_ws):
        mock_ws.return_value = {
            "sensor.house_energy": [
                {
                    "start": 1782866400000,
                    "end": 1782952800000,
                    "sum": 123.45,
                    "change": 7.88,
                }
            ]
        }

        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "2026-07-01T00:00:00+02:00",
            "end_time": "2026-07-02T00:00:00+02:00",
            "period": "day",
            "types": ["sum", "change"],
        }))

        assert "error" not in result

        message = mock_ws.await_args.args[0]
        assert message == {
            "type": "recorder/statistics_during_period",
            "start_time": "2026-07-01T00:00:00+02:00",
            "end_time": "2026-07-02T00:00:00+02:00",
            "statistic_ids": ["sensor.house_energy"],
            "period": "day",
            "types": ["sum", "change"],
        }

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_get_statistics_accepts_non_entity_statistic_id(self, mock_ws):
        mock_ws.return_value = {"custom:house_energy": []}

        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["custom:house_energy"],
            "start_time": "2026-07-01T00:00:00+02:00",
        }))

        assert "error" not in result
        message = mock_ws.await_args.args[0]
        assert message["statistic_ids"] == ["custom:house_energy"]

    def test_get_statistics_requires_statistic_ids(self):
        result = json.loads(_handle_get_statistics({
            "start_time": "2026-07-01T00:00:00+02:00",
        }))

        assert "error" in result

    def test_get_statistics_requires_start_time(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
        }))

        assert "error" in result

    def test_get_statistics_rejects_invalid_period(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "2026-07-01T00:00:00+02:00",
            "period": "century",
        }))

        assert "error" in result

    def test_get_statistics_rejects_invalid_types(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "2026-07-01T00:00:00+02:00",
            "types": ["sum", "banana"],
        }))

        assert "error" in result


class TestStatisticsValidation:
    def test_get_statistics_rejects_empty_statistic_ids(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": [],
            "start_time": "2026-07-01T00:00:00+02:00",
        }))
        assert "error" in result

    def test_get_statistics_rejects_non_list_statistic_ids(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": "sensor.house_energy",
            "start_time": "2026-07-01T00:00:00+02:00",
        }))
        assert "error" in result

    def test_get_statistics_rejects_invalid_statistic_id(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["not a valid id"],
            "start_time": "2026-07-01T00:00:00+02:00",
        }))
        assert "error" in result

    def test_get_statistics_rejects_invalid_start_time(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "yesterday sometime",
        }))
        assert "error" in result

    def test_get_statistics_rejects_invalid_end_time(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "2026-07-01T00:00:00+02:00",
            "end_time": "tomorrow",
        }))
        assert "error" in result

    def test_get_statistics_rejects_end_before_start(self):
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy"],
            "start_time": "2026-07-02T00:00:00+02:00",
            "end_time": "2026-07-01T00:00:00+02:00",
        }))
        assert "error" in result


# ---------------------------------------------------------------------------
# Home Assistant WebSocket protocol
# ---------------------------------------------------------------------------


class TestWebSocketCommand:
    @pytest.mark.asyncio
    async def test_successful_auth_and_command(self, monkeypatch):
        import aiohttp

        ws = MagicMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send_json = AsyncMock()

        ws.receive_json = AsyncMock(side_effect=[
            {'type': 'auth_required'},
            {'type': 'auth_ok'},
            {
                'id': 1,
                'type': 'result',
                'success': True,
                'result': [{'statistic_id': 'sensor.house_energy'}],
            },
        ])

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.ws_connect = MagicMock(return_value=ws)

        monkeypatch.setattr(
            aiohttp,
            'ClientSession',
            MagicMock(return_value=session),
        )

        with patch(
            'homeassistant_statistics.statistics_tools._get_config',
            return_value=('https://ha.example.test', 'secret-token'),
        ):
            result = await _ws_command({
                'type': 'recorder/list_statistic_ids',
            })

        assert result == [
            {'statistic_id': 'sensor.house_energy'}
        ]

        session.ws_connect.assert_called_once_with(
            'wss://ha.example.test/api/websocket',
            max_msg_size=ha_tool._REGISTRY_MAX_MSG,
        )

        assert ws.send_json.await_args_list[0].args[0] == {
            'type': 'auth',
            'access_token': 'secret-token',
        }

        assert ws.send_json.await_args_list[1].args[0] == {
            'id': 1,
            'type': 'recorder/list_statistic_ids',
        }

    @pytest.mark.asyncio
    async def test_authentication_failure(self, monkeypatch):
        import aiohttp

        ws = MagicMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send_json = AsyncMock()
        ws.receive_json = AsyncMock(side_effect=[
            {'type': 'auth_required'},
            {
                'type': 'auth_invalid',
                'message': 'Invalid access token',
            },
        ])

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.ws_connect = MagicMock(return_value=ws)

        monkeypatch.setattr(
            aiohttp,
            'ClientSession',
            MagicMock(return_value=session),
        )

        with patch(
            'homeassistant_statistics.statistics_tools._get_config',
            return_value=('https://ha.example.test', 'bad-token'),
        ):
            with pytest.raises(
                RuntimeError,
                match='authentication failed',
            ):
                await _ws_command({
                    'type': 'recorder/list_statistic_ids',
                })

    @pytest.mark.asyncio
    async def test_home_assistant_command_error(self, monkeypatch):
        import aiohttp

        ws = MagicMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send_json = AsyncMock()
        ws.receive_json = AsyncMock(side_effect=[
            {'type': 'auth_required'},
            {'type': 'auth_ok'},
            {
                'id': 1,
                'type': 'result',
                'success': False,
                'error': {
                    'code': 'unknown_command',
                    'message': 'Unknown command',
                },
            },
        ])

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.ws_connect = MagicMock(return_value=ws)

        monkeypatch.setattr(
            aiohttp,
            'ClientSession',
            MagicMock(return_value=session),
        )

        with patch(
            'homeassistant_statistics.statistics_tools._get_config',
            return_value=('https://ha.example.test', 'secret-token'),
        ):
            with pytest.raises(
                RuntimeError,
                match='Unknown command',
            ):
                await _ws_command({
                    'type': 'recorder/list_statistic_ids',
                })


class TestStatisticsOperatorFilters:
    """The entity allow/deny lists also govern Recorder statistics (same ids as the entities)."""

    @pytest.fixture
    def filters(self, monkeypatch):
        def _set(allow="", deny=""):
            monkeypatch.setattr(ha_tool, "_HASS_ENTITY_ALLOWLIST", allow)
            monkeypatch.setattr(ha_tool, "_HASS_ENTITY_DENYLIST", deny)
        return _set

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_list_hides_denied_and_unlisted_statistics(self, mock_ws, filters):
        filters(allow="sensor.*", deny="sensor.secret*")
        mock_ws.return_value = [
            {"statistic_id": "sensor.house_energy"},
            {"statistic_id": "sensor.secret_meter"},
            {"statistic_id": "lock.front_door"},
        ]
        result = json.loads(_handle_list_statistics({}))
        assert [s["statistic_id"] for s in result["result"]["statistics"]] == ["sensor.house_energy"]

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_get_refuses_a_denied_statistic_before_any_request(self, mock_ws, filters):
        filters(deny="sensor.secret*")
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.house_energy", "sensor.secret_meter"],
            "start_time": "2026-10-01T00:00:00+00:00",
        }))
        assert "HASS_ENTITY_DENYLIST" in result["error"]
        mock_ws.assert_not_awaited()

    @patch("homeassistant_statistics.statistics_tools._ws_command", new_callable=AsyncMock)
    def test_get_refuses_a_statistic_outside_the_allowlist(self, mock_ws, filters):
        filters(allow="sensor.house_*")
        result = json.loads(_handle_get_statistics({
            "statistic_ids": ["sensor.garage_energy"],
            "start_time": "2026-10-01T00:00:00+00:00",
        }))
        assert "HASS_ENTITY_ALLOWLIST" in result["error"]
        mock_ws.assert_not_awaited()
