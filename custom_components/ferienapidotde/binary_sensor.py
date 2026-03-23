"""
Utilizes the api `ferien-api.de` to provide a binary sensor to indicate if
today is a german vacational day or not - based on your configured state.

For more details about this platform, please refer to the documentation at
https://github.com/HazardDede/home-assistant-ferienapidotde
"""

import json
import logging
import os
from datetime import datetime, timedelta

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_NAME
from homeassistant.exceptions import PlatformNotReady
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)

ALL_STATE_CODES = [
    "BW",
    "BY",
    "BE",
    "BB",
    "HB",
    "HH",
    "HE",
    "MV",
    "NI",
    "NW",
    "RP",
    "SL",
    "SN",
    "ST",
    "SH",
    "TH",
]

ATTR_DAYS_OFFSET = "days_offset"
ATTR_START = "start"
ATTR_END = "end"
ATTR_NEXT_START = "next_start"
ATTR_NEXT_END = "next_end"
ATTR_VACATION_NAME = "vacation_name"

CONF_DAYS_OFFSET = "days_offset"
CONF_STATE = "state_code"

DEFAULT_DAYS_OFFSET = 0
DEFAULT_NAME = "Vacation Sensor"

ICON_OFF_DEFAULT = "mdi:calendar-remove"
ICON_ON_DEFAULT = "mdi:calendar-check"

# Only fetch fresh data from the API once per day.
MIN_TIME_BETWEEN_UPDATES = timedelta(hours=24)

# Cache is valid for 30 days - vacation schedules don't change frequently.
CACHE_TTL = timedelta(days=30)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_DAYS_OFFSET, default=DEFAULT_DAYS_OFFSET):
            vol.Coerce(int),
        vol.Required(CONF_STATE): vol.In(ALL_STATE_CODES),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)

# Check every 6 hours whether an update is due; actual API calls are throttled.
SCAN_INTERVAL = timedelta(hours=6)


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info=None
):
    """Setups the ferienapidotde platform."""
    _, _ = hass, discovery_info  # Fake usage
    days_offset = config.get(CONF_DAYS_OFFSET)
    state_code = config.get(CONF_STATE)
    name = config.get(CONF_NAME)

    data_object = VacationData(hass, state_code)
    await data_object.async_init()

    if data_object.data is None:
        # No valid cache available - need an initial fetch from the API.
        try:
            await data_object.async_update()
        except Exception as ex:
            import traceback
            _LOGGER.warning(traceback.format_exc())
            raise PlatformNotReady() from ex

    async_add_entities([VacationSensor(name, days_offset, data_object)], True)


class VacationSensor(BinarySensorEntity):
    """Implementation of the vacation sensor."""

    def __init__(self, name, days_offset, data_object):
        self._name = name
        self._days_offset = days_offset
        self.data_object = data_object
        self._state = None
        self._state_attrs = {}

    @property
    def name(self):
        """Returns the name of the sensor."""
        return self._name

    @property
    def icon(self):
        """Return the icon for the frontend."""
        return ICON_ON_DEFAULT if self.is_on else ICON_OFF_DEFAULT

    @property
    def is_on(self):
        """Returns the state of the device."""
        return self._state

    @property
    def device_state_attributes(self):
        """Returns the state attributes of this device. This is deprecated but
        we keep it for backwards compatibility."""
        return self._state_attrs

    @property
    def extra_state_attributes(self):
        """Returns the state attributes of this device."""
        return self._state_attrs

    async def async_update(self):
        """Updates the state and state attributes."""
        import ferien

        await self.data_object.async_update()
        vacs = self.data_object.data
        dt_offset = datetime.now() + timedelta(days=self._days_offset)

        cur = ferien.current_vacation(vacs=vacs, dt=dt_offset)
        if cur is None:
            self._state = False
            nextvac = ferien.next_vacation(vacs=vacs, dt=dt_offset)
            if nextvac is None:
                self._state_attrs = {}
            else:
                self._state_attrs = {
                    ATTR_NEXT_START: nextvac.start.strftime("%Y-%m-%d"),
                    ATTR_NEXT_END: nextvac.end.strftime("%Y-%m-%d"),
                    ATTR_VACATION_NAME: nextvac.name,
                    ATTR_DAYS_OFFSET: self._days_offset
                }
        else:
            self._state = True
            self._state_attrs = {
                ATTR_START: cur.start.strftime("%Y-%m-%d"),
                ATTR_END: cur.end.strftime("%Y-%m-%d"),
                ATTR_VACATION_NAME: cur.name,
                ATTR_DAYS_OFFSET: self._days_offset
            }


class VacationData:
    """Class for handling data retrieval with local caching."""

    def __init__(self, hass, state_code):
        """Initializer. Cache loading is deferred to async_init."""
        self.hass = hass
        self.state_code = str(state_code)
        self.data = None

    async def async_init(self):
        """Load cached data in an executor to avoid blocking the event loop."""
        self.data = await self.hass.async_add_executor_job(self._load_from_cache)

    def _cache_path(self):
        return self.hass.config.path(
            "ferienapidotde_{}.json".format(self.state_code)
        )

    def _load_from_cache(self):
        """Returns cached vacation data if present and not expired, else None."""
        try:
            path = self._cache_path()
            if not os.path.exists(path):
                return None
            with open(path) as f:
                cached = json.load(f)
            cached_at = datetime.fromisoformat(cached["cached_at"])
            if datetime.now() - cached_at > CACHE_TTL:
                _LOGGER.debug("Cache for %s is expired", self.state_code)
                return None
            from ferien.model import Vacation  # pylint: disable=import-outside-toplevel
            data = [Vacation.from_dict(v) for v in cached["vacations"]]
            _LOGGER.debug(
                "Loaded %d vacations for %s from cache (cached at %s)",
                len(data), self.state_code, cached_at.strftime("%Y-%m-%d %H:%M")
            )
            return data
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Could not load cache for %s: %s", self.state_code, ex)
            return None

    def _save_to_cache(self, data):
        """Persists vacation data to a local JSON file."""
        try:
            serialized = [
                {
                    "start": v.start.strftime("%Y-%m-%d"),
                    "end": v.end.strftime("%Y-%m-%d"),
                    "year": v.year,
                    "stateCode": v.state_code,
                    "name": v.name,
                    "slug": v.slug,
                }
                for v in data
            ]
            with open(self._cache_path(), "w") as f:
                json.dump(
                    {"cached_at": datetime.now().isoformat(), "vacations": serialized},
                    f,
                    indent=2
                )
            _LOGGER.debug("Saved %d vacations for %s to cache", len(data), self.state_code)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not save cache for %s: %s", self.state_code, ex)

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Updates the publicly available data container."""
        try:
            import ferien  # pylint: disable=import-outside-toplevel
            _LOGGER.debug(
                "Retrieving data from ferien-api.de for %s",
                self.state_code
            )
            self.data = await self.hass.async_add_executor_job(
                ferien.state_vacations, self.state_code
            )
            await self.hass.async_add_executor_job(self._save_to_cache, self.data)
        except Exception as ex:  # pylint: disable=broad-except
            if self.data is None:
                raise
            _LOGGER.warning(
                "Failed to update the vacation data for %s (%s). Re-using cached state.",
                self.state_code, ex
            )
