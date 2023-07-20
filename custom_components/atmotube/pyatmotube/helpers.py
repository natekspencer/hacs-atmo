"""Atmotube helpers."""
from __future__ import annotations

from collections.abc import Iterable

PM1_LEVELS = [14, 34, 61, 95, 100]
PM25_LEVELS = [20, 50, 90, 140, 170]
PM10_LEVELS = [30, 75, 125, 200, 250]


def decode_info(info_byte: int) -> map[bool]:
    """Decode an info byte.

    Returns, in order: `pm_on`, `error`, `bonded`, `charging`, `timer`, `_`, `voc_ready`.
    `_` denotes a reserved/future value and can be discarded/ignored.
    """
    return map(lambda i: bool(info_byte >> i & 0x01), range(7))


def decode_pm(data: bytes) -> float | None:
    """Decode potential unavailable PM."""
    if (msg_len := len(data)) and data == b"\xff" * msg_len:
        return None
    byteorder, divisor = ("big", 1) if msg_len == 2 else ("little", 100)
    return int.from_bytes(data, byteorder=byteorder) / divisor


def decode_pms(data: bytes, count: int, size: int) -> Iterable[float | None]:
    """Decode multiple PM data."""
    if len(data) < count * size:
        return (None,) * count
    return map(lambda i: decode_pm(data[i * size : i * size + size]), range(count))


def aqs_from_voc(voc: float) -> int:
    """Get AQS from VOC."""
    if voc < 0.5:
        aqs = 100 - 60 * voc
    elif voc < 2:
        aqs = (118 - 26 * voc) / 1.5
    else:
        aqs = (374 - 44 * voc) / 6.5
    return max(int(aqs), 0)


def aqs_from_pm(pm: float, levels: list[int]) -> int:  # pylint: disable=invalid-name
    """Get AQS from PM."""
    for i, level in enumerate(levels):
        if pm <= level:
            break
    else:
        i = len(levels) - 1
    adj = levels[i - 1] if i > 0 else 0
    aqs = int(100 - 20 * i - 20 * ((pm - adj) / (levels[i] - adj)))
    return max(aqs, 0)


def calc_aqs(voc: float, pm1: float, pm25: float, pm10: float) -> int:
    """Calculate the AQS from VOC and PM sensors."""
    values = [
        aqs_from_voc(voc),
        aqs_from_pm(pm1, PM1_LEVELS),
        aqs_from_pm(pm25, PM25_LEVELS),
        aqs_from_pm(pm10, PM10_LEVELS),
    ]
    return min(values)
