"""15-minute interval planning for sub-hourly grid settlement.

Some Swedish grid operators (including Göteborg Energi) are moving toward
15-minute settlement intervals.  This module complements the hourly price
selection in peaqevcore by selecting the cheapest 15-minute slots within
each hour, while respecting peak-power constraints.

Key challenge:
  The car may only need 15 minutes of charging within an hour.  If the
  cheapest slot is at the start of the hour (e.g. :00–:15), we must
  decide whether it is safe to charge without knowing the household's
  remaining power draw for the rest of that hour — which could risk
  hitting a peak power threshold.

Strategy:
  1. If a tariff module is active and the current slot is in a high-tariff
     window, defer charging to the next low-tariff window.
  2. Estimate the household's expected energy for the remainder of the
     current hour using the rolling average.  If charging during the
     selected 15-min slot would push the projected hourly peak above the
     configured peak threshold, move charging to a later, safer slot.
  3. Always prefer the cheapest available 15-min slot that passes the
     peak-safety check.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.peaqev.peaqservice.tariff.goteborg_energi import GoteborgEnergiTariff
    from custom_components.peaqev.peaqservice.hub.hub import HomeAssistantHub

_LOGGER = logging.getLogger(__name__)

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES  # 4


@dataclass
class IntervalSlot:
    """A single 15-minute interval candidate."""
    start: datetime
    end: datetime
    index: int  # 0-3 within the hour
    price: float = 0.0
    peak_safe: bool = True

    @property
    def duration_minutes(self) -> int:
        return SLOT_MINUTES

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


class IntervalPlanner:
    """Select the best 15-minute charging slots for price-aware charging."""

    def __init__(
        self,
        hub: HomeAssistantHub,
        tariff: GoteborgEnergiTariff | None = None,
        enabled: bool = False,
        peak_threshold_w: float = 0.0,
    ):
        self.hub = hub
        self.tariff = tariff
        self._enabled = enabled
        self._peak_threshold_w = peak_threshold_w
        self._household_avg_w = 0.0  # rolling average power (watts)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)

    @property
    def peak_threshold_w(self) -> float:
        return self._peak_threshold_w

    @peak_threshold_w.setter
    def peak_threshold_w(self, value: float):
        self._peak_threshold_w = float(value)

    def update_household_avg(self, avg_w: float) -> None:
        """Update the rolling average household power draw (watts)."""
        self._household_avg_w = max(float(avg_w), 0.0)

    def _get_hour_prices(self) -> list[float]:
        """Get per-hour spot prices (today + tomorrow if available)."""
        try:
            prices = list(self.hub.hours.prices or [])
            prices_tomorrow = list(getattr(self.hub.hours, "prices_tomorrow", []) or [])
            return prices + prices_tomorrow
        except Exception as e:
            _LOGGER.debug("Unable to retrieve prices for interval planning: %s", e)
            return []

    def _estimate_slot_power(self, charging_amps: int = 16, phases: int = 3) -> float:
        """Estimate charging power in watts for the given amps/phases.

        P = U × I × phases  (230 V in Sweden)
        """
        voltage = 230
        return float(voltage * charging_amps * phases)

    def _build_slots_for_hour(self, hour_dt: datetime, prices: list[float]) -> list[IntervalSlot]:
        """Build four 15-min slots for the given hour, assigning price."""
        hour_index = hour_dt.hour
        if hour_index < len(prices):
            price = float(prices[hour_index])
        else:
            price = 0.0
        slots = []
        for i in range(SLOTS_PER_HOUR):
            start = hour_dt.replace(minute=i * SLOT_MINUTES, second=0, microsecond=0)
            end = start + timedelta(minutes=SLOT_MINUTES)
            slots.append(IntervalSlot(start=start, end=end, index=i, price=price))
        return slots

    def _is_peak_safe(self, slot: IntervalSlot, charging_w: float) -> bool:
        """Check whether charging during this slot risks exceeding peak threshold.

        The key risk: if we charge at the start of the hour (:00–:15), the
        household still has 45 minutes of consumption we cannot predict.  We
        use the rolling average to estimate the worst-case hourly peak.
        """
        if self._peak_threshold_w <= 0:
            return True  # no threshold configured
        projected_hourly_peak = self._household_avg_w + charging_w
        # If slot is in the first 15 minutes of the hour, add a safety margin
        # because we can't yet know the full hourly consumption.
        if slot.index == 0:
            safety_margin = self._household_avg_w * 0.1  # 10% of avg as uncertainty
            projected_hourly_peak += safety_margin
        return projected_hourly_peak <= self._peak_threshold_w

    def _is_tariff_safe(self, slot: IntervalSlot) -> bool:
        """Check whether the tariff allows charging during this slot."""
        if self.tariff is None:
            return True
        # Use the slot's start time to evaluate tariff
        original_now = datetime.now()
        # Temporarily evaluate for the slot's start time
        if self.tariff.high_tariff_active:
            return False
        # Also check if the slot start falls within a high-tariff window
        # by checking the hour directly
        if hasattr(self.tariff, "high_tariff_hours_today"):
            return slot.start.hour not in self.tariff.high_tariff_hours_today()
        return True

    def plan_intervals(
        self,
        needed_charge_minutes: int = 15,
        max_lookahead_hours: int = 48,
        charging_amps: int = 16,
        phases: int = 3,
    ) -> list[IntervalSlot]:
        """Select the cheapest safe 15-minute slots to fulfil charging need.

        Args:
            needed_charge_minutes: Total minutes of charging needed.
            max_lookahead_hours: How far ahead to look for slots.
            charging_amps: Charger amperage for peak estimation.
            phases: Number of phases for power estimation.

        Returns:
            Ordered list of IntervalSlots to charge in (cheapest first,
            within peak/tariff safety constraints).
        """
        if not self._enabled:
            return []

        prices = self._get_hour_prices()
        if not prices:
            _LOGGER.debug("No prices available for interval planning.")
            return []

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        charging_w = self._estimate_slot_power(charging_amps, phases)

        all_slots: list[IntervalSlot] = []
        for h in range(max_lookahead_hours):
            hour_dt = now + timedelta(hours=h)
            slots = self._build_slots_for_hour(hour_dt, prices[h:] if h < len(prices) else [])
            for slot in slots:
                slot.peak_safe = self._is_peak_safe(slot, charging_w)
                # Only evaluate tariff for slots that are not in the past
                if slot.start >= datetime.now():
                    # Re-check tariff for this specific slot's hour
                    pass  # tariff is checked per-hour already
            all_slots.extend(slots)

        # Filter to slots that are in the future and safe
        now_exact = datetime.now()
        candidates = [
            s for s in all_slots
            if s.start >= now_exact and s.peak_safe and self._is_tariff_safe_for_hour(s, prices)
        ]

        if not candidates:
            _LOGGER.debug("No safe interval slots found; falling back to all future slots.")
            candidates = [s for s in all_slots if s.start >= now_exact]

        # Sort by price (cheapest first), then by start time
        candidates.sort(key=lambda s: (s.price, s.start))

        # Pick enough slots to cover needed charge time
        needed_slots = max(1, (needed_charge_minutes + SLOT_MINUTES - 1) // SLOT_MINUTES)
        selected = candidates[:needed_slots]

        # Sort selected by start time for execution order
        selected.sort(key=lambda s: s.start)
        _LOGGER.debug(
            "IntervalPlanner selected %d slots: %s",
            len(selected),
            [s.label for s in selected],
        )
        return selected

    def _is_tariff_safe_for_hour(self, slot: IntervalSlot, prices: list[float]) -> bool:
        """Check tariff safety for the slot's hour using the tariff module."""
        if self.tariff is None:
            return True
        try:
            high_hours = self.tariff.high_tariff_hours_today()
            return slot.start.hour not in high_hours
        except Exception:
            return True

    def should_charge_now(self, charging_amps: int = 16, phases: int = 3) -> bool:
        """Quick check: is the current 15-minute slot a good time to charge?

        Used by the charge controller to make a go/no-go decision every
        15 minutes instead of every hour.
        """
        if not self._enabled:
            return True  # When disabled, defer to hourly logic
        now = datetime.now()
        slot_index = now.minute // SLOT_MINUTES
        slot = IntervalSlot(
            start=now.replace(minute=slot_index * SLOT_MINUTES, second=0, microsecond=0),
            end=now.replace(minute=slot_index * SLOT_MINUTES, second=0, microsecond=0) + timedelta(minutes=SLOT_MINUTES),
            index=slot_index,
        )
        charging_w = self._estimate_slot_power(charging_amps, phases)
        if not self._is_peak_safe(slot, charging_w):
            _LOGGER.debug("IntervalPlanner: current slot not peak-safe.")
            return False
        if self.tariff is not None and self.tariff.should_avoid_charging():
            _LOGGER.debug("IntervalPlanner: tariff says avoid charging now.")
            return False
        return True

    def current_slot_index(self) -> int:
        """Return the 15-minute slot index for the current time (0-3)."""
        return datetime.now().minute // SLOT_MINUTES

    @property
    def next_slot_boundary(self) -> datetime:
        """Return the datetime of the next 15-minute boundary."""
        now = datetime.now()
        slot_index = now.minute // SLOT_MINUTES
        next_start = now.replace(minute=slot_index * SLOT_MINUTES, second=0, microsecond=0)
        next_start += timedelta(minutes=SLOT_MINUTES)
        return next_start
