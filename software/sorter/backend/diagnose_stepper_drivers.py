"""Read each Pico stepper driver's IFCNT register without moving motors.

Stop the sorter backend first: it owns the USB serial port exclusively.
Run from this directory with ``.venv/bin/python diagnose_stepper_drivers.py``.
"""

import argparse
import json
import struct

from hardware.bus import BaseCommandCode, MCUBus
from hardware.sorter_interface import InterfaceCommandCode


def inspect_port(port: str) -> None:
    print(f"{port}:")
    bus = MCUBus(port)
    try:
        # Pico USB boards use address zero. PING and GET_VERSION do not
        # reinitialize the firmware or change any motor outputs.
        if not bus.scan_devices(min_address=0, max_address=0):
            print("  SorterInterface did not answer PING")
            return
        try:
            response = bus.send_command(0, BaseCommandCode.GET_VERSION, 0, b"", retries=0)
            version = json.loads(response.payload.decode())
            print(f"  firmware: {version}")
        except Exception as exc:
            print(f"  firmware version unavailable: {exc}")

        for channel in range(5):
            try:
                response = bus.send_command(
                    0, InterfaceCommandCode.STEPPER_DRV_READ_REGISTER,
                    channel, bytes([0x02]), retries=0,
                )
                if len(response.payload) != 4:
                    raise ValueError(f"expected 4 register bytes, received {len(response.payload)}")
                count = struct.unpack("<I", response.payload)[0] & 0xFF
                print(f"  channel {channel}: driver answered, IFCNT={count}")
            except Exception as exc:
                print(f"  channel {channel}: {exc}")
    finally:
        bus.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", help="USB serial port; default: all Pico SDK ports")
    args = parser.parse_args()
    ports = [args.port] if args.port else MCUBus.enumerate_buses()
    if not ports:
        parser.error("no Pico SDK USB serial ports found")
    for port in ports:
        try:
            inspect_port(port)
        except Exception as exc:
            print(f"{port}: could not inspect port: {exc}")


if __name__ == "__main__":
    main()
