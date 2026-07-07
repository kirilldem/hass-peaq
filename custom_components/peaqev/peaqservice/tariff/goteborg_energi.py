"""Göteborg Energi time-of-use power tariff (effektavgift) support.

The grid is measured in Normal Time (CET/UTC+1) all year.  During Summer
Time (DST / CET+2) the high-price window shifts forward by one hour in
local clock time.

Winter  (Nov 1 – Mar 31):  High tariff on non-holiday weekdays 07:00–20:00 (Normal Time).
                            Low tariff on weekends, Swedish red holidays, and 20:00–07:00.
Summer  (Apr 1 – Oct 31):  Low tariff 24/7.

Cost is billed on the average of the three highest hourly peaks spread
across three different days in the month.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.core import State

_LOGGER = logging.getLogger(__name__)

DEFAULT_SENSOR = "binary_sensor.goteborg_energi_nat_ab_tidsindelad_6_kw_max_63a_high_tariff_active"

# Swedish "red" (public) holidays that affect the tariff.
# We use a minimal, deterministic set for the fallback calculation.
# For production use, the `holidays` Python package or the HA holiday
# integration is recommended.  This covers the most common Swedish holidays.
_SWEDISH_HOLIDAYS_MONTH_DAY = {
    (1, 1):   "Nyårsdagen",
    (1, 6):   "Trettondedag jul",
    (5, 1):   "Första maj",
    (6, 6):   "Sveriges nationaldag",
    (12, 24): "Julafton",
    (12, 25): "Juldagen",
    (12, 26): "Annandag jul",
    (12, 31): "Nyårsafton",
    # Movable feasts are handled dynamically below
}


class GoteborgEnergiTariff:
    """Determine whether the Göteborg Energi high-tariff window is currently active.

    Primary: reads the configured HA binary sensor.
    Fallback: computes the window from local time, DST awareness, and
    Swedish holiday rules.
    """

    HIGH_TARIFF_START_NORMAL = 7   # 07:00 Normal Time
    HIGH_TARIFF_END_NORMAL = 20    # 20:00 Normal Time

    def __init__(
        self,
        hass: HomeAssistant,
        sensor_entity: str | None = None,
        enable_fallback: bool = True,
    ):
        self._hass = hass
        self._sensor = sensor_entity or DEFAULT_SENSOR
        self._enable_fallback = enable_fallback
        self._sensor_available = False

    @property
    def sensor_entity(self) -> str:
        return self._sensor

    @sensor_entity.setter
    def sensor_entity(self, value: str | None):
        self._sensor = value or DEFAULT_SENSOR

    @property
    def enable_fallback(self) -> bool:
        return self._enable_fallback

    @enable_fallback.setter
    def enable_fallback(self, value: bool):
        self._enable_fallback = bool(value)

    def _check_sensor(self) -> bool:
        """Try to read the HA sensor.  Returns True if the sensor reported 'on'."""
        if not self._sensor:
            return False
        state: State | None = self._hass.states.get(self._sensor)
        if state is None or state.state in ("unavailable", "unknown", None):
            self._sensor_available = False
            return False
        self._sensor_available = True
        return state.state.lower() == "on"

    @property
    def high_tariff_active(self) -> bool:
        """True when the Göteborg Energi high-tariff window is active *now*."""
        if self._check_sensor():
            return True
        if not self._enable_fallback:
            return False
        return self._fallback_high_tariff_active()

    @property
    def is_summer(self) -> bool:
        """Apr 1 – Oct 31 is always low tariff."""
        now = datetime.now()
        return now.month >= 4 and now.month <= 10

    @property
    def is_holiday(self) -> bool:
        """True if today is a Swedish red holiday or weekend."""
        return self._is_swedish_holiday(datetime.now())

    def _fallback_high_tariff_active(self) -> bool:
        """Compute high-tariff from clock time, DST, and holiday calendar."""
        now = datetime.now()
        if self.is_summer:
            return False
        if self.is_holiday:
            return False
        # Grid measured in Normal Time (CET/UTC+1).
        # If local time is DST (last Sunday March → last Sunday October),
        # the high-price window is 08:00–21:00 local.
        # Otherwise it is 07:00–20:00 local.
        is_dst = self._is_dst(now)
        start_hour = self.HIGH_TARIFF_START_NORMAL + (1 if is_dst else 0)
        end_hour = self.HIGH_TARIFF_END_NORMAL + (1 if is_dst else 0)
        return start_hour <= now.hour < end_hour

    @staticmethod
    def _is_dst(now: datetime) -> bool:
        """Determine if Daylight Saving Time is active in Sweden.

        DST starts the last Sunday in March and ends the last Sunday in October.
        """
        # March: last Sunday
        dst_start = GoteborgEnergiTariff._last_sunday(now.year, 3)
        # October: last Sunday
        dst_end = GoteborgEnergiTariff._last_sunday(now.year, 10)
        return dst_start <= now.replace(tzinfo=None) < dst_end

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        """Find the last Sunday of the given month/year."""
        if month == 12:
            last_day = 31
        else:
            last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).day
        d = datetime(year, month, last_day)
        offset = (d.weekday() - 6) % 7  # 6 = Sunday
        return d - timedelta(days=offset)

    def _is_swedish_holiday(self, d: datetime) -> bool:
        """Check if a date is a Swedish red holiday or weekend."""
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return True
        if (d.month, d.day) in _SWEDISH_HOLIDAYS_MONTH_DAY:
            return True
        # Movable feasts
        easter = self._easter_sunday(d.year)
        good_friday = easter - timedelta(days=2)
        easter_monday = easter + timedelta(days=1)
        ascension = easter + timedelta(days=39)
        pentecost = easter + timedelta(days=49)
        midsummer_friday = self._midsummer_friday(d.year)
        if d.date() in [
            easter.date(),
            good_friday.date(),
            easter_monday.date(),
            ascension.date(),
            pentecost.date(),
            midsummer_friday.date(),
        ]:
            return True
        return False

    @staticmethod
    def _easter_sunday(year: int) -> datetime:
        """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return datetime(year, month, day)

    @staticmethod
    def _midsummer_friday(year: int) -> datetime:
        """Midsummer Eve is the Friday between June 19 and June 25."""
        d = datetime(year, 6, 19)
        # Move to first Friday on or after June 19
        while d.weekday() != 4:
            d += timedelta(days=1)
        return d

    @property
    def peak_cost_calculation(self) -> str:
        """Describe the billing method for user-facing sensors."""
        return "Average of the three highest hourly peaks across three different days in the month."

    def should_avoid_charging(self) -> bool:
        """Convenience: True when charging during this moment would risk a peak fee."""
        return self.high_tariff_active

    def next_low_tariff_window(self) -> datetime:
        """Return the next datetime when low tariff starts (for scheduling)."""
        now = datetime.now()
        if not self.high_tariff_active:
            return now
        is_dst = self._is_dst(now)
        end_hour = self.HIGH_TARIFF_END_NORMAL + (1 if is_dst else 0)
        candidate = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        # If tomorrow is a holiday or summer, low tariff continues
        return candidate

    def high_tariff_hours_today(self) -> list[int]:
        """Return list of clock hours (local) where high tariff is active today."""
        if self.is_summer or self.is_holiday:
            return []
        is_dst = self._is_dst(datetime.now())
        start = self.HIGH_TARIFF_START_NORMAL + (1 if is_dst else 0)
        end = self.HIGH_TARIFF_END_NORMAL + (1 if is_dst else 0)
        return list(range(start, end))
