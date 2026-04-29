import pytest

from sma.control import u32_to_words
from sma.registers import REGISTERS, decode

BY_NAME = {r.name: r for r in REGISTERS}


def test_decode_u32():
    assert decode(BY_NAME["serial_number"], [0x0001, 0x0002]) == 0x00010002


def test_decode_u32_nan():
    assert decode(BY_NAME["serial_number"], [0xFFFF, 0xFFFF]) is None


def test_decode_s32_negative():
    # -100 as s32 = 0xFFFFFF9C
    assert decode(BY_NAME["dc_power_w"], [0xFFFF, 0xFF9C]) == -100


def test_decode_s32_nan():
    assert decode(BY_NAME["dc_power_w"], [0x8000, 0x0000]) is None


def test_decode_scaled_frequency():
    # 5000 * 0.01 = 50.0 Hz
    assert decode(BY_NAME["grid_frequency_hz"], [0x0000, 0x1388]) == pytest.approx(50.0)


def test_decode_scaled_signed_temperature():
    # 235 * 0.1 = 23.5 °C
    assert decode(BY_NAME["device_temperature_c"], [0x0000, 0x00EB]) == pytest.approx(23.5)


def test_decode_u64():
    assert decode(BY_NAME["total_yield_wh"], [0x0000, 0x0000, 0x0001, 0x86A0]) == 100000


def test_decode_u64_nan():
    assert decode(BY_NAME["total_yield_wh"], [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]) is None


def test_decode_wrong_word_count():
    with pytest.raises(ValueError):
        decode(BY_NAME["serial_number"], [0x0001])


def test_register_catalog_unique_addresses():
    addresses = [r.address for r in REGISTERS]
    assert len(addresses) == len(set(addresses))


def test_u32_to_words_zero():
    assert u32_to_words(0) == [0x0000, 0x0000]


def test_u32_to_words_split():
    assert u32_to_words(1077) == [0x0000, 0x0435]
    assert u32_to_words(0xCAFEBABE) == [0xCAFE, 0xBABE]


def test_u32_to_words_rejects_negative():
    with pytest.raises(ValueError):
        u32_to_words(-1)


def test_u32_to_words_rejects_overflow():
    with pytest.raises(ValueError):
        u32_to_words(1 << 32)
