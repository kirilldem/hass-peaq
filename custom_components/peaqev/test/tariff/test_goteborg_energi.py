"""Tests for the Göteborg Energi tariff module."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from custom_components.peaqev.peaqservice.tariff.goteborg_energi import (
    DEFAULT_SENSOR, GoteborgEnergiTariff)


def _make_hass(sensor_state=None):
    """Create a mock HA instance with a sensor state."""
    hass = MagicMock()
    state = MagicMock()
    if sensor_state is None:
        hass.states.get.return_value = None
    else:
        state.state = sensor_state
        hass.states.get.return_value = state
    return hass


class TestGoteborgEnergiTariff:
    """Test the Göteborg Energi tariff logic."""

    def test_sensor_on_returns_high_tariff(self):
        """When the HA sensor reports 'on', high tariff is active."""
        hass = _make_hass("on")
        tariff = GoteborgEnergiTariff(hass, enable_fallback=False)
        assert tariff.high_tariff_active is True
        assert tariff.should_avoid_charging() is True

    def test_sensor_off_returns_low_tariff(self):
        """When the HA sensor reports 'off', high tariff is not active."""
        hass = _make_hass("off")
        tariff = GoteborgEnergiTariff(hass, enable_fallback=False)
        assert tariff.high_tariff_active is False

    def test_sensor_unavailable_falls_back(self):
        """When the sensor is unavailable and fallback enabled, use time-based calc."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        # Should not raise; should return a bool
        result = tariff.high_tariff_active
        assert isinstance(result, bool)

    def test_sensor_unavailable_no_fallback(self):
        """When the sensor is unavailable and fallback disabled, return False."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=False)
        assert tariff.high_tariff_active is False

    def test_summer_is_low_tariff(self):
        """April through October is always low tariff."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        # Mock datetime.now() to July
        original_now = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 7, 15, 14, 0))
            assert tariff.is_summer is True
            assert tariff.high_tariff_active is False
        finally:
            datetime.now = original_now

    def test_winter_weekday_daytime_is_high_tariff(self):
        """Winter weekday daytime should be high tariff (fallback)."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            # Wednesday, Jan 15, 2025, 10:00 — not a holiday, not DST
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 0))
            assert tariff.is_summer is False
            assert tariff.is_holiday is False
            assert tariff.high_tariff_active is True
        finally:
            datetime.now = original_now

    def test_winter_weekday_nighttime_is_low_tariff(self):
        """Winter weekday night (21:00) should be low tariff."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 21, 0))
            assert tariff.high_tariff_active is False
        finally:
            datetime.now = original_now

    def test_winter_weekend_is_low_tariff(self):
        """Winter Saturday should be low tariff."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            # Saturday, Jan 18, 2025, 10:00
            datetime.now = staticmethod(lambda: datetime(2025, 1, 18, 10, 0))
            assert tariff.is_holiday is True
            assert tariff.high_tariff_active is False
        finally:
            datetime.now = original_now

    def test_christmas_day_is_low_tariff(self):
        """Christmas Day (Dec 25) should be low tariff even on a weekday."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            # Thursday, Dec 25, 2025, 10:00
            datetime.now = staticmethod(lambda: datetime(2025, 12, 25, 10, 0))
            assert tariff.is_holiday is True
            assert tariff.high_tariff_active is False
        finally:
            datetime.now = original_now

    def test_dst_shifts_window(self):
        """During DST (summer time), the high-price window shifts +1h."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        # In summer it's always low tariff, so test DST detection directly
        assert tariff._is_dst(datetime(2025, 6, 15, 12, 0)) is True
        assert tariff._is_dst(datetime(2025, 1, 15, 12, 0)) is False

    def test_easter_sunday_detection(self):
        """Easter Sunday should be detected as a holiday."""
        # Easter Sunday 2025 is April 20
        easter = GoteborgEnergiTariff._easter_sunday(2025)
        assert easter.month == 4
        assert easter.day == 20

    def test_midsummer_friday_detection(self):
        """Midsummer Friday should be detected as a holiday."""
        # Midsummer Friday 2025 is June 20
        ms = GoteborgEnergiTariff._midsummer_friday(2025)
        assert ms.month == 6
        assert ms.day == 20

    def test_high_tariff_hours_today_summer(self):
        """In summer, there are no high-tariff hours."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 7, 15, 10, 0))
            assert tariff.high_tariff_hours_today() == []
        finally:
            datetime.now = original_now

    def test_high_tariff_hours_today_winter_weekday(self):
        """In winter on a weekday, high-tariff hours should be 7-20 (Normal Time)."""
        hass = _make_hass(None)
        tariff = GoteborgEnergiTariff(hass, enable_fallback=True)
        original_now = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 0))
            hours = tariff.high_tariff_hours_today()
            assert len(hours) > 0
            assert 7 in hours
            assert 20 not in hours
        finally:
            datetime.now = original_now

    def test_default_sensor_constant(self):
        """The default sensor entity ID should match the expected pattern."""
        assert DEFAULT_SENSOR == (
            "binary_sensor.goteborg_energi_nat_ab_tidsindelad_6_kw_max_63a_high_tariff_active"
        )
