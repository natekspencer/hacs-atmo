"""pyatmotube tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from struct import unpack

from custom_components.atmotube.pyatmotube.helpers import (
    PM1_LEVELS,
    PM10_LEVELS,
    PM25_LEVELS,
    aqs_from_pm,
    aqs_from_voc,
    decode_pm,
    decode_pms,
)


def test_aqs() -> None:
    """Test AQS."""
    for voc in range(0, 100001):
        voc /= 1000
        aqs = aqs_from_voc(voc)
        assert aqs is not None

    for levels in (PM1_LEVELS, PM25_LEVELS, PM10_LEVELS):
        for pm in range(1, 100001):
            pm /= 100
            aqs = aqs_from_pm(pm, levels)
            assert aqs is not None


def test_decode_pms() -> None:
    """Test decode_pm and decode_pms methods."""
    data = b"\xff"
    assert decode_pm(data) is None

    (pm1,) = decode_pms(data, 1, 1)
    assert pm1 is None

    (pm1, pm2) = decode_pms(data, 2, 1)
    assert (pm1, pm2) == (None, None)

    (pm1, pm2) = decode_pms(b"\xff\xff\xff\xff", 2, 2)
    assert (pm1, pm2) == (None, None)

    (pm1, pm2, pm3) = decode_pms(bytes.fromhex("00010001000274051F"), 3, 2)
    assert (pm1, pm2, pm3) == (1, 1, 2)

    (pm1, pm2, pm3, pm4) = decode_pms(bytes.fromhex("640000A30000E30000640000"), 4, 3)
    assert (pm1, pm2, pm3, pm4) == (1, 1.63, 2.27, 1)


def test_other() -> None:
    """Other tests."""
    data = bytes.fromhex("01039e321a19000140ea4164")
    voc = int.from_bytes(data[:2], byteorder="big")
    assert voc == 259

    raw_data = bytes.fromhex(
        "0201060FFFFFFF00002DEB12FE0001418B4157090941544D4F545542451107B48A324AD96ED7AD18489A8E010045DB0CFFFFFF00010001000274051E"
    )
    voc, id, humi, temp, pres, info, batt = unpack(">h2sbbibb", raw_data[7:19])
    assert None not in (voc, id, humi, temp, pres, info, batt)
    assert int.from_bytes(raw_data[12:13], byteorder="big", signed=True) == temp

    pm1, pm25, pm10, fw = unpack(">hhh3s", raw_data[-9:])
    assert None not in (pm1, pm25, pm10, fw)


def test_data() -> None:
    """Data tests."""
    data = {
        "latitude": 40.448394775390625,
        "longitude": -111.87195105656713,
        "altitude": 1649,
        "voc": 0.273,
        "aqs": None,
        "pm1": 1,
        "pm25": 1.4,
        "pm10": 2.45,
        "temp": 22.8,
        "humidity": 28,
        "pressure": 841.7,
        "device_id": "CC:61:37:BB:02:02",
        "company_name": "ATMOTUBE",
    }
    assert None in data.values()


def test_time() -> None:
    """Time tests."""
    now = datetime.now(timezone.utc)
    limiter = timedelta(seconds=30)
    last_updated = None or now - limiter
    assert last_updated == now - limiter
    last_updated = None or now - limiter - limiter
    assert last_updated == now - limiter * 2

    assert now.isoformat()
