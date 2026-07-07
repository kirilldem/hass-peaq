"""Simulation mode entity overrides for Docker-based integration testing.

When Simulation Mode is enabled, the integration swaps production hardware
sensor entity IDs for predefined dummy/template helpers, allowing full
end-to-end testing without real Easee charger, Volvo, or smart meter hardware.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# Fixed entity IDs for the UI Simulator test environment
SIMULATION_ENTITIES = {
    "gothenburg_tariff_sensor": "binary_sensor.goteborg_energi_nat_ab_tidsindelad_6_kw_max_63a_high_tariff_active",
    "smart_meter_power": "sensor.smart_meter_active_power",
    "spot_price": "sensor.current_spot_price",
    "volvo_battery_level": "sensor.volvo_battery_level",
    "target_soc_slider": "input_number.dummy_target_soc",
    "departure_time_picker": "input_datetime.dummy_departure_time",
    "easee_charger_active": "input_boolean.dummy_charger_active",
}


@dataclass
class SimulationConfig:
    """Configuration for simulation mode."""
    enabled: bool = False

    @property
    def gothenburg_tariff_sensor(self) -> str:
        return SIMULATION_ENTITIES["gothenburg_tariff_sensor"]

    @property
    def smart_meter_power(self) -> str:
        return SIMULATION_ENTITIES["smart_meter_power"]

    @property
    def spot_price(self) -> str:
        return SIMULATION_ENTITIES["spot_price"]

    @property
    def volvo_battery_level(self) -> str:
        return SIMULATION_ENTITIES["volvo_battery_level"]

    @property
    def target_soc_slider(self) -> str:
        return SIMULATION_ENTITIES["target_soc_slider"]

    @property
    def departure_time_picker(self) -> str:
        return SIMULATION_ENTITIES["departure_time_picker"]

    @property
    def easee_charger_active(self) -> str:
        return SIMULATION_ENTITIES["easee_charger_active"]

    def get_entity_overrides(self) -> dict[str, str]:
        """Return a mapping of config keys to simulation entity IDs."""
        return dict(SIMULATION_ENTITIES)


class SimulationEntityMapper:
    """Maps production entity IDs to simulation entity IDs when simulation is active."""

    def __init__(self, config: SimulationConfig):
        self._config = config

    @property
    def active(self) -> bool:
        return self._config.enabled

    def resolve_tariff_sensor(self, production_sensor: str | None) -> str:
        if self._config.enabled:
            return self._config.gothenburg_tariff_sensor
        return production_sensor or ""

    def resolve_volvo_soc_sensor(self, production_sensor: str | None) -> str:
        if self._config.enabled:
            return self._config.volvo_battery_level
        return production_sensor or ""

    def resolve_spot_price_sensor(self, production_sensor: str | None) -> str:
        if self._config.enabled:
            return self._config.spot_price
        return production_sensor or ""

    def resolve_power_sensor(self, production_sensor: str | None) -> str:
        if self._config.enabled:
            return self._config.smart_meter_power
        return production_sensor or ""

    def resolve_charger_entity(self, production_entity: str | None) -> str:
        if self._config.enabled:
            return self._config.easee_charger_active
        return production_entity or ""

    def resolve_departure_time(self) -> str:
        """Return the input_datetime entity for departure time in simulation."""
        return self._config.departure_time_picker

    def resolve_target_soc(self) -> str:
        """Return the input_number entity for target SoC in simulation."""
        return self._config.target_soc_slider
