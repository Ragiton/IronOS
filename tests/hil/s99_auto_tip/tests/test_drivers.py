from __future__ import annotations

from contextlib import closing
import ctypes
import socket
import threading
import unittest

from s99_auto_tip_hil.drivers import InstrumentIdentity, PicoTc08Driver, ScpiTcpTransport, WaveformCapture
from s99_auto_tip_hil.model import HILError


class ScpiTransportTests(unittest.TestCase):
    def serve_once(self, response: bytes) -> tuple[str, int, threading.Thread]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()

        def serve() -> None:
            with closing(listener):
                connection, _ = listener.accept()
                with closing(connection):
                    command = bytearray()
                    while not command.endswith(b"\n"):
                        part = connection.recv(1)
                        if not part:
                            return
                        command.extend(part)
                    connection.sendall(response)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return host, port, thread

    def test_query_ascii_and_parse_instrument_identity(self) -> None:
        host, port, server = self.serve_once(b"RIGOL TECHNOLOGIES,DS1104Z+,DS1ZA000000000,00.04.05\n")
        with ScpiTcpTransport(host, port, timeout_s=1) as client:
            response = client.query_ascii("*IDN?")
        server.join(timeout=1)
        identity = InstrumentIdentity.parse_scpi_idn(response)
        self.assertEqual(identity.model, "DS1104Z+")
        self.assertEqual(identity.serial_number, "DS1ZA000000000")

    def test_query_binary_definite_length_block(self) -> None:
        host, port, server = self.serve_once(b"#16abcdef\n")
        with ScpiTcpTransport(host, port, timeout_s=1) as client:
            payload = client.query_definite_block(":WAV:DATA?")
        server.join(timeout=1)
        self.assertEqual(payload, b"abcdef")

    def test_transport_refuses_scpi_write_commands(self) -> None:
        with ScpiTcpTransport("127.0.0.1", 5555, timeout_s=0.1) as client:
            with self.assertRaisesRegex(HILError, "query commands"):
                client.query_ascii(":OUTP ON")

    def test_waveform_capture_requires_channel_and_timebase_consistency(self) -> None:
        capture = WaveformCapture(
            instrument_id="scope-1",
            channel_ids=("CH1",),
            sample_rate_hz=100_000_000,
            time_origin_s=0,
            time_step_s=1e-8,
            sample_count=4,
            raw_channels={"CH1": b"\x00\x01\x02\x03"},
        )
        capture.validate()
        invalid = WaveformCapture(
            instrument_id="scope-1",
            channel_ids=("CH1",),
            sample_rate_hz=100_000_000,
            time_origin_s=0,
            time_step_s=1e-8,
            sample_count=4,
            raw_channels={"CH1": b"\x00"},
        )
        with self.assertRaisesRegex(HILError, "payload length"):
            invalid.validate()


class FakeTc08Sdk:
    USBTC08_UNITS = {"USBTC08_UNITS_CENTIGRADE": 0}

    class USBTC08_INFO(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("size", ctypes.c_int16),
            ("DriverVersion", ctypes.c_char * 12),
            ("PicoppVersion", ctypes.c_int16),
            ("HardwareVersion", ctypes.c_int16),
            ("Variant", ctypes.c_int16),
            ("szSerial", ctypes.c_char * 11),
            ("szCalDate", ctypes.c_char * 9),
        ]

    def __init__(self, *, status: int = 1, overflow: int = 0):
        self.status = status
        self.overflow = overflow
        self.closed: list[int] = []
        self.configured: list[tuple[int, int, int]] = []

    def usb_tc08_open_unit(self):
        return 42

    def usb_tc08_set_mains(self, handle, sixty_hertz):
        return self.status

    def usb_tc08_set_channel(self, handle, channel, tc_type):
        self.configured.append((handle, channel, tc_type.value))
        return self.status

    def usb_tc08_get_minimum_interval_ms(self, handle):
        return 900

    def usb_tc08_get_unit_info(self, handle, info_pointer):
        info = ctypes.cast(info_pointer, ctypes.POINTER(self.USBTC08_INFO)).contents
        info.DriverVersion = b"1.2.3"
        info.HardwareVersion = 1
        info.Variant = 1
        info.szSerial = b"TC08-1234"
        info.szCalDate = b"2025/01/1"
        return self.status

    def usb_tc08_get_single(self, handle, temperature_pointer, overflow_pointer, units):
        temperatures = ctypes.cast(temperature_pointer, ctypes.POINTER(ctypes.c_float * 9)).contents
        temperatures[0] = 24.0
        temperatures[1] = 97.25
        temperatures[2] = 31.5
        overflow = ctypes.cast(overflow_pointer, ctypes.POINTER(ctypes.c_int16)).contents
        overflow.value = self.overflow
        return self.status

    def usb_tc08_close_unit(self, handle):
        self.closed.append(handle)
        return 1


class PicoTc08DriverTests(unittest.TestCase):
    def make_driver(self, sdk: FakeTc08Sdk) -> PicoTc08Driver:
        return PicoTc08Driver(sdk, {"tip": (1, "K"), "handle": (2, "T")}, mains_hz=60)

    def test_reads_named_temperature_channels_and_identifies_unit(self) -> None:
        sdk = FakeTc08Sdk()
        driver = self.make_driver(sdk)
        identity = driver.identify()
        readings = driver.read_temperatures_c(("tip", "handle"))
        self.assertEqual(identity.manufacturer, "Pico Technology")
        self.assertEqual(identity.model, "USB TC-08")
        self.assertEqual(identity.serial_number, "TC08-1234")
        self.assertEqual(identity.firmware, "1.2.3")
        self.assertEqual(readings, {"tip": 97.25, "handle": 31.5})
        self.assertEqual(driver.minimum_interval_ms, 900)
        self.assertEqual(sdk.configured, [(42, 1, ord("K")), (42, 2, ord("T"))])
        driver.close()
        self.assertEqual(sdk.closed, [42])

    def test_overflow_fails_closed_and_closes_unit(self) -> None:
        sdk = FakeTc08Sdk(overflow=1 << 1)
        driver = self.make_driver(sdk)
        with self.assertRaisesRegex(HILError, "overflow on channel 1"):
            driver.read_temperatures_c(("tip",))
        self.assertEqual(sdk.closed, [42])

    def test_driver_rejects_invalid_channel_assignments(self) -> None:
        sdk = FakeTc08Sdk()
        with self.assertRaisesRegex(ValueError, "from 1 through 8"):
            PicoTc08Driver(sdk, {"tip": (0, "K")}, mains_hz=50)
        with self.assertRaisesRegex(ValueError, "cannot be assigned more than once"):
            PicoTc08Driver(sdk, {"tip": (1, "K"), "handle": (1, "T")}, mains_hz=50)

    def test_sdk_error_fails_closed_and_releases_open_handle(self) -> None:
        sdk = FakeTc08Sdk(status=0)
        driver = self.make_driver(sdk)
        with self.assertRaisesRegex(HILError, "mains rejection setup failed"):
            driver.identify()
        self.assertEqual(sdk.closed, [42])


if __name__ == "__main__":
    unittest.main()
