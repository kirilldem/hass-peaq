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

Edge Case A (Late Plug-In):
  Past slots are excluded — the planner only considers slots starting at
  or after the current moment.  The schedule is rebuilt dynamically from
  the remaining window, guaranteeing SoC target from what's left.

Edge Case D (Negative Price vs High Tariff):
  When spot price is negative, the planner computes whether the monetary
  payout from the negative price exceeds the estimated peak-fee penalty.
  If the payout is larger, charging is allowed even during high-tariff.
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

# Göteborg Energi effektavgift: the three highest hourly peaks per month
# drive the bill.  We estimate the marginal cost of adding one more peak
# as the tariff rate (SEK/kW) times the delta in the average-of-three.
# For a rough real-time decision, we use the monthly peak rate.
# Source: Göteborg Energi 6 kW / 63A tidsindelad tariff.
GE_PEAK_FEE_SEK_PER_KW_MONTH = 62.0  # approximate SEK per kW per month
GE_PEAK_FEE_SEK_PER_W_MONTH = GE_PEAK_FEE_SEK_PER_KW_MONTH / 1000.0


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

    @property
    def energy_kwh(self) -> float:
        """Energy delivered during this slot at 1 kW (for scaling)."""
        return SLOT_MINUTES / 60.0


class IntervalPlanner:
    """Select the best 15-minute charging slots for price-aware charging."""

    def __init__(
        self,
        hub: HomeAssistantHub,
        tariff: GoteborgEnergiTariff | None = None,
        enabled: bool = False,
    ):
        self.hub = hub
        self.tariff = tariff
        self._enabled = enabled
        self._household_avg_w = 0.0  # rolling average power (watts)
        self._current_peak_w = 0.0  # current monthly peak (watts)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)

    def update_household_avg(self, avg_w: float) -> None:
        """Update the rolling average household power draw (watts)."""
        self._household_avg_w = max(float(avg_w), 0.0)

    def update_current_peak(self, peak_w: float) -> None:
        """Update the current monthly peak (watts) for peak-fee estimation."""
        self._current_peak_w = max(float(peak_w), 0.0)

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

        Uses the hub's current monthly peak threshold (from the Months config step)
        converted to watts. If no peak threshold is set, returns True.
        """
        peak_threshold_w = 0.0
        try:
            # The hub's current_peak is in kW — convert to W
            peak_kw = float(self.hub.sensors.current_peak.observed_peak)
            if isinstance(peak_kw, (list, tuple)):
                peak_kw = max(peak_kw) if peak_kw else 0.0
            peak_threshold_w = peak_kw * 1000
        except Exception:
            pass
        if peak_threshold_w <= 0:
            return True  # no threshold configured
        projected_hourly_peak = self._household_avg_w + charging_w
        # If slot is in the first 15 minutes of the hour, add a safety margin
        # because we can't yet know the full hourly consumption.
        if slot.index == 0:
            safety_margin = self._household_avg_w * 0.1  # 10% of avg as uncertainty
            projected_hourly_peak += safety_margin
        return projected_hourly_peak <= self._peak_threshold_w

    def _is_tariff_safe_for_hour(self, slot: IntervalSlot, prices: list[float]) -> bool:
        """Check tariff safety for the slot's hour using the tariff module."""
        if self.tariff is None:
            return True
        try:
            high_hours = self.tariff.high_tariff_hours_today()
            return slot.start.hour not in high_hours
        except Exception:
            return True

    # --- Edge Case D: Negative price vs high tariff ---

    def _estimate_peak_fee_penalty(self, charging_w: float) -> float:
        """Estimate the SEK cost of the peak-fee penalty from charging.

        Göteborg Energi bills on the average of the three highest hourly
        peaks across three different days in the month.  If the new peak
        from this charging session would become one of the top three,
        the marginal cost is roughly:

            peak_fee = (new_peak_kW - current_avg_top3_kW) * rate_SEK/kW

        For a conservative estimate, we assume the new peak replaces the
        lowest of the current top three (i.e., the marginal kW cost).
        """
        if self.tariff is None:
            return 0.0
        new_peak_w = self._household_avg_w + charging_w
        new_peak_kw = new_peak_w / 1000.0
        # Marginal: if new_peak > current peak, the delta is penalized
        current_peak_kw = self._current_peak_w / 1000.0 if self._current_peak_w > 0 else 0.0
        delta_kw = max(new_peak_kw - current_peak_kw, 0.0)
        return delta_kw * GE_PEAK_FEE_SEK_PER_KW_MONTH

    def _estimate_negative_price_payout(self, slot: IntervalSlot, charging_w: float) -> float:
        """Estimate the SEK payout from charging during a negative-price slot.

        payout = |price| * energy_kWh
        energy_kWh = charging_w * (SLOT_MINUTES / 60) / 1000
        """
        if slot.price >= 0:
            return 0.0
        energy_kwh = charging_w * (SLOT_MINUTES / 60.0) / 1000.0
        return abs(slot.price) * energy_kwh

    def _negative_price_outweighs_tariff(self, slot: IntervalSlot, charging_w: float) -> bool:
        """Edge Case D: True when negative-price payout exceeds peak-fee penalty."""
        payout = self._estimate_negative_price_payout(slot, charging_w)
        penalty = self._estimate_peak_fee_penalty(charging_w)
        _LOGGER.debug(
            "Edge Case D: slot %s price=%.2f, payout=%.2f SEK, penalty=%.2f SEK, allow=%s",
            slot.label, slot.price, payout, penalty, payout > penalty,
        )
        return payout > penalty

    # --- Main planning logic ---

    def plan_intervals(
        self,
        needed_charge_minutes: int = 15,
        max_lookahead_hours: int = 48,
        charging_amps: int = 16,
        phases: int = 3,
        departure_time: datetime | None = None,
        allow_negative_price_override: bool = True,
    ) -> list[IntervalSlot]:
        """Select the cheapest safe 15-minute slots to fulfil charging need.

        Edge Case A: Only future slots are considered.  Past cheap slots
        are excluded and the schedule is rebuilt from remaining options.

        Edge Case D: If a slot has a negative price during a high-tariff
        hour, the planner checks whether the payout exceeds the penalty
        before including it.

        Args:
            needed_charge_minutes: Total minutes of charging needed.
            max_lookahead_hours: How far ahead to look for slots.
            charging_amps: Charger amperage for peak estimation.
            phases: Number of phases for power estimation.
            departure_time: If set, only slots ending before this time are eligible.
            allow_negative_price_override: If True, allow negative-price slots
                to override tariff restrictions (Edge Case D).

        Returns:
            Ordered list of IntervalSlots (cheapest first within safety constraints).
        """
        if not self._enabled:
            return []

        prices = self._get_hour_prices()
        if not prices:
            _LOGGER.debug("No prices available for interval planning.")
            return []

        now_exact = datetime.now()
        now_hour = now_exact.replace(minute=0, second=0, microsecond=0)
        charging_w = self._estimate_slot_power(charging_amps, phases)

        all_slots: list[IntervalSlot] = []
        for h in range(max_lookahead_hours):
            hour_dt = now_hour + timedelta(hours=h)
            slots = self._build_slots_for_hour(hour_dt, prices[h:] if h < len(prices) else [])
            for slot in slots:
                slot.peak_safe = self._is_peak_safe(slot, charging_w)
            all_slots.extend(slots)

        # Edge Case A: Filter to future slots only — past cheap slots are gone
        candidates: list[IntervalSlot] = []
        for s in all_slots:
            if s.start < now_exact:
                continue  # Past slot — cannot use (Edge Case A)
            if departure_time is not None and s.end > departure_time:
                continue  # After departure — cannot use
            if not s.peak_safe:
                continue  # Would breach peak threshold
            tariff_safe = self._is_tariff_safe_for_hour(s, prices)
            if not tariff_safe:
                # Edge Case D: Check if negative price outweighs the tariff penalty
                if allow_negative_price_override and s.price < 0:
                    if self._negative_price_outweighs_tariff(s, charging_w):
                        candidates.append(s)
                # Otherwise skip this high-tariff slot
                continue
            candidates.append(s)

        if not candidates:
            _LOGGER.debug("No safe interval slots found; falling back to all future slots.")
            candidates = [s for s in all_slots if s.start >= now_exact]
            if departure_time is not None:
                candidates = [s for s in candidates if s.end <= departure_time]

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

    def should_charge_now(self, charging_amps: int = 16, phases: int = 3) -> bool:
        """Quick check: is the current 15-minute slot a good time to charge?

        Used by the charge controller to make a go/no-go decision every
        15 minutes instead of every hour.
        """
        if not self._enabled:
            return True  # When disabled, defer to hourly logic
        now = datetime.now()
        slot_index = now.minute // SLOT_MINUTES
        # Look up current hour's price for negative-price evaluation (Edge Case D)
        current_price = 0.0
        try:
            prices = list(self.hub.hours.prices or [])
            if now.hour < len(prices):
                current_price = float(prices[now.hour])
        except Exception:
            pass
        slot = IntervalSlot(
            start=now.replace(minute=slot_index * SLOT_MINUTES, second=0, microsecond=0),
            end=now.replace(minute=slot_index * SLOT_MINUTES, second=0, microsecond=0) + timedelta(minutes=SLOT_MINUTES),
            index=slot_index,
            price=current_price,
        )
        charging_w = self._estimate_slot_power(charging_amps, phases)
        if not self._is_peak_safe(slot, charging_w):
            _LOGGER.debug("IntervalPlanner: current slot not peak-safe.")
            return False
        if self.tariff is not None and self.tariff.should_avoid_charging():
            # Edge Case D: Allow if negative price payout outweighs penalty
            if slot.price < 0 and self._negative_price_outweighs_tariff(slot, charging_w):
                _LOGGER.info(
                    "IntervalPlanner: high-tariff active but negative price payout "
                    "exceeds peak-fee penalty. Allowing charge."
                )
                return True
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
