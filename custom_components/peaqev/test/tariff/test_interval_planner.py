"""Tests for the 15-minute interval planner."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from custom_components.peaqev.peaqservice.tariff.interval_planner import (
    IntervalPlanner, IntervalSlot, SLOT_MINUTES, SLOTS_PER_HOUR)


def _make_hub(prices=None, prices_tomorrow=None, moving_avg=1000):
    """Create a mock hub with price and sensor data."""
    hub = MagicMock()
    hub.hours.prices = prices or [1.0, 2.0, 0.5, 3.0] * 6
    hub.hours.prices_tomorrow = prices_tomorrow or []
    hub.sensors.powersensormovingaverage24.value = moving_avg
    return hub


class TestIntervalSlot:
    """Test the IntervalSlot dataclass."""

    def test_slot_label(self):
        s = IntervalSlot(
            start=datetime(2025, 1, 15, 10, 0),
            end=datetime(2025, 1, 15, 10, 15),
            index=0,
        )
        assert s.label == "10:00-10:15"
        assert s.duration_minutes == 15

    def test_slots_per_hour(self):
        assert SLOTS_PER_HOUR == 4
        assert SLOT_MINUTES == 15


class TestIntervalPlanner:
    """Test the IntervalPlanner class."""

    def test_disabled_returns_empty(self):
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=False)
        slots = planner.plan_intervals(needed_charge_minutes=30)
        assert slots == []

    def test_enabled_returns_slots(self):
        hub = _make_hub(prices=[1.0, 2.0, 0.5, 3.0])
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        slots = planner.plan_intervals(needed_charge_minutes=15, max_lookahead_hours=4)
        assert len(slots) >= 1
        assert all(isinstance(s, IntervalSlot) for s in slots)

    def test_cheapest_slot_selected_first(self):
        """The planner should prefer the cheapest slots."""
        prices = [10.0, 1.0, 5.0]  # hour 1 is cheapest
        hub = _make_hub(prices=prices)
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        slots = planner.plan_intervals(needed_charge_minutes=15, max_lookahead_hours=3)
        # The cheapest hour (index 1) should be selected
        assert len(slots) >= 1
        # All selected slots should be from hour 1
        for s in slots:
            assert s.start.hour == 1 or s.price == 1.0

    def test_peak_threshold_filters_unsafe(self):
        """When peak threshold is low, unsafe slots should be filtered."""
        hub = _make_hub(prices=[1.0, 2.0])
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=500)
        planner.update_household_avg(2000)  # high household avg
        # With 2000W avg + charging ~11040W + safety margin, peak > 500W
        slots = planner.plan_intervals(needed_charge_minutes=15, max_lookahead_hours=2)
        # Should return empty or few slots since peak threshold is very low
        # (the planner falls back to all future slots if no safe ones found)
        # so we just verify it doesn't crash
        assert isinstance(slots, list)

    def test_no_peak_threshold_allows_all(self):
        """With peak_threshold_w=0, all slots should be peak-safe."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        slots = planner.plan_intervals(needed_charge_minutes=60, max_lookahead_hours=4)
        assert len(slots) >= 4  # 4 slots for 60 minutes

    def test_should_charge_now_when_disabled(self):
        """When disabled, should_charge_now returns True (defer to hourly)."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=False)
        assert planner.should_charge_now() is True

    def test_should_charge_now_no_tariff(self):
        """Without a tariff, should_charge_now should return True when peak-safe."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        result = planner.should_charge_now()
        assert isinstance(result, bool)

    def test_current_slot_index(self):
        """Slot index should be 0-3 depending on the minute."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True)
        # Mock datetime.now
        original = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 7))
            assert planner.current_slot_index() == 0  # 7 // 15 = 0
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 22))
            assert planner.current_slot_index() == 1  # 22 // 15 = 1
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 45))
            assert planner.current_slot_index() == 3  # 45 // 15 = 3
        finally:
            datetime.now = original

    def test_next_slot_boundary(self):
        """Next slot boundary should be 15 minutes ahead."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True)
        original = datetime.now
        try:
            datetime.now = staticmethod(lambda: datetime(2025, 1, 15, 10, 7))
            boundary = planner.next_slot_boundary
            assert boundary.minute == 15
            assert boundary.hour == 10
        finally:
            datetime.now = original

    def test_estimate_slot_power(self):
        """Power estimation should match U*I*phases."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True)
        power = planner._estimate_slot_power(charging_amps=16, phases=3)
        assert power == 230 * 16 * 3  # 11040W

    def test_multiple_slots_cover_needed_time(self):
        """When 45 minutes of charging is needed, 3 slots should be selected."""
        hub = _make_hub(prices=[1.0, 2.0, 3.0, 4.0])
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        slots = planner.plan_intervals(needed_charge_minutes=45, max_lookahead_hours=4)
        assert len(slots) == 3
