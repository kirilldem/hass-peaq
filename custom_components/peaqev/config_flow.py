"""Config flow for Peaq integration."""
from __future__ import annotations

import logging
from typing import Any, Optional

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from custom_components.peaqev.configflow.config_flow_helpers import \
    async_set_startpeak_dict
from custom_components.peaqev.configflow.config_flow_schemas import (
    CHARGER_DETAILS_SCHEMA, CHARGER_SCHEMA, DEPARTURE_SCHEDULING_SCHEMA,
    HOURS_SCHEMA, INTERVAL_PLANNING_SCHEMA, MONTHS_SCHEMA,
    OUTLET_DETAILS_SCHEMA, PRICEAWARE_HOURS_SCHEMA, PRICEAWARE_SCHEMA,
    SIMULATION_SCHEMA,
    SENSOR_SCHEMA, TARIFF_SCHEMA, TYPE_SCHEMA)
from custom_components.peaqev.configflow.config_flow_validation import (
    ConfigFlowValidation, FaultyPriceSensor)
from custom_components.peaqev.peaqservice.powertools.power_canary.const import \
    FUSES_LIST
from custom_components.peaqev.peaqservice.tariff.goteborg_energi import \
    DEFAULT_SENSOR as DEFAULT_GE_SENSOR
from custom_components.peaqev.peaqservice.util.constants import (
    CAUTIONHOURTYPE_NAMES, SPOTPRICE_VALUETYPES, TYPELITE, CautionHourType)

from .const import DOMAIN  # pylint:disable=unused-import
from .peaqservice.chargertypes.models.chargertypes_enum import ChargerType

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    OPTIONS = 'options'
    data: Optional[dict[str, Any]]
    info: Optional[dict[str, Any]]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Invoked when a user initiates a flow via the user interface."""
        errors = {}
        if user_input is not None:
            self.data = user_input
            if self.data['peaqevtype'] == TYPELITE:
                self.info = {'title': TYPELITE}
                return await self.async_step_charger()
            return await self.async_step_sensor()

        return self.async_show_form(step_id='user', data_schema=TYPE_SCHEMA, errors=errors, last_step=False)

    async def async_step_sensor(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self.info = await ConfigFlowValidation.validate_input_first(user_input)
                await ConfigFlowValidation.validate_power_sensor(self.hass, user_input['name'])
            except ValueError:
                errors['base'] = 'invalid_powersensor'
            if not errors:
                self.data.update(user_input)
                return await self.async_step_charger()

        return self.async_show_form(
            step_id='sensor', data_schema=SENSOR_SCHEMA, errors=errors, last_step=False
        )

    async def async_step_charger(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self.data.update(user_input)
                if self.data['chargertype'] == ChargerType.Outlet.value:
                    return await self.async_step_outletdetails()
                return await self.async_step_chargerdetails()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception('Unexpected exception')
                errors['base'] = 'unknown'

        return self.async_show_form(
            step_id='charger',
            data_schema=CHARGER_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_chargerdetails(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_priceaware()

        return self.async_show_form(
            step_id='chargerdetails',
            data_schema=CHARGER_DETAILS_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_outletdetails(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_priceaware()

        return self.async_show_form(
            step_id='outletdetails',
            data_schema=OUTLET_DETAILS_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_priceaware(self, user_input=None):
        errors = {}
        if user_input is not None:
            _custom_sensor = user_input.get('custom_price_sensor', None)
            if _custom_sensor and len(_custom_sensor) > 2:
                try:
                    await ConfigFlowValidation.validate_price_sensor(self.hass, _custom_sensor)
                except (ValueError, FaultyPriceSensor) as e:
                    _LOGGER.debug('Price sensor validation failed: %s', e)
                    errors['base'] = 'invalid_pricesensor'
            else:
                user_input['custom_price_sensor'] = ''
            if not errors:
                self.data.update(user_input)
                if self.data['priceaware'] is False:
                    return await self.async_step_hours()
                return await self.async_step_priceaware_hours()

        return self.async_show_form(
            step_id='priceaware',
            data_schema=PRICEAWARE_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_priceaware_hours(self, user_input=None):
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_months()

        return self.async_show_form(
            step_id='priceaware_hours',
            data_schema=PRICEAWARE_HOURS_SCHEMA,
            last_step=False,
        )

    async def async_step_hours(self, user_input=None):
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_months()

        return self.async_show_form(
            step_id='hours',
            data_schema=HOURS_SCHEMA,
            last_step=False,
        )

    async def async_step_months(self, user_input=None):
        if user_input is not None:
            months_dict = await async_set_startpeak_dict(user_input)
            self.data['startpeaks'] = months_dict
            self.data['use_peak_history'] = user_input.get('use_peak_history', False)
            return await self.async_step_tariff()

        return self.async_show_form(
            step_id='months',
            data_schema=MONTHS_SCHEMA,
            last_step=False,
        )

    async def async_step_tariff(self, user_input=None):
        """Göteborg Energi tariff settings"""
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_interval_planning()

        return self.async_show_form(
            step_id='tariff',
            data_schema=TARIFF_SCHEMA,
            last_step=False,
        )

    async def async_step_interval_planning(self, user_input=None):
        """15-minute interval planning settings"""
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_departure_scheduling()

        return self.async_show_form(
            step_id='interval_planning',
            data_schema=INTERVAL_PLANNING_SCHEMA,
            last_step=False,
        )

    async def async_step_departure_scheduling(self, user_input=None):
        """Departure scheduling settings"""
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_simulation()

        return self.async_show_form(
            step_id='departure_scheduling',
            data_schema=DEPARTURE_SCHEDULING_SCHEMA,
            last_step=False,
        )

    async def async_step_simulation(self, user_input=None):
        """Simulation mode settings"""
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_misc()

        return self.async_show_form(
            step_id='simulation',
            data_schema=SIMULATION_SCHEMA,
            last_step=False,
        )

    async def async_step_misc(self, user_input=None):
        """Misc options"""
        if user_input is not None:
            self.data['mains'] = user_input['mains']
            self.data['gainloss'] = user_input['gainloss']
            return self.async_create_entry(title=self.info['title'], data=self.data)

        schema = vol.Schema(
            {
                vol.Optional(
                    'mains',
                    default='',
                ):                                      vol.In(FUSES_LIST),
                vol.Optional('gainloss', default=True): cv.boolean,
            }
        )

        return self.async_show_form(step_id='misc', last_step=True, data_schema=schema)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        """Initialize options flow."""
        # In HA 2025.x+, config_entry is a read-only property on OptionsFlow.
        # Do NOT assign it via self.config_entry = config_entry.
        self.options = dict(config_entry.options)

    async def _get_existing_param(self, parameter: str, default_val: any):
        if parameter in self.config_entry.options.keys():
            return self.config_entry.options.get(parameter)
        if parameter in self.config_entry.data.keys():
            return self.config_entry.data.get(parameter)
        return default_val

    async def async_step_init(self, user_input=None):
        """Priceaware"""
        errors = {}
        if user_input is not None:
            if len(user_input['custom_price_sensor']) > 2:
                try:
                    await ConfigFlowValidation.validate_price_sensor(self.hass, user_input['custom_price_sensor'])
                except (ValueError, FaultyPriceSensor) as e:
                    _LOGGER.debug('Price sensor validation failed: %s', e)
                    errors['base'] = 'invalid_pricesensor'
            else:
                _LOGGER.info('Nulling Custom price sensor')
                user_input['custom_price_sensor'] = ''
            if not errors:
                self.options.update(user_input)
                if self.options['priceaware'] is False:
                    return await self.async_step_hours()
                return await self.async_step_sensor()

        _priceaware = await self._get_existing_param('priceaware', False)
        _custompricesensor = await self._get_existing_param('custom_price_sensor', '')
        _spotprice_type = await self._get_existing_param('spotprice_type', 'Auto')
        _topprice = await self._get_existing_param('absolute_top_price', 0)
        _minprice = await self._get_existing_param('min_priceaware_threshold_price', 0)
        _hourtype = await self._get_existing_param('cautionhour_type', CautionHourType.INTERMEDIATE.value)
        _dynamic_top_price = await self._get_existing_param('dynamic_top_price', False)
        _max_charge = await self._get_existing_param('max_charge', 0)

        return self.async_show_form(
            step_id='init',
            last_step=False,
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional('priceaware', default=_priceaware):                   cv.boolean,
                    vol.Optional(
                        'spotprice_type',
                        default=_spotprice_type,
                    ): vol.In(SPOTPRICE_VALUETYPES),
                    vol.Optional('custom_price_sensor', default=_custompricesensor):   cv.string,
                    vol.Optional('dynamic_top_price', default=_dynamic_top_price):     cv.boolean,
                    vol.Optional('absolute_top_price', default=_topprice):             cv.positive_float,
                    vol.Optional('min_priceaware_threshold_price', default=_minprice): cv.positive_float,
                    vol.Optional(
                        'cautionhour_type',
                        default=_hourtype,
                    ):                                                                 vol.In(CAUTIONHOURTYPE_NAMES),
                    vol.Optional('max_charge', default=_max_charge):                   cv.positive_float,
                }
            ),
        )

    async def async_step_sensor(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await ConfigFlowValidation.validate_power_sensor(self.hass, user_input['name'])
            except ValueError:
                errors['base'] = 'invalid_powersensor'
            if not errors:
                self.options.update(user_input)
            return await self.async_step_priceaware_hours()

        _powersensorname = await self._get_existing_param('name', '')
        _powersensorincludescar = await self._get_existing_param('powersensorincludescar', False)

        return self.async_show_form(
            step_id='sensor', data_schema=vol.Schema(
                {
                    vol.Optional('name', default=_powersensorname):                          cv.string,
                    vol.Optional('powersensorincludescar', default=_powersensorincludescar): cv.boolean,
                }), errors=errors, last_step=False
        )

    async def async_step_priceaware_hours(self, user_input=None):
        """Hours"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_months()

        _nonhours = await self._get_existing_param('priceaware_nonhours', list(range(0, 24)))

        return self.async_show_form(
            step_id='priceaware_hours',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('priceaware_nonhours', default=_nonhours): cv.multi_select(list(range(0, 24))),
                }
            ),
        )

    async def async_step_hours(self, user_input=None):
        """Hours"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_months()

        _nonhours = await self._get_existing_param('nonhours', list(range(0, 24)))
        _cautionhours = await self._get_existing_param('cautionhours', list(range(0, 24)))

        return self.async_show_form(
            step_id='hours',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('nonhours', default=_nonhours):         cv.multi_select(list(range(0, 24))),
                    vol.Optional('cautionhours', default=_cautionhours): cv.multi_select(list(range(0, 24))),
                }
            ),
        )

    async def async_step_months(self, user_input=None):
        """Months"""
        if user_input is not None:
            months_dict = await async_set_startpeak_dict(user_input)
            self.options['use_peak_history'] = user_input.get('use_peak_history', False)
            self.options['startpeaks'] = months_dict

            return await self.async_step_tariff()

        _defaultvalues = self.config_entry.options.get('startpeaks', self.config_entry.data.get('startpeaks'))
        _default_history = await self._get_existing_param('use_peak_history', False)
        defaultvalues = {float(k): v for (k, v) in _defaultvalues.items()}

        return self.async_show_form(
            step_id='months',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('jan', default=defaultvalues[1]):              cv.positive_float,
                    vol.Optional('feb', default=defaultvalues[2]):              cv.positive_float,
                    vol.Optional('mar', default=defaultvalues[3]):              cv.positive_float,
                    vol.Optional('apr', default=defaultvalues[4]):              cv.positive_float,
                    vol.Optional('may', default=defaultvalues[5]):              cv.positive_float,
                    vol.Optional('jun', default=defaultvalues[6]):              cv.positive_float,
                    vol.Optional('jul', default=defaultvalues[7]):              cv.positive_float,
                    vol.Optional('aug', default=defaultvalues[8]):              cv.positive_float,
                    vol.Optional('sep', default=defaultvalues[9]):              cv.positive_float,
                    vol.Optional('oct', default=defaultvalues[10]):             cv.positive_float,
                    vol.Optional('nov', default=defaultvalues[11]):             cv.positive_float,
                    vol.Optional('dec', default=defaultvalues[12]):             cv.positive_float,
                    vol.Optional('use_peak_history', default=_default_history): cv.boolean,
                }
            ),
        )

    async def async_step_tariff(self, user_input=None):
        """Göteborg Energi tariff options"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_interval_planning()

        _ge_enabled = await self._get_existing_param('ge_tariff_enabled', False)
        _ge_sensor = await self._get_existing_param('ge_tariff_sensor', DEFAULT_GE_SENSOR)
        _ge_fallback = await self._get_existing_param('ge_tariff_fallback', True)

        return self.async_show_form(
            step_id='tariff',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('ge_tariff_enabled', default=_ge_enabled):     cv.boolean,
                    vol.Optional('ge_tariff_sensor', default=_ge_sensor):       cv.string,
                    vol.Optional('ge_tariff_fallback', default=_ge_fallback):   cv.boolean,
                }
            ),
        )

    async def async_step_interval_planning(self, user_input=None):
        """15-minute interval planning options"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_departure_scheduling()

        _iv_enabled = await self._get_existing_param('interval_planning_enabled', False)

        return self.async_show_form(
            step_id='interval_planning',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('interval_planning_enabled', default=_iv_enabled): cv.boolean,
                }
            ),
        )

    async def async_step_departure_scheduling(self, user_input=None):
        """Departure scheduling options"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_simulation()

        _volvo_sensor = await self._get_existing_param('volvo_soc_sensor', '')
        _efficiency = await self._get_existing_param('charger_efficiency', 0.9)

        return self.async_show_form(
            step_id='departure_scheduling',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('volvo_soc_sensor', default=_volvo_sensor): cv.string,
                    vol.Optional('charger_efficiency', default=_efficiency): vol.All(
                        vol.Coerce(float), vol.Range(min=0.5, max=1.0)
                    ),
                }
            ),
        )

    async def async_step_simulation(self, user_input=None):
        """Simulation mode options"""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_misc()

        _sim_mode = await self._get_existing_param('simulation_mode', False)

        return self.async_show_form(
            step_id='simulation',
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional('simulation_mode', default=_sim_mode): cv.boolean,
                }
            ),
        )

    async def async_step_misc(self, user_input=None):
        """Misc options"""
        if user_input is not None:
            self.options['mains'] = user_input['mains']
            self.options['gainloss'] = user_input['gainloss']
            return self.async_create_entry(title='', data=self.options)

        mainsvalue = await self._get_existing_param('mains', '')
        gainloss = await self._get_existing_param('gainloss', True)

        schema = vol.Schema(
            {
                vol.Optional(
                    'mains',
                    default=mainsvalue,
                ):                                          vol.In(FUSES_LIST),
                vol.Optional('gainloss', default=gainloss): cv.boolean,
            }
        )

        return self.async_show_form(step_id='misc', last_step=True, data_schema=schema)
