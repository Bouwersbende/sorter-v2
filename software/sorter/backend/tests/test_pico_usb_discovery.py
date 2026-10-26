from types import SimpleNamespace
from unittest.mock import patch

from hardware.bus import MCUBus


def test_both_pico_sdk_usb_serial_ids_are_candidates() -> None:
    ports = [
        SimpleNamespace(device="/dev/ttyACM0", vid=0x2E8A, pid=0x0009),
        SimpleNamespace(device="/dev/ttyACM1", vid=0x2E8A, pid=0x000A),
        SimpleNamespace(device="/dev/ttyACM2", vid=0x2E8A, pid=0x0003),
        SimpleNamespace(device="/dev/ttyUSB0", vid=0x1234, pid=0x0009),
    ]
    with patch("serial.tools.list_ports.comports", return_value=ports):
        assert MCUBus.enumerate_buses() == ["/dev/ttyACM0", "/dev/ttyACM1"]
        assert MCUBus.enumerate_buses(pid=0x000A) == ["/dev/ttyACM1"]
