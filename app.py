"""Ride Radar: an honest estimate of ride-hail availability in London.

Uber, Bolt and the rest publish no free availability API, so this app
estimates from time-of-week patterns, UK bank holidays and the Open-Meteo
rain forecast (best-effort, no key). It never claims live driver data.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
LONDON = ZoneInfo("Europe/London")
USER_AGENT = "pippa-agent-app-ride-radar/0.1 (https://github.com/Pippa-Labs)"
FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=51.5074&longitude=-0.1278"
    "&hourly=precipitation_probability&timezone=Europe/London&forecast_days=7"
)

LEVELS = ["low", "normal", "busy", "high", "very high"]
WAITS = {
    "low": "about 5-12 minutes (fewer drivers about)",
    "normal": "about 2-5 minutes",
    "busy": "about 4-8 minutes",
    "high": "about 8-15 minutes, with surge pricing",
    "very high": "15 minutes or more, with heavy surge pricing",
}
APPS_TO_TRY = ["Uber", "Bolt", "FREENOW", "Addison Lee"]

# England and Wales bank holidays, 2026-2027.
BANK_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4),
    date(2026, 5, 25), date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3),
    date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
}

NIGHT_TUBE_LINES = "Central, Jubilee, Northern, Piccadilly and Victoria lines"


class CannotReadTime(AgentAppError):
    user_message = (
        "I could not work out the date and time. Try something like "
        "\"Saturday 11pm\" or \"21 November at 23:30\"."
    )
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def _fetch(self, url: str, timeout: float = 4.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "ride.availability":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        text = (call.inputs.get("when") or "").strip()
        place = (call.inputs.get("place") or "").strip()
        moment = self._parse(text) if text else call.context.now.astimezone(LONDON)

        level, reasons = self._demand(moment)
        night_tube = self._night_tube(moment)
        tube = self._tube(moment)

        rain = self._rain(moment)
        if rain is not None and rain >= 50:
            reasons.append(f"Rain is likely ({rain}%), which pushes demand up.")
            level = LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]

        if night_tube:
            reasons.append(
                f"The Night Tube runs on the {NIGHT_TUBE_LINES}, and the "
                "Overground between Highbury & Islington and New Cross Gate."
            )
        elif not tube:
            reasons.append("The Tube is closed then, so night buses are the fallback.")
        if place:
            reasons.append(f"These are London-wide patterns, not specific to {place}.")

        when_used = (
            f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')} "
            f"{moment.year}, {moment.strftime('%H:%M')} London time"
        )
        surge = level in ("high", "very high")
        data = {
            "when_used": when_used,
            "day": moment.strftime("%A"),
            "demand_level": level,
            "surge_likely": surge,
            "wait_estimate": WAITS[level],
            "night_tube_running": night_tube,
            "tube_running": tube,
            "reasons": reasons,
            "apps_to_try": list(APPS_TO_TRY),
        }
        if rain is not None:
            data["rain_chance_percent"] = rain

        narrative = (
            f"This is an estimate from time patterns, not live availability: for "
            f"{when_used}, demand is likely {level}, with a wait of "
            f"{WAITS[level]}."
        )
        return Result(data=data, narrative=narrative)

    @staticmethod
    def _parse(text: str) -> datetime:
        cleaned = text.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            parsed = None
        if parsed is None:
            for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
                try:
                    parsed = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise CannotReadTime(f"could not parse {text!r}")
        if len(cleaned) <= 10 and parsed.time() == time(0, 0):
            # A date with no time: assume an evening out.
            parsed = parsed.replace(hour=22)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LONDON)
        return parsed.astimezone(LONDON)

    @staticmethod
    def _demand(moment: datetime) -> tuple[str, list[str]]:
        reasons: list[str] = []
        d, minutes = moment.date(), moment.hour * 60 + moment.minute
        weekday = moment.weekday()  # Monday = 0
        # Before 06:00 counts as the night before.
        night_of = (moment.toordinal() - 1) if minutes < 360 else moment.toordinal()
        night_weekday = date.fromordinal(night_of).weekday()
        night_date = date.fromordinal(night_of)

        if (night_date.month, night_date.day) == (12, 31) and (
            minutes >= 18 * 60 or minutes < 360
        ):
            return "very high", ["New Year's Eve is the busiest night of the year."]
        if (d.month, d.day) == (12, 24) and minutes >= 18 * 60:
            return "very high", ["Christmas Eve evenings are very busy."]
        if d in BANK_HOLIDAYS or night_date in BANK_HOLIDAYS:
            reasons.append("It is a bank holiday, so demand runs higher.")
            if minutes >= 18 * 60 or minutes < 360:
                return "very high", reasons

        is_weekend_night = night_weekday in (4, 5) and (
            minutes >= 22 * 60 or minutes < 3 * 60
        )
        if is_weekend_night:
            reasons.append("Friday and Saturday late nights are peak time for rides.")
            return "high", reasons
        if 3 * 60 <= minutes < 6 * 60:
            reasons.append("Between 3am and 6am few drivers are working.")
            return "low", reasons
        rush = (7 * 60 + 30 <= minutes < 9 * 60 + 30) or (17 * 60 <= minutes < 19 * 60)
        if weekday < 5 and rush and d not in BANK_HOLIDAYS:
            reasons.append("Weekday rush hour keeps drivers busy.")
            return "busy", reasons
        if not reasons:
            reasons.append("An ordinary time of the week for rides.")
        return "normal", reasons

    @staticmethod
    def _night_tube(moment: datetime) -> bool:
        minutes = moment.hour * 60 + moment.minute
        # Friday night and Saturday night: early hours of Saturday and Sunday.
        return moment.weekday() in (5, 6) and 30 <= minutes < 5 * 60 + 30

    @staticmethod
    def _tube(moment: datetime) -> bool:
        if (moment.month, moment.day) == (12, 25):
            return False
        minutes = moment.hour * 60 + moment.minute
        if moment.weekday() == 6:  # Sunday: later start, earlier finish
            return 7 * 60 <= minutes < 23 * 60 + 30
        return minutes >= 5 * 60 + 30 or minutes < 30

    def _rain(self, moment: datetime) -> int | None:
        """Best-effort: any failure or an hour outside the forecast is None."""
        try:
            raw = self._fetch(FORECAST_URL)
            hourly = json.loads(raw)["hourly"]
            key = moment.strftime("%Y-%m-%dT%H:00")
            index = hourly["time"].index(key)
            value = hourly["precipitation_probability"][index]
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError):
            return None
        except (ValueError, KeyError, TypeError, IndexError, OSError):
            return None
        if value is None:
            return None
        return int(round(value))
