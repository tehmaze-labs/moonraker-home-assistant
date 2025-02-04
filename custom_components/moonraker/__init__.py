"""
Moonraker integration for Home Assistant
"""
import asyncio
from datetime import timedelta
import logging

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Config, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MoonrakerApiClient
from .const import CONF_URL, DOMAIN, HOSTNAME, OBJ, PLATFORMS
from .sensor import SENSORS

SCAN_INTERVAL = timedelta(seconds=30)
TIMEOUT = 10

_LOGGER = logging.getLogger(__name__)

_LOGGER.debug("loading moonraker init")


async def async_setup(_hass: HomeAssistant, _config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})

    url = entry.data.get(CONF_URL)

    api = MoonrakerApiClient(url, async_get_clientsession(hass, verify_ssl=False))

    await api.start()

    try:
        async with async_timeout.timeout(TIMEOUT):
            printer_info = await api.client.call_method("printer.info")
            _LOGGER.debug(printer_info)
            api_device_name = printer_info[HOSTNAME]
    except Exception as exc:
        raise ConfigEntryNotReady from exc

    coordinator = MoonrakerDataUpdateCoordinator(
        hass, client=api, config_entry=entry, api_device_name=api_device_name
    )

    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator
    for platform in PLATFORMS:
        if entry.options.get(platform, True):
            coordinator.platforms.append(platform)
            hass.async_add_job(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


class MoonrakerDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MoonrakerApiClient,
        config_entry: ConfigEntry,
        api_device_name: str,
    ) -> None:
        """Initialize."""
        self.moonraker = client
        self.platforms = []
        self.hass = hass
        self.config_entry = config_entry
        self.api_device_name = api_device_name
        config_entry.title = api_device_name
        self.query_obj = {OBJ: {}}
        self.load_all_sensor_data()

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)

    async def _async_update_data(self):
        """Update data via library."""
        query = await self._async_fetch_data("printer.objects.query", self.query_obj)
        info = await self._async_fetch_data("printer.info", None)
        thumbnail = await self._async_get_thumbnail(
            query["status"]["print_stats"]["filename"]
        )
        return {**query, **{"printer.info": info}, **thumbnail}

    async def _async_get_thumbnail(self, gcode_filename):
        if gcode_filename is None or gcode_filename == "":
            return {"thumbnails": None}
        query_object = {"filename": gcode_filename}
        gcode = await self._async_fetch_data("server.files.metadata", query_object)
        return {
            "thumbnails": gcode["thumbnails"][len(gcode["thumbnails"]) - 1][
                "relative_path"
            ]
        }

    async def _async_fetch_data(self, query_path, query_object):
        if not self.moonraker.client.is_connected:
            _LOGGER.warning("connection to moonraker down, restarting")
            await self.moonraker.start()
        try:
            if query_object is None:
                result = await self.moonraker.client.call_method(query_path)
            else:
                result = await self.moonraker.client.call_method(
                    query_path, **query_object
                )
            _LOGGER.debug(result)
            return result
        except Exception as exception:
            raise UpdateFailed() from exception

    async def async_get_cameras(self):
        """Return list of cameras"""
        return await self._async_fetch_data("server.webcams.list", None)

    def load_all_sensor_data(self):
        """pre loading all sensor data, so we can poll the right object"""
        for sensor in SENSORS:
            for subscriptions in sensor.subscriptions:
                self.add_query_objects(subscriptions[0], subscriptions[1])

    def add_query_objects(self, query_object: str, result_key: str):
        """Building the list of object we want to retreive from the server"""
        if query_object not in self.query_obj[OBJ]:
            self.query_obj[OBJ][query_object] = []
        if result_key not in self.query_obj[OBJ][query_object]:
            self.query_obj[OBJ][query_object].append(result_key)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in coordinator.platforms
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
