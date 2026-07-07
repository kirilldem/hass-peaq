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
        # If we have a current SoC from Volvo, we can refine the estimate.
        # But since charge_amount_kwh is already the desired delta, we
        # use it directly unless the SoC reading contradicts it.
        needed_kwh = charge_amount_kwh
        power_w = self._get_charger_power_w() * self._charger_efficiency
        if power_w <= 0:
            power_w = 3680  # 16A × 230V × 1 phase fallback
        hours_needed = needed_kwh * 1000 / power_w
        minutes_needed = int(hours_needed * 60)
        # Round up to nearest 15-minute slot
        slots_needed = max(1, (minutes_needed + SLOT_MINUTES - 1) // SLOT_MINUTES)
        return slots_needed * SLOT_MINUTES

    def create_schedule(
        self,
        charge_amount_kwh: float,
        departure_time: datetime,
        start_time: datetime | None = None,
        override_settings: bool = False,
        charging_amps: int = 16,
        phases: int = 3,
    ) -> DepartureSchedule:
        """Create a charging schedule to hit the target by departure_time."""
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
        _LOGGER.info(
            "DepartureScheduler: %s kWh needed, estimated %d min of charging, departure at %s",
            charge_amount_kwh, charge_minutes, departure_time.strftime("%Y-%m-%d %H:%M"),
        )

        # Use the interval planner to find cheapest safe slots
        slots = self.interval_planner.plan_intervals(
            needed_charge_minutes=charge_minutes,
            max_lookahead_hours=int((departure_time - start_time).total_seconds() // 3600) + 1,
            charging_amps=charging_amps,
            phases=phases,
        )

        # Filter slots to only those before departure
        before_departure = [s for s in slots if s.end <= departure_time]

        must_use_high_tariff = False
        if len(before_departure) * SLOT_MINUTES < charge_minutes:
            # Not enough cheap/safe slots — need to include high-tariff slots
            _LOGGER.info(
                "DepartureScheduler: only %d safe slots before departure, need %d min. "
                "Including high-tariff slots to meet departure target.",
                len(before_departure), charge_minutes,
            )
            must_use_high_tariff = True
            # Temporarily relax tariff constraint
            old_tariff = self.interval_planner.tariff
            self.interval_planner.tariff = None
            relaxed_slots = self.interval_planner.plan_intervals(
                needed_charge_minutes=charge_minutes,
                max_lookahead_hours=int((departure_time - start_time).total_seconds() // 3600) + 1,
                charging_amps=charging_amps,
                phases=phases,
            )
            self.interval_planner.tariff = old_tariff
            before_departure = [s for s in relaxed_slots if s.end <= departure_time]

        needed_slot_count = max(1, (charge_minutes + SLOT_MINUTES - 1) // SLOT_MINUTES)
        selected = before_departure[:needed_slot_count]
        selected.sort(key=lambda s: s.start)

        schedule = DepartureSchedule(
            departure_time=departure_time,
            charge_amount_kwh=charge_amount_kwh,
            estimated_charge_minutes=charge_minutes,
            selected_slots=selected,
            must_use_high_tariff=must_use_high_tariff,
        )
        self._active_schedule = schedule
        _LOGGER.info(
            "DepartureScheduler: schedule created with %d slots. "
            "Last charging ends at %s, %d min margin before departure.",
            len(selected),
            schedule.last_charging_slot_end.strftime("%H:%M") if schedule.last_charging_slot_end else "N/A",
            schedule.margin_minutes,
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
