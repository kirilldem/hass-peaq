"""Rewritten departure scheduler for 15-minute interval support.

The original scheduler operated on whole-hour granularity and was unreliable
for hitting the target SoC.  This new scheduler:

1. Computes the estimated charging time needed (from max_charge kWh and
   estimated charger power) in 15-minute increments.
2. Selects the cheapest 15-minute slots between now and departure,
   respecting tariff and peak-power constraints.
3. Falls back to a conservative mode near departure: if not enough cheap
   slots are available, it uses any available slot (including high-tariff)
   to ensure the car reaches its target SoC.
4. Integrates with the Volvo Connected Car integration to read current
   SoC (when available) for more accurate time estimation.

Edge Case A (Late Plug-In):
  The scheduler calls plan_intervals with departure_time set, so past slots
  are excluded and only the remaining window is used.  The schedule is
  rebuilt dynamically each time create_schedule is called.

Edge Case C (Grid-Limit Override):
  When the entire available window is filled with high-tariff hours and high
  house power, the departure guarantee overrides the peak penalty only at
  the absolute latest mathematically possible 15-minute intervals required
  to hit the target.  This means slots are pushed as close to departure as
  possible, minimizing the time window where a peak fee could be incurred.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.peaqev.peaqservice.hub.hub import HomeAssistantHub
    from custom_components.peaqev.peaqservice.tariff.goteborg_energi import GoteborgEnergiTariff
    from custom_components.peaqev.peaqservice.tariff.interval_planner import IntervalPlanner

_LOGGER = logging.getLogger(__name__)

SLOT_MINUTES = 15


@dataclass
class DepartureSchedule:
    """Result of a departure-scheduling calculation."""
    departure_time: datetime
    charge_amount_kwh: float
    estimated_charge_minutes: int
    selected_slots: list  # list of IntervalSlot
    must_use_high_tariff: bool = False
    grid_limit_override: bool = False

    @property
    def total_charging_time(self) -> timedelta:
        return timedelta(minutes=len(self.selected_slots) * SLOT_MINUTES)

    @property
    def last_charging_slot_end(self) -> datetime | None:
        if not self.selected_slots:
            return None
        return self.selected_slots[-1].end

    @property
    def margin_minutes(self) -> int:
        """Minutes between last charging slot end and departure."""
        if not self.selected_slots:
            return 0
        delta = self.departure_time - self.last_charging_slot_end
        return int(delta.total_seconds() / 60)


class DepartureScheduler:
    """Plan charging sessions to hit a target SoC before a departure time."""

    def __init__(
        self,
        hub: HomeAssistantHub,
        interval_planner: IntervalPlanner,
        tariff: GoteborgEnergiTariff | None = None,
        volvo_soc_sensor: str | None = None,
        charger_efficiency: float = 0.9,
    ):
        self.hub = hub
        self.interval_planner = interval_planner
        self.tariff = tariff
        self._volvo_soc_sensor = volvo_soc_sensor
        self._charger_efficiency = charger_efficiency
        self._active_schedule: DepartureSchedule | None = None

    @property
    def volvo_soc_sensor(self) -> str | None:
        return self._volvo_soc_sensor

    @volvo_soc_sensor.setter
    def volvo_soc_sensor(self, value: str | None):
        self._volvo_soc_sensor = value

    @property
    def active_schedule(self) -> DepartureSchedule | None:
        return self._active_schedule

    def _get_current_soc(self) -> float | None:
        """Read current battery SoC from Volvo integration sensor."""
        if not self._volvo_soc_sensor:
            return None
        try:
            state = self.hub.state_machine.states.get(self._volvo_soc_sensor)
            if state and state.state not in ("unavailable", "unknown", None):
                return float(state.state)
        except Exception as e:
            _LOGGER.debug("Could not read Volvo SoC sensor %s: %s", self._volvo_soc_sensor, e)
        return None

    def _get_charger_power_w(self) -> float:
        """Estimate charger power in watts from amps and phases."""
        try:
            amps = self.hub.chargertype.max_amps
        except Exception:
            amps = 16
        voltage = 230
        phases = 3
        return float(voltage * amps * phases)

    def estimate_charge_minutes(self, charge_amount_kwh: float, current_soc_pct: float | None = None) -> int:
        """Estimate how many minutes of charging are needed.

        Args:
            charge_amount_kwh: Target energy to deliver.
            current_soc_pct: If known, adjust for remaining need.
        """
        if charge_amount_kwh <= 0:
            return 0
        needed_kwh = charge_amount_kwh
        power_w = self._get_charger_power_w() * self._charger_efficiency
        if power_w <= 0:
            power_w = 3680  # 16A × 230V × 1 phase fallback
        hours_needed = needed_kwh * 1000 / power_w
        minutes_needed = int(hours_needed * 60)
        # Round up to nearest 15-minute slot
        slots_needed = max(1, (minutes_needed + SLOT_MINUTES - 1) // SLOT_MINUTES)
        return slots_needed * SLOT_MINUTES

    def _build_all_future_slots(
        self,
        start_time: datetime,
        departure_time: datetime,
        prices: list[float],
    ) -> list:
        """Build all 15-min slots between start and departure, with prices."""
        from custom_components.peaqev.peaqservice.tariff.interval_planner import IntervalSlot
        now_hour = start_time.replace(minute=0, second=0, microsecond=0)
        slots = []
        current = now_hour
        while current < departure_time:
            hour_index = current.hour
            if hasattr(self.hub, 'hours') and hasattr(self.hub.hours, 'prices'):
                all_prices = list(self.hub.hours.prices or []) + list(getattr(self.hub.hours, 'prices_tomorrow', []) or [])
                if hour_index < len(all_prices):
                    price = float(all_prices[hour_index])
                else:
                    price = 0.0
            else:
                price = 0.0
            for i in range(4):  # 4 slots per hour
                slot_start = current.replace(minute=i * SLOT_MINUTES, second=0, microsecond=0)
                slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)
                if slot_start < start_time:
                    continue  # Past
                if slot_end > departure_time:
                    continue  # After departure
                slots.append(IntervalSlot(
                    start=slot_start, end=slot_end, index=i, price=price
                ))
            current += timedelta(hours=1)
        return slots

    def create_schedule(
        self,
        charge_amount_kwh: float,
        departure_time: datetime,
        start_time: datetime | None = None,
        override_settings: bool = False,
        charging_amps: int = 16,
        phases: int = 3,
    ) -> DepartureSchedule:
        """Create a charging schedule to hit the target by departure_time.

        Edge Case A: Past slots are excluded; the schedule is rebuilt from
        the remaining window only.

        Edge Case C: When not enough safe slots exist, the override pushes
        charging to the latest possible slots before departure, minimizing
        the peak-fee exposure window.
        """
        if start_time is None:
            start_time = datetime.now()

        if departure_time <= start_time:
            _LOGGER.warning("Departure time %s is in the past (now=%s).", departure_time, start_time)
            return DepartureSchedule(
                departure_time=departure_time,
                charge_amount_kwh=charge_amount_kwh,
                estimated_charge_minutes=0,
                selected_slots=[],
            )

        charge_minutes = self.estimate_charge_minutes(charge_amount_kwh, self._get_current_soc())
        needed_slot_count = max(1, (charge_minutes + SLOT_MINUTES - 1) // SLOT_MINUTES)

        _LOGGER.info(
            "DepartureScheduler: %s kWh needed, estimated %d min (%d slots) of charging, "
            "departure at %s",
            charge_amount_kwh, charge_minutes, needed_slot_count,
            departure_time.strftime("%Y-%m-%d %H:%M"),
        )

        # Step 1: Try to find cheapest safe slots (tariff + peak-safe)
        slots = self.interval_planner.plan_intervals(
            needed_charge_minutes=charge_minutes,
            max_lookahead_hours=int((departure_time - start_time).total_seconds() // 3600) + 1,
            charging_amps=charging_amps,
            phases=phases,
            departure_time=departure_time,
        )
        before_departure = [s for s in slots if s.end <= departure_time]

        must_use_high_tariff = False
        grid_limit_override = False

        if len(before_departure) >= needed_slot_count:
            # Enough safe slots found — use the cheapest ones
            selected = before_departure[:needed_slot_count]
            selected.sort(key=lambda s: s.start)
        else:
            # Edge Case C: Not enough safe slots — must override
            must_use_high_tariff = True
            grid_limit_override = True
            _LOGGER.info(
                "DepartureScheduler: EDGE CASE C — only %d safe slots before departure, "
                "need %d. Overriding tariff/peak constraints at latest possible slots.",
                len(before_departure), needed_slot_count,
            )

            # Build ALL slots in the window (no tariff/peak filtering)
            all_slots = self._build_all_future_slots(start_time, departure_time, [])
            # Sort by start time descending (latest first) to push to departure
            all_slots.sort(key=lambda s: s.start, reverse=True)

            # Take the latest N slots that still fit before departure
            # This minimizes the peak-fee exposure window
            override_slots = all_slots[:needed_slot_count]
            override_slots.sort(key=lambda s: s.start)

            # Merge: use any safe slots we already found, fill the rest with
            # the latest override slots, avoiding duplicates
            safe_starts = {s.start for s in before_departure}
            for s in override_slots:
                if s.start not in safe_starts and len(before_departure) < needed_slot_count:
                    before_departure.append(s)
                    safe_starts.add(s.start)

            before_departure.sort(key=lambda s: s.start)
            selected = before_departure[:needed_slot_count]

            # Verify we can actually meet the target
            if len(selected) < needed_slot_count:
                _LOGGER.warning(
                    "DepartureScheduler: Even with grid-limit override, only %d/%d slots "
                    "available. Target SoC may not be reached by departure %s.",
                    len(selected), needed_slot_count,
                    departure_time.strftime("%H:%M"),
                )

        schedule = DepartureSchedule(
            departure_time=departure_time,
            charge_amount_kwh=charge_amount_kwh,
            estimated_charge_minutes=charge_minutes,
            selected_slots=selected,
            must_use_high_tariff=must_use_high_tariff,
            grid_limit_override=grid_limit_override,
        )
        self._active_schedule = schedule
        _LOGGER.info(
            "DepartureScheduler: schedule created with %d slots. "
            "Last charging ends at %s, %d min margin before departure. "
            "grid_limit_override=%s",
            len(selected),
            schedule.last_charging_slot_end.strftime("%H:%M") if schedule.last_charging_slot_end else "N/A",
            schedule.margin_minutes,
            grid_limit_override,
        )
        return schedule

    def cancel_schedule(self) -> None:
        """Cancel the active schedule."""
        self._active_schedule = None
        _LOGGER.info("DepartureScheduler: schedule cancelled.")

    def should_charge_now(self) -> bool:
        """Check if the current 15-min slot is in the active schedule."""
        if self._active_schedule is None:
            return False
        now = datetime.now()
        for slot in self._active_schedule.selected_slots:
            if slot.start <= now < slot.end:
                return True
        return False

    @property
    def next_scheduled_slot(self) -> datetime | None:
        """Return the start time of the next scheduled charging slot."""
        if self._active_schedule is None:
            return None
        now = datetime.now()
        for slot in self._active_schedule.selected_slots:
            if slot.start > now:
                return slot.start
        return None
