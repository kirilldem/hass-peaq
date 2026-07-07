"""Tests for the departure scheduler."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from custom_components.peaqev.peaqservice.tariff.departure_scheduler import (
    DepartureSchedule, DepartureScheduler)
from custom_components.peaqev.peaqservice.tariff.interval_planner import (
    IntervalPlanner, IntervalSlot)


def _make_hub(charger_amps=16, soc_state=None):
    hub = MagicMock()
    hub.hours.prices = [1.0, 2.0, 0.5, 3.0, 1.5, 0.8]
    hub.hours.prices_tomorrow = []
    hub.sensors.powersensormovingaverage24.value = 1000
    hub.chargertype.max_amps = charger_amps
    hub.state_machine.states.get.return_value = MagicMock()
    hub.state_machine.states.get.return_value.state = soc_state
    return hub


class TestDepartureScheduler:
    """Test the DepartureScheduler class."""

    def test_estimate_charge_minutes(self):
        hub = _make_hub(charger_amps=16)
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        minutes = scheduler.estimate_charge_minutes(10.0)
        # 10 kWh at 11040W * 0.9 efficiency = ~3242 minutes... no:
        # P = 230 * 16 * 3 * 0.9 = 9936W = 9.936 kW
        # time = 10 / 9.936 = 1.006 hours = ~60 minutes
        assert minutes > 0
        assert minutes % 15 == 0  # Should be rounded to 15-min slots

    def test_estimate_zero_charge(self):
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        assert scheduler.estimate_charge_minutes(0.0) == 0

    def test_create_schedule_returns_slots(self):
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        now = datetime.now()
        departure = now + timedelta(hours=4)
        schedule = scheduler.create_schedule(
            charge_amount_kwh=5.0,
            departure_time=departure,
        )
        assert isinstance(schedule, DepartureSchedule)
        assert schedule.departure_time == departure
        assert len(schedule.selected_slots) >= 1

    def test_create_schedule_past_departure(self):
        """If departure is in the past, return empty schedule."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        now = datetime.now()
        departure = now - timedelta(hours=1)
        schedule = scheduler.create_schedule(
            charge_amount_kwh=5.0,
            departure_time=departure,
        )
        assert len(schedule.selected_slots) == 0
        assert schedule.estimated_charge_minutes == 0

    def test_cancel_schedule(self):
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        now = datetime.now()
        departure = now + timedelta(hours=4)
        scheduler.create_schedule(charge_amount_kwh=5.0, departure_time=departure)
        assert scheduler.active_schedule is not None
        scheduler.cancel_schedule()
        assert scheduler.active_schedule is None

    def test_should_charge_now_no_schedule(self):
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        assert scheduler.should_charge_now() is False

    def test_volvo_soc_sensor_reading(self):
        hub = _make_hub(soc_state="45.5")
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(
            hub=hub, interval_planner=planner,
            volvo_soc_sensor="sensor.volvo_battery_level",
        )
        soc = scheduler._get_current_soc()
        assert soc == 45.5

    def test_volvo_soc_sensor_unavailable(self):
        hub = _make_hub()
        hub.state_machine.states.get.return_value = None
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(
            hub=hub, interval_planner=planner,
            volvo_soc_sensor="sensor.volvo_battery_level",
        )
        assert scheduler._get_current_soc() is None

    def test_margin_minutes(self):
        """Margin should be positive when departure is well ahead."""
        hub = _make_hub()
        planner = IntervalPlanner(hub=hub, enabled=True, peak_threshold_w=0)
        scheduler = DepartureScheduler(hub=hub, interval_planner=planner)
        now = datetime.now()
        departure = now + timedelta(hours=6)
        schedule = scheduler.create_schedule(
            charge_amount_kwh=2.0,
            departure_time=departure,
        )
        # Should have some margin
        assert schedule.margin_minutes >= 0
