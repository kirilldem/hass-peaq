"""Switch platform for peaqev.

The charger enable/disable switch has been removed. Peaqev is always enabled.
Charging is gated by the tariff sensor, non-hours, peak-shaving thresholds,
and the interval planner — not by a manual enable/disable toggle.
"""
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=4)


async def async_setup_entry(
    hass: HomeAssistant, config_entry, async_add_entities
):  # pylint:disable=unused-argument
    """No switch entities — peaqev is always enabled."""
    return
