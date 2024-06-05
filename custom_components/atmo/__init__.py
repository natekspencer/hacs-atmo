"""The Atmo integration."""

from __future__ import annotations

import logging

from sensor_state_data import SensorUpdate

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_PLANETWATCH, CONF_POLLING, DOMAIN
from .pyatmo import AtmoBluetoothDeviceData

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Atmo device from a config entry."""
    address = entry.unique_id
    assert address is not None
    data = AtmoBluetoothDeviceData(hass)

    polling_enabled = entry.options.get(CONF_POLLING, False)
    if entry.options.get(CONF_PLANETWATCH, False):
        data.planetwatch_sensor = None

    def needs_poll_method(  # pylint: disable=unused-argument
        svc_info: BluetoothServiceInfoBleak, last_poll: float | None
    ) -> bool:
        return polling_enabled

    async def poll_method(svc_info: BluetoothServiceInfoBleak) -> SensorUpdate:
        # Make sure the device we have is one that we can connect with
        # in case it's coming from a passive scanner
        if svc_info.connectable:
            connectable_device = svc_info.device
        elif device := async_ble_device_from_address(
            hass, svc_info.device.address, True
        ):
            connectable_device = device
        else:
            # We have no bluetooth controller that is in range of the device to poll it
            raise RuntimeError(
                f"No connectable device found for {svc_info.device.address}"
            )
        return await data.async_poll(connectable_device)

    coordinator = hass.data.setdefault(DOMAIN, {})[entry.entry_id] = (
        ActiveBluetoothProcessorCoordinator(
            hass,
            _LOGGER,
            address=address,
            mode=BluetoothScanningMode.PASSIVE,
            update_method=data.update,
            needs_poll_method=needs_poll_method,
            poll_method=poll_method,
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(
        coordinator.async_start()
    )  # only start after all platforms have had a chance to subscribe

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
