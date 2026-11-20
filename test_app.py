"""Tests for Ride Ready, run through LocalRunner with the network stubbed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner

from app import App, CannotReadTime

FIXED_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # a Friday

FORECAST = json.dumps({
    "hourly": {
        "time": ["2026-11-21T02:00", "2026-11-24T14:00"],
        "precipitation_probability": [10, 20],
    }
}).encode()


def make_app(fetch=None):
    app = App()
    app._fetch = fetch or (lambda url, timeout=4.0: FORECAST)
    return app


def failing_fetch(url, timeout=4.0):
    raise TimeoutError("no answer")


def run(inputs, app=None, explicit=False):
    return LocalRunner(app or make_app()).call(
        "ride.availability", inputs, granted_secrets={}, now=FIXED_NOW,
        request_id="t1", explicit=explicit,
    ).result


def test_manifest_is_valid():
    assert App().manifest.app_id == "hack.app_cd1d_3"


def test_saturday_2am_is_high_with_night_tube():
    result = run({"when": "2026-11-21T02:00"})
    assert result.data["day"] == "Saturday"
    assert result.data["demand_level"] == "high"
    assert result.data["surge_likely"] is True
    assert result.data["night_tube_running"] is True
    assert result.data["tube_running"] is False
    assert result.data["rain_chance_percent"] == 10
    assert "estimate" in result.narrative


def test_tuesday_afternoon_is_normal():
    result = run({"when": "2026-11-24T14:00", "place": "Camden"})
    assert result.data["day"] == "Tuesday"
    assert result.data["demand_level"] == "normal"
    assert result.data["surge_likely"] is False
    assert result.data["night_tube_running"] is False
    assert result.data["tube_running"] is True
    assert result.data["apps_to_try"] == ["Uber", "Bolt", "FREENOW", "Addison Lee"]


def test_rain_is_left_out_when_forecast_fails():
    result = run({"when": "2026-11-24T14:00"}, app=make_app(failing_fetch))
    assert "rain_chance_percent" not in result.data


def test_empty_when_uses_now():
    result = run({"when": ""}, app=make_app(failing_fetch))
    assert result.data["day"] == "Friday"


def test_unreadable_time_is_refused():
    with pytest.raises(CannotReadTime) as failure:
        run({"when": "sometime-ish"})
    assert "Saturday 11pm" in failure.value.user_message


def test_picked_by_name_answers_the_same():
    picked = run({"when": "2026-11-21T02:00"}, explicit=True)
    assert picked.data == run({"when": "2026-11-21T02:00"}).data
