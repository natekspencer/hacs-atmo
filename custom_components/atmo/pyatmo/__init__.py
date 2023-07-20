"""Atmo module."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
from struct import unpack

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import (
    BinarySensorDeviceClass,
    SensorDeviceClass,
    SensorLibrary,
    SensorUpdate,
    Units,
)
from sensor_state_data.description import BaseSensorDescription

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import now as dt_now

from .helpers import calc_aqs, decode_info, decode_pms

_LOGGER = logging.getLogger(__name__)

ERROR = "error"

CHARACTERISTIC_VOC = "db450002-8e9a-4818-add7-6ed94a328ab4"
CHARACTERISTIC_BME280 = "db450003-8e9a-4818-add7-6ed94a328ab4"
CHARACTERISTIC_STATUS = "db450004-8e9a-4818-add7-6ed94a328ab4"
CHARACTERISTIC_PM = "db450005-8e9a-4818-add7-6ed94a328ab4"


PRESSURE__PA = BaseSensorDescription(
    device_class=SensorDeviceClass.PRESSURE,
    native_unit_of_measurement=Units.PRESSURE_PA,
)
VOLATILE_ORGANIC_COMPOUNDS__CONCENTRATION_PARTS_PER_BILLION = BaseSensorDescription(
    device_class=SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
    native_unit_of_measurement=Units.CONCENTRATION_PARTS_PER_BILLION,
)

ATMO_MANUFACTURER_ID = 0xFFFF
PLANETWATCH_CHECK_RATE_LIMIT = timedelta(minutes=5)
PLANETWATCH_UPDATE_RATE_LIMIT = timedelta(seconds=20)


class AtmoDataMixin:
    """Atmo data mixin."""

    battery: int | None = None
    bonded: bool = False
    charging: bool = False
    error: bool = False
    humidity: int | None = None
    pm_on: bool = False
    pm1: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    pressure: int | None = None
    temperature: float | None = None
    timer: bool = False
    voc: int | None = None
    voc_ready: bool = False

    planetwatch_sensor: bool | None = False
    planetwatch_checked: datetime | None = None
    planetwatch_updated: datetime | None = None


class AtmoBluetoothDeviceData(BluetoothData, AtmoDataMixin):
    """Data for Atmo BLE sensors."""

    def __init__(self, hass: HomeAssistant | None = None) -> None:
        """Initialize the class."""
        super().__init__()
        if hass:
            self.latitude = hass.config.latitude
            self.longitude = hass.config.longitude
            self.elevation = hass.config.elevation
            self.client = async_get_clientsession(hass)
        self.task: asyncio.Task | None = None

    def _start_update(self, data: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        address = data.address
        _LOGGER.debug("%s: Parsing Atmo BLE advertisement data", address)
        manufacturer_data = data.manufacturer_data
        service_uuids = data.service_uuids
        local_name = data.name

        for mfr_id, mfr_data in manufacturer_data.items():
            self._process_mfr_data(address, local_name, mfr_id, mfr_data, service_uuids)

        if self.get_device_name() is None:
            return

        if (aqs := self._update_aqs()) is not None:
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(
                    self.update_planetwatch_sensor_data(address, aqs)
                )

    def _process_mfr_data(
        self,
        address: str,
        local_name: str,
        mfr_id: int,
        data: bytes,
        service_uuids: list[str],
    ) -> None:
        """Parser for Atmo sensors."""
        if mfr_id != ATMO_MANUFACTURER_ID:
            return

        _LOGGER.debug("%s: Parsing Atmo sensor: %s %s", address, mfr_id, data.hex())
        self.set_device_manufacturer("Atmo")
        self.set_device_type("Atmotube PRO")
        self.set_device_name(f"Atmotube PRO {address}")
        msg_length = len(data)

        if msg_length == 12:
            # unpack(">h2sbbibb", data)
            self.voc = int.from_bytes(data[:2], byteorder="big")
            device_id = data[2:4].hex()
            self.humidity = data[4]
            self.temperature = int.from_bytes(data[5:6], byteorder="big", signed=True)
            self.pressure = int.from_bytes(data[6:10], byteorder="big")
            info_byte = data[10]
            (
                self.pm_on,
                self.error,
                self.bonded,
                self.charging,
                self.timer,
                _,
                self.voc_ready,
            ) = decode_info(info_byte)
            self.battery = data[11]

            _LOGGER.debug(
                "%s: voc: %s, device_id: %s, humidity: %s, temperature: %s, pressure: %s, info_byte: %s, battery: %s, pm_on: %s, error: %s, bonded: %s, charging: %s, timer: %s, voc_ready: %s",
                address,
                self.voc,
                device_id,
                self.humidity,
                self.temperature,
                self.pressure,
                f"{info_byte:b}",
                self.battery,
                self.pm_on,
                self.error,
                self.bonded,
                self.charging,
                self.timer,
                self.voc_ready,
            )

            self.update_predefined_sensor(
                VOLATILE_ORGANIC_COMPOUNDS__CONCENTRATION_PARTS_PER_BILLION,
                self.voc,
                name="Volatile organic compounds",
            )
            self.update_predefined_sensor(
                SensorLibrary.HUMIDITY__PERCENTAGE, self.humidity
            )
            self.update_predefined_sensor(
                SensorLibrary.TEMPERATURE__CELSIUS, self.temperature
            )
            self.update_predefined_sensor(PRESSURE__PA, self.pressure)
            self.update_predefined_sensor(
                SensorLibrary.BATTERY__PERCENTAGE, self.battery
            )
            self.update_predefined_binary_sensor(
                BinarySensorDeviceClass.BATTERY_CHARGING,
                self.charging,
                name="Battery charging",
            )

            return

        if msg_length == 9:
            pm1, pm25, pm10 = decode_pms(data, 3, 2)
            firmware = data[6:9].hex().upper()
            self.set_device_sw_version(firmware)

            _LOGGER.debug(
                "%s: pm1: %s, pm25: %s, pm10: %s, firmware: %s",
                address,
                pm1,
                pm25,
                pm10,
                firmware,
            )

            if None in (pm1, pm25, pm10):
                return  # PM sensor is off, so don't update values

            self.pm1, self.pm25, self.pm10 = (pm1, pm25, pm10)
            self.update_predefined_sensor(
                SensorLibrary.PM1__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                pm1,
                name="PM1",
            )
            self.update_predefined_sensor(
                SensorLibrary.PM25__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                pm25,
                name="PM2.5",
            )
            self.update_predefined_sensor(
                SensorLibrary.PM10__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                pm10,
                name="PM10",
            )

            return

        _LOGGER.debug(
            "%s: Unknown message received: local_name: %s, data: %s, service_uuids: %s, msg_length: %s",
            address,
            local_name,
            data.hex(),
            service_uuids,
            msg_length,
        )

    def _process_bme280_data(self, data: bytes, address: str) -> None:
        """Parse BME280 characteristic data."""
        self.humidity, temperature, self.pressure, temperature_extended = unpack(
            "<bbih", data
        )
        self.temperature = temperature_extended / 100
        _LOGGER.debug(
            "%s: BME280 data: %s, humidity: %s, temperature: %s, pressure: %s, temperature_extended: %s",
            address,
            data.hex(),
            self.humidity,
            temperature,
            self.pressure,
            self.temperature,
        )
        self.update_predefined_sensor(SensorLibrary.HUMIDITY__PERCENTAGE, self.humidity)
        self.update_predefined_sensor(
            SensorLibrary.TEMPERATURE__CELSIUS, self.temperature
        )
        self.update_predefined_sensor(PRESSURE__PA, self.pressure)

    def _process_pm_data(self, data: bytes, address: str) -> None:
        """Parse PM characteristic data."""
        pm1, pm25, pm10, pm4 = decode_pms(data, 4, 3)
        _LOGGER.debug(
            "%s: PM data: %s, pm1: %s, pm25: %s, pm10: %s, pm4: %s",
            address,
            data.hex(),
            pm1,
            pm25,
            pm10,
            pm4,
        )

        if None in (pm1, pm25, pm10):
            return  # PM sensor is off, so don't update values

        self.pm1, self.pm25, self.pm10 = (pm1, pm25, pm10)
        self.update_predefined_sensor(
            SensorLibrary.PM1__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER, pm1, name="PM1"
        )
        self.update_predefined_sensor(
            SensorLibrary.PM25__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
            pm25,
            name="PM2.5",
        )
        self.update_predefined_sensor(
            SensorLibrary.PM10__CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
            pm10,
            name="PM10",
        )

    def _process_status_data(self, data: bytes, address: str) -> None:
        """Parse status characteristic data."""
        info_byte, self.battery = unpack("<bb", data)
        (
            self.pm_on,
            self.error,
            self.bonded,
            self.charging,
            self.timer,
            _,
            self.voc_ready,
        ) = decode_info(info_byte)
        _LOGGER.debug(
            "%s: status data: %s, info_byte: %s, battery: %s, pm_on: %s, error: %s, bonded: %s, charging: %s, timer: %s, voc_ready: %s",
            address,
            data.hex(),
            f"{info_byte:b}",
            self.battery,
            self.pm_on,
            self.error,
            self.bonded,
            self.charging,
            self.timer,
            self.voc_ready,
        )
        self.update_predefined_sensor(SensorLibrary.BATTERY__PERCENTAGE, self.battery)
        self.update_predefined_binary_sensor(
            BinarySensorDeviceClass.BATTERY_CHARGING,
            self.charging,
            name="Battery charging",
        )

    def _process_voc_data(self, data: bytes, address: str) -> None:
        """Parse VOC characteristic data."""
        self.voc, _ = unpack("<hh", data)
        _LOGGER.debug("%s: VOC data: %s, voc: %s", address, data.hex(), self.voc)
        self.update_predefined_sensor(
            VOLATILE_ORGANIC_COMPOUNDS__CONCENTRATION_PARTS_PER_BILLION,
            self.voc,
            name="Volatile organic compounds",
        )

    def _update_aqs(self) -> int | None:
        """Update the AQS."""
        if None in (self.voc, self.pm1, self.pm25, self.pm10):
            return None
        aqs = calc_aqs(self.voc / 1000, self.pm1, self.pm25, self.pm10)
        self.update_sensor("air_quality_score", None, aqs, name="Air quality score")
        return aqs

    async def async_poll(self, ble_device: BLEDevice) -> SensorUpdate:
        """Poll the device to retrieve any values we can't get from passive listening."""
        address = ble_device.address
        _LOGGER.debug("%s: Polling for additional data", address)

        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClient, ble_device, address, max_attempts=4
            )
            for uuid, callback in (
                # (CHARACTERISTIC_VOC, self._process_voc_data),
                (CHARACTERISTIC_BME280, self._process_bme280_data),
                # (CHARACTERISTIC_STATUS, self._process_status_data),
                (CHARACTERISTIC_PM, self._process_pm_data),
            ):
                char = client.services.get_characteristic(uuid)
                data = await client.read_gatt_char(char)
                callback(data, address)

            await client.disconnect()
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception(ex)
        else:
            if (aqs := self._update_aqs()) is not None:
                await self.update_planetwatch_sensor_data(address, aqs, True)
        finally:
            if client:
                await client.disconnect()

        return self._finish_update()

    async def update_planetwatch_sensor_data(
        self, address: str, aqs: int, bypass_rate_limit: bool = False
    ) -> None:
        """Update PlanetWatch sensor with new data."""
        if self.planetwatch_sensor is False:
            return

        now = dt_now()
        if (
            not self.planetwatch_sensor
            or self.planetwatch_checked <= now - PLANETWATCH_CHECK_RATE_LIMIT
        ):
            self.planetwatch_checked = now
            url = f"https://algorandapi.planetwatch.io/api/planetwatch/checkSensor/{address}"
            try:
                async with self.client.get(url) as resp:
                    resp_data: dict | None = None
                    if resp.status == 200:
                        resp_data = await resp.json()
                        self.planetwatch_sensor = resp_data["sensorfound"]
                        if self.planetwatch_sensor:
                            self.update_predefined_sensor(
                                SensorLibrary.COUNT__NONE,
                                resp_data["sensor"]["data_collected"],
                                "planetwatch_data_collected",
                                "PlanetWatch data collected",
                            )
                    _LOGGER.debug(
                        "%s: %s %s", address, resp.status, json.dumps(resp_data)
                    )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error(ex)

        now = dt_now()
        last_updated = self.planetwatch_updated or now - PLANETWATCH_UPDATE_RATE_LIMIT
        if self.planetwatch_sensor and (
            last_updated <= now - PLANETWATCH_UPDATE_RATE_LIMIT or bypass_rate_limit
        ):
            self.planetwatch_updated = now
            data = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "altitude": self.elevation,
                "voc": self.voc / 1000,
                "aqs": aqs,
                "pm1": self.pm1,
                "pm25": self.pm25,
                "pm10": self.pm10,
                "temp": self.temperature,
                "humidity": self.humidity,
                "pressure": self.pressure / 100,
                "device_id": address,
                "company_name": "ATMOTUBE",
            }
            _LOGGER.debug(
                "%s: Updating PlanetWatch sensor: %s", address, json.dumps(data)
            )
            if None in data.values():
                return  # not ready to update
            url = "https://sensorsws.planetwatch.io/atmo/v1"
            try:
                async with self.client.post(url, json=data) as resp:
                    _LOGGER.debug("%s: %s %s", address, resp.status, await resp.text())
                    self.update_sensor(
                        "planetwatch_last_updated",
                        None,
                        now,
                        SensorDeviceClass.TIMESTAMP,
                        "PlanetWatch last updated",
                    )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error(ex)
