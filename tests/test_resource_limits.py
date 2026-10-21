"""Resource budgets reject excessive requests and never return partial histories."""
import json
from unittest.mock import AsyncMock, patch

from agent.secret_scope import set_secret_scope, reset_secret_scope
import pytest
from homeassistant_statistics import statistics_tools as tools

BASE = {
    "statistic_ids": ["sensor.energy"],
    "start_time": "2026-01-01T00:00:00Z",
    "end_time": "2026-01-02T00:00:00Z",
    "period": "day",
}


def test_too_many_unique_ids_fail_before_network():
    with patch.object(tools, "_ws_command", new_callable=AsyncMock) as ws:
        result = json.loads(tools._handle_get_statistics(BASE | {
            "statistic_ids": [f"sensor.example_{i}" for i in range(101)],
        }))
        assert "error" in result
        assert "100" in result["error"]
        ws.assert_not_awaited()


def test_default_id_boundary_and_long_coarse_query_are_allowed():
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value={}) as ws:
        result = json.loads(tools._handle_get_statistics(BASE | {
            "statistic_ids": [f"sensor.example_{i}" for i in range(100)],
        }))
        assert "result" in result
        result = json.loads(tools._handle_get_statistics(BASE | {
            "start_time": "2016-01-01T00:00:00Z", "period": "month",
        }))
        assert "result" in result
        assert ws.await_count == 2


def test_duplicate_ids_and_types_are_sent_once_in_original_order():
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value={}) as ws:
        result = json.loads(tools._handle_get_statistics(BASE | {
            "statistic_ids": ["sensor.energy", "custom:energy", "sensor.energy"],
            "types": ["sum", "change", "sum"],
        }))
        assert "result" in result
        assert ws.await_args.args[0]["statistic_ids"] == ["sensor.energy", "custom:energy"]
        assert ws.await_args.args[0]["types"] == ["sum", "change"]


def test_fine_resolution_range_fails_before_network():
    with patch.object(tools, "_ws_command", new_callable=AsyncMock) as ws:
        result = json.loads(tools._handle_get_statistics(BASE | {
            "end_time": "2027-01-01T00:00:00Z", "period": "5minute",
        }))
        assert "error" in result
        assert "period" in result["error"].lower()
        ws.assert_not_awaited()


def test_actual_row_cap_checks_all_series_without_partial_data():
    response = {"sensor.energy": [{"sum": 1}] * 5000, "custom:energy": [{"sum": 2}] * 5001}
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=response) as ws:
        result = json.loads(tools._handle_get_statistics(BASE | {"statistic_ids": list(response)}))
        assert set(result) == {"error"}
        ws.assert_awaited_once()


def test_discovery_cap_applies_after_search_and_never_silently_truncates():
    response = [{"statistic_id": f"sensor.example_{i}"} for i in range(10001)]
    response.append({"statistic_id": "custom:unique_energy"})
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=response):
        assert "error" in json.loads(tools._handle_list_statistics({}))
        result = json.loads(tools._handle_list_statistics({"search": "unique_energy"}))
        assert result["result"] == {"count": 1, "statistics": [response[-1]]}


def test_oversized_json_result_is_refused_without_returning_payload():
    response = {"sensor.energy": [{"extra": "synthetic-large-data" * 70000}]}
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=response):
        result = tools._handle_get_statistics(BASE)
        assert set(json.loads(result)) == {"error"}
        assert "synthetic-large-data" not in result


@pytest.mark.parametrize("delta,permitted", [(0, True), (-1, False)])
def test_output_byte_boundary(delta, permitted):
    payload = [{"statistic_id": "custom:café"}]
    expected = json.dumps({"result": {"count": 1, "statistics": payload}})
    limit = len(expected.encode("utf-8")) + delta
    scope = set_secret_scope({"HASS_STATISTICS_MAX_BYTES": str(limit)})
    try:
        with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=payload):
            result = tools._handle_list_statistics({})
        assert ("result" in json.loads(result)) is permitted
        if permitted:
            assert len(result.encode("utf-8")) == limit
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number", "1.5"])
def test_invalid_budget_fails_before_network(value):
    scope = set_secret_scope({"HASS_STATISTICS_MAX_ROWS": value})
    try:
        with patch.object(tools, "_ws_command", new_callable=AsyncMock) as ws:
            assert "error" in json.loads(tools._handle_list_statistics({}))
            ws.assert_not_awaited()
    finally:
        reset_secret_scope(scope)


def test_operator_budgets_follow_the_active_profile():
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value={}) as ws:
        scope = set_secret_scope({"HASS_STATISTICS_MAX_IDS": "1"})
        try:
            assert "error" in json.loads(tools._handle_get_statistics(BASE | {
                "statistic_ids": ["sensor.energy", "custom:energy"],
            }))
            ws.assert_not_awaited()
        finally:
            reset_secret_scope(scope)
        assert "result" in json.loads(tools._handle_get_statistics(BASE | {
            "statistic_ids": ["sensor.energy", "custom:energy"],
        }))


@pytest.mark.parametrize("response", [[], {"sensor.energy": "not-rows"}])
def test_invalid_series_shape_returns_a_fixed_error(response):
    with patch.object(tools, "_ws_command", new_callable=AsyncMock, return_value=response):
        assert "error" in json.loads(tools._handle_get_statistics(BASE))
