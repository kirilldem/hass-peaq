import logging
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

def defer_start(non_hours: list) -> bool:
    """Defer starting if next hour is a non-hour and minute is 50 or greater, to avoid short running times."""
    if (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0) in non_hours:
        if datetime.now().minute >= 50:
            return True
    return False


def should_defer_for_tariff(hub) -> bool:
    """Check if charging should be deferred due to an active high-tariff window.

    Returns True if the tariff module is active and indicates that charging
    should be avoided right now.
    """
    tariff = getattr(hub, 'tariff', None)
    if tariff is None:
        return False
    return tariff.should_avoid_charging()


def should_defer_for_interval(hub) -> bool:
    """Check if the 15-minute interval planner advises against charging now.

    Returns True if the interval planner is enabled and the current slot
    is not suitable for charging.
    """
    interval_planner = getattr(hub, 'interval_planner', None)
    if interval_planner is None or not interval_planner.enabled:
        return False
    return not interval_planner.should_charge_now()
