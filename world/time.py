from __future__ import annotations

import re
from dataclasses import dataclass


_TIME_RE = re.compile(
    r"^Y(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) (?P<hour>\d{2}):(?P<minute>\d{2})(:(?P<second>\d{2}))?$"
)

_SIMPLE_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    flags=re.IGNORECASE,
)


def _minutes_per_day() -> int:
    return 24 * 60


def _minutes_per_month() -> int:
    return 30 * _minutes_per_day()


def _minutes_per_year() -> int:
    return 12 * _minutes_per_month()


def _seconds_per_minute() -> int:
    return 60


def _seconds_per_day() -> int:
    return _minutes_per_day() * _seconds_per_minute()


def _seconds_per_month() -> int:
    return _minutes_per_month() * _seconds_per_minute()


def _seconds_per_year() -> int:
    return _minutes_per_year() * _seconds_per_minute()


@dataclass(frozen=True, order=True)
class WorldTime:
    """Fiction-friendly time.

    Format: Y0000-01-01 00:00
    Calendar rules (simple + deterministic):
    - 12 months per year
    - 30 days per month
    """

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int = 0

    @staticmethod
    def parse(value: str) -> "WorldTime":
        m = _TIME_RE.match((value or "").strip())
        if not m:
            raise ValueError("Invalid time format. Expected: Y0000-01-01 00:00 (optional :SS)")

        year = int(m.group("year"))
        month = int(m.group("month"))
        day = int(m.group("day"))
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        second = int(m.group("second") or "0")

        if not (0 <= year <= 9999):
            raise ValueError("Year out of range (0..9999)")
        if not (1 <= month <= 12):
            raise ValueError("Month out of range (1..12)")
        if not (1 <= day <= 30):
            raise ValueError("Day out of range (1..30)")
        if not (0 <= hour <= 23):
            raise ValueError("Hour out of range (0..23)")
        if not (0 <= minute <= 59):
            raise ValueError("Minute out of range (0..59)")
        if not (0 <= second <= 59):
            raise ValueError("Second out of range (0..59)")

        return WorldTime(year=year, month=month, day=day, hour=hour, minute=minute, second=second)

    def to_string(self) -> str:
        return (
            f"Y{self.year:04d}-{self.month:02d}-{self.day:02d} "
            f"{self.hour:02d}:{self.minute:02d}:{int(self.second or 0):02d}"
        )

    def time_of_day(self) -> str:
        """Return a human-readable label for the current hour."""
        h = self.hour
        if h < 5:
            return "night"
        if h < 7:
            return "dawn"
        if h < 12:
            return "morning"
        if h < 14:
            return "midday"
        if h < 17:
            return "afternoon"
        if h < 19:
            return "dusk"
        if h < 22:
            return "evening"
        return "night"

    def to_minutes(self) -> int:
        return self.to_seconds() // 60

    def to_seconds(self) -> int:
        return (
            self.year * _seconds_per_year()
            + (self.month - 1) * _seconds_per_month()
            + (self.day - 1) * _seconds_per_day()
            + self.hour * 3600
            + self.minute * 60
            + int(self.second or 0)
        )

    @staticmethod
    def from_minutes(total_minutes: int) -> "WorldTime":
        return WorldTime.from_seconds(int(total_minutes) * 60)

    @staticmethod
    def from_seconds(total_seconds: int) -> "WorldTime":
        if total_seconds < 0:
            raise ValueError("Time cannot be negative")

        year, rem = divmod(int(total_seconds), _seconds_per_year())
        month0, rem = divmod(rem, _seconds_per_month())
        day0, rem = divmod(rem, _seconds_per_day())
        hour, rem = divmod(rem, 3600)
        minute, second = divmod(rem, 60)

        return WorldTime(
            year=int(year),
            month=int(month0) + 1,
            day=int(day0) + 1,
            hour=int(hour),
            minute=int(minute),
            second=int(second),
        )

    def add_minutes(self, delta_minutes: int) -> "WorldTime":
        return WorldTime.from_minutes(self.to_minutes() + int(delta_minutes))

    def add_seconds(self, delta_seconds: int) -> "WorldTime":
        return WorldTime.from_seconds(self.to_seconds() + int(delta_seconds))

    def add_duration(self, duration: "WorldDuration") -> "WorldTime":
        return self.add_seconds(duration.to_seconds())


@dataclass(frozen=True)
class WorldDuration:
    """Fiction-friendly duration.

    Uses the same text layout as WorldTime but is interpreted as a *duration*.

    Format: Y0000-00-00 00:00 (optional :SS)
    Rules:
    - months: 0..11
    - days: 0..30
    - hours: 0..23
    - minutes: 0..59
    - seconds: 0..59
    """

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int = 0

    @staticmethod
    def parse_user_input(value: str) -> "WorldDuration":
        """Parse a human-friendly duration.

        Single accepted format: number + unit, e.g. "30 seconds", "5m", "2 hours", "1d".

        This is intended for GM/tool inputs. Persisted durations are stored in the
        canonical WorldDuration string form and should be parsed with parse().
        """

        raw = (value or "").strip()
        sm = _SIMPLE_DURATION_RE.match(raw)
        if not sm:
            raise ValueError(
                "Invalid duration input. Expected '<number><unit>' like '30s', '5m', '2h', '1d' (also accepts '30 seconds', '5 minutes', '2 hours', '1 day')."
            )

        amount = int(sm.group("value"))
        if amount <= 0:
            raise ValueError("Duration must be greater than zero")

        unit = sm.group("unit").lower()
        if unit in {"s", "sec", "secs", "second", "seconds"}:
            seconds = amount
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            seconds = amount * 60
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            seconds = amount * 3600
        elif unit in {"d", "day", "days"}:
            seconds = amount * _seconds_per_day()
        else:
            raise ValueError("Invalid duration unit")

        return WorldDuration.from_seconds(seconds)

    @staticmethod
    def parse(value: str) -> "WorldDuration":
        m = _TIME_RE.match((value or "").strip())
        if not m:
            raise ValueError("Invalid duration format. Expected: Y0000-00-00 00:00 (optional :SS)")

        year = int(m.group("year"))
        month = int(m.group("month"))
        day = int(m.group("day"))
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        second = int(m.group("second") or "0")

        if not (0 <= year <= 9999):
            raise ValueError("Duration year out of range (0..9999)")
        if not (0 <= month <= 11):
            raise ValueError("Duration month out of range (0..11)")
        if not (0 <= day <= 30):
            raise ValueError("Duration day out of range (0..30)")
        if not (0 <= hour <= 23):
            raise ValueError("Duration hour out of range (0..23)")
        if not (0 <= minute <= 59):
            raise ValueError("Duration minute out of range (0..59)")
        if not (0 <= second <= 59):
            raise ValueError("Duration second out of range (0..59)")

        if year == 0 and month == 0 and day == 0 and hour == 0 and minute == 0 and second == 0:
            raise ValueError("Duration must be greater than zero")

        return WorldDuration(year=year, month=month, day=day, hour=hour, minute=minute, second=second)

    def to_string(self) -> str:
        base = f"Y{self.year:04d}-{self.month:02d}-{self.day:02d} {self.hour:02d}:{self.minute:02d}"
        if int(self.second or 0) != 0:
            return base + f":{self.second:02d}"
        return base

    def to_user_string(self) -> str:
        """Render as the single user-facing duration format.

        Format: <number><unit> using the largest unit that divides evenly.
        Units: d, h, m, s.
        """

        total_seconds = int(self.to_seconds())
        if total_seconds <= 0:
            raise ValueError("Duration must be greater than zero")

        if total_seconds % _seconds_per_day() == 0:
            return f"{total_seconds // _seconds_per_day()}d"
        if total_seconds % 3600 == 0:
            return f"{total_seconds // 3600}h"
        if total_seconds % 60 == 0:
            return f"{total_seconds // 60}m"
        return f"{total_seconds}s"

    def to_seconds(self) -> int:
        return (
            self.year * _seconds_per_year()
            + self.month * _seconds_per_month()
            + self.day * _seconds_per_day()
            + self.hour * 3600
            + self.minute * 60
            + int(self.second or 0)
        )

    @staticmethod
    def from_minutes(total_minutes: int) -> "WorldDuration":
        return WorldDuration.from_seconds(int(total_minutes) * 60)

    @staticmethod
    def from_seconds(total_seconds: int) -> "WorldDuration":
        if total_seconds <= 0:
            raise ValueError("Duration must be greater than zero")

        year, rem = divmod(int(total_seconds), _seconds_per_year())
        month, rem = divmod(rem, _seconds_per_month())
        day, rem = divmod(rem, _seconds_per_day())
        hour, rem = divmod(rem, 3600)
        minute, second = divmod(rem, 60)

        # Normalize month/day to the supported ranges.
        month = int(month)
        day = int(day)
        hour = int(hour)
        minute = int(minute)
        second = int(second)
        year = int(year)

        if month > 11 or day > 30:
            # Should not happen due to divmod bases, but keep defensive.
            raise ValueError("Duration components out of range")

        return WorldDuration(year=year, month=month, day=day, hour=hour, minute=minute, second=second)
