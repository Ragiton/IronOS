"""Hardware-facing contracts and a query-only SCPI-over-TCP transport.

These interfaces deliberately stop short of a live S99 fixture.  Instrument
drivers may collect evidence, but the package does not yet contain source
switching, heater actuation, or an independently qualified cutoff driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
import ctypes
import math
import socket
import threading

from .model import HILError


@dataclass(frozen=True)
class InstrumentIdentity:
    manufacturer: str
    model: str
    serial_number: str
    firmware: str

    @classmethod
    def parse_scpi_idn(cls, response: str) -> "InstrumentIdentity":
        fields = [field.strip() for field in response.strip().split(",")]
        if len(fields) != 4 or not all(fields):
            raise HILError("SCPI *IDN? response must contain manufacturer, model, serial, and firmware")
        return cls(*fields)


@dataclass(frozen=True)
class WaveformCapture:
    """A single instrument capture with calibration and timing metadata."""

    instrument_id: str
    channel_ids: tuple[str, ...]
    sample_rate_hz: float
    time_origin_s: float
    time_step_s: float
    sample_count: int
    raw_channels: Mapping[str, bytes]

    def validate(self) -> None:
        if not self.instrument_id:
            raise HILError("waveform capture is missing instrument_id")
        if not self.channel_ids or set(self.channel_ids) != set(self.raw_channels):
            raise HILError("waveform capture channels do not match the raw channel payloads")
        if not math.isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise HILError("waveform sample rate must be finite and positive")
        if not math.isfinite(self.time_origin_s) or not math.isfinite(self.time_step_s) or self.time_step_s <= 0:
            raise HILError("waveform timebase is invalid")
        if self.sample_count <= 0:
            raise HILError("waveform sample_count must be positive")
        if any(len(self.raw_channels[channel]) != self.sample_count for channel in self.channel_ids):
            raise HILError("waveform raw payload length does not match sample_count")


class ScpiQueryTransport(Protocol):
    """Read-only SCPI query boundary; implementations must not energize loads."""

    def query_ascii(self, command: str) -> str: ...

    def query_definite_block(self, command: str) -> bytes: ...

    def close(self) -> None: ...


class ScpiTcpTransport:
    """Query-only SCPI transport for instruments with a TCP text socket.

    It intentionally has no command-write method.  A device-specific scope
    adapter can use it to identify the instrument and retrieve data from an
    already configured acquisition.  Live fixture actuation remains outside
    this transport and is refused by the runner.
    """

    def __init__(self, host: str, port: int = 5555, timeout_s: float = 2.0, max_response_bytes: int = 16_777_216):
        if not host or "\n" in host or "\r" in host:
            raise ValueError("SCPI host must be a non-empty host name or address")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("SCPI port must be between 1 and 65535")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("SCPI timeout must be finite and positive")
        if not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self._socket: socket.socket | None = None
        self._stream = None
        self._lock = threading.Lock()

    def _connected_stream(self):
        if self._socket is None or self._stream is None:
            try:
                self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                self._socket.settimeout(self.timeout_s)
                self._stream = self._socket.makefile("rb", buffering=0)
            except OSError as exc:
                self.close()
                raise HILError(f"could not connect to SCPI instrument {self.host}:{self.port}: {exc}") from exc
        return self._socket, self._stream

    @staticmethod
    def _validate_query(command: str) -> bytes:
        if not isinstance(command, str) or not command.strip().endswith("?"):
            raise HILError("read-only SCPI transport accepts query commands ending in '?'")
        if "\n" in command or "\r" in command:
            raise HILError("SCPI command must be one line")
        return command.strip().encode("ascii") + b"\n"

    def query_ascii(self, command: str) -> str:
        payload = self._validate_query(command)
        with self._lock:
            sock, stream = self._connected_stream()
            try:
                sock.sendall(payload)
                response = stream.readline(self.max_response_bytes + 1)
            except (OSError, TimeoutError) as exc:
                raise HILError(f"SCPI query failed: {exc}") from exc
        if not response or len(response) > self.max_response_bytes or not response.endswith(b"\n"):
            raise HILError("SCPI ASCII response was empty, oversized, or unterminated")
        try:
            return response.rstrip(b"\r\n").decode("ascii")
        except UnicodeDecodeError as exc:
            raise HILError("SCPI ASCII response contained non-ASCII bytes") from exc

    def query_definite_block(self, command: str) -> bytes:
        payload = self._validate_query(command)
        with self._lock:
            sock, stream = self._connected_stream()
            try:
                sock.sendall(payload)
                marker = stream.read(1)
                if marker != b"#":
                    raise HILError("SCPI response is not a definite-length binary block")
                digits_raw = stream.read(1)
                if len(digits_raw) != 1 or digits_raw < b"1" or digits_raw > b"9":
                    raise HILError("SCPI binary block has an invalid length-digit count")
                digits = digits_raw[0] - ord("0")
                length_text = self._read_exactly(stream, digits)
                if len(length_text) != digits or not length_text.isdigit():
                    raise HILError("SCPI binary block has an invalid payload length")
                payload_length = int(length_text)
                if payload_length > self.max_response_bytes:
                    raise HILError("SCPI binary block exceeds the configured response limit")
                response = self._read_exactly(stream, payload_length)
                terminator = stream.read(1)
                if terminator not in {b"", b"\n", b"\r"}:
                    raise HILError("SCPI binary block had an unexpected terminator")
                if terminator == b"\r":
                    stream.read(1)
            except (OSError, TimeoutError) as exc:
                raise HILError(f"SCPI binary query failed: {exc}") from exc
        return response

    @staticmethod
    def _read_exactly(stream, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise HILError("SCPI binary block payload was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        stream, sock = self._stream, self._socket
        self._stream = None
        self._socket = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "ScpiTcpTransport":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class OscilloscopeDriver(Protocol):
    """Vendor adapter contract for high-speed voltage/current waveform evidence."""

    def identify(self) -> InstrumentIdentity: ...

    def acquire(self, channels: Sequence[str]) -> WaveformCapture: ...


class ThermocoupleDriver(Protocol):
    """Vendor adapter contract for calibrated low-rate temperature logging."""

    def identify(self) -> InstrumentIdentity: ...

    def read_temperatures_c(self, channels: Sequence[str]) -> Mapping[str, float]: ...


class PicoTc08Driver:
    """Low-rate USB TC-08 adapter using Pico's documented Python bindings.

    Pass the `picosdk.usbtc08.usbtc08` module as `sdk`; importing Pico's SDK
    remains optional for offline users. `channel_types` maps caller-defined
    probe names to `(channel_number, thermocouple_type)` where channels are
    1..8 and types are the documented TC-08 letters. This adapter only reads
    thermocouple temperatures. It does not switch a source, command the iron,
    or provide a safety cutoff.
    """

    _SUPPORTED_TYPES = frozenset("BEJKNRSTX")

    def __init__(
        self,
        sdk: object,
        channel_types: Mapping[str, tuple[int, str]],
        *,
        mains_hz: int,
    ):
        if mains_hz not in (50, 60):
            raise ValueError("TC-08 mains rejection must be 50 or 60 Hz")
        if not channel_types:
            raise ValueError("at least one TC-08 thermocouple channel is required")
        used_channels: set[int] = set()
        normalized: dict[str, tuple[int, str]] = {}
        for name, assignment in channel_types.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("TC-08 channel names must be non-empty strings")
            if not isinstance(assignment, tuple) or len(assignment) != 2:
                raise ValueError(f"TC-08 channel {name} must map to (channel_number, type)")
            channel, kind = assignment
            if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 8:
                raise ValueError(f"TC-08 channel number for {name} must be from 1 through 8")
            if channel in used_channels:
                raise ValueError("a TC-08 input channel cannot be assigned more than once")
            if not isinstance(kind, str) or len(kind) != 1 or kind.upper() not in self._SUPPORTED_TYPES:
                raise ValueError(f"unsupported TC-08 thermocouple type for {name}")
            used_channels.add(channel)
            normalized[name] = (channel, kind.upper())
        self.sdk = sdk
        self.channel_types = normalized
        self.mains_hz = mains_hz
        self._handle: int | None = None
        self._identity: InstrumentIdentity | None = None
        self.minimum_interval_ms: int | None = None

    def _check_success(self, result: object, operation: str) -> None:
        if result != 1:
            self.close()
            raise HILError(f"Pico USB TC-08 {operation} failed (status {result!r})")

    def _open_and_configure(self) -> None:
        if self._handle is not None:
            return
        try:
            handle = self.sdk.usb_tc08_open_unit()
        except (AttributeError, OSError, RuntimeError) as exc:
            raise HILError(f"could not open Pico USB TC-08: {exc}") from exc
        # Pico documents a positive unit handle on success; zero means no unit
        # found and negative values indicate an error.
        if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
            raise HILError(f"Pico USB TC-08 open failed (handle {handle!r})")
        self._handle = handle
        try:
            mains_rejection_60hz = 1 if self.mains_hz == 60 else 0
            self._check_success(
                self.sdk.usb_tc08_set_mains(handle, mains_rejection_60hz), "mains rejection setup"
            )
            for channel_name, (channel_number, kind) in self.channel_types.items():
                self._check_success(
                    self.sdk.usb_tc08_set_channel(handle, channel_number, ctypes.c_int8(ord(kind))),
                    f"channel setup ({channel_name})",
                )
            interval = self.sdk.usb_tc08_get_minimum_interval_ms(handle)
            if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
                self.close()
                raise HILError(f"Pico USB TC-08 returned an invalid sample interval: {interval!r}")
            self.minimum_interval_ms = interval
        except Exception:
            self.close()
            raise

    def identify(self) -> InstrumentIdentity:
        self._open_and_configure()
        if self._identity is not None:
            return self._identity
        info_type = getattr(self.sdk, "USBTC08_INFO", None)
        if info_type is None:
            self.close()
            raise HILError("Pico SDK binding is missing the documented USBTC08_INFO structure")
        try:
            info = info_type()
            info.size = ctypes.sizeof(info)
            self._check_success(self.sdk.usb_tc08_get_unit_info(self._handle, ctypes.byref(info)), "identity query")
            serial = bytes(info.szSerial).split(b"\0", 1)[0].decode("ascii")
            firmware = bytes(info.DriverVersion).split(b"\0", 1)[0].decode("ascii")
            if not serial or not firmware:
                raise HILError("Pico USB TC-08 identity is missing serial or driver version")
            self._identity = InstrumentIdentity("Pico Technology", "USB TC-08", serial, firmware)
            return self._identity
        except (AttributeError, OSError, RuntimeError, UnicodeDecodeError) as exc:
            self.close()
            raise HILError(f"Pico USB TC-08 identity query failed: {exc}") from exc

    def read_temperatures_c(self, channels: Sequence[str]) -> Mapping[str, float]:
        if not channels:
            raise HILError("at least one named TC-08 channel must be requested")
        if len(set(channels)) != len(channels):
            raise HILError("duplicate TC-08 channel requested")
        unknown = [name for name in channels if name not in self.channel_types]
        if unknown:
            raise HILError(f"unknown configured TC-08 channel(s): {', '.join(unknown)}")
        self._open_and_configure()
        temperatures = (ctypes.c_float * 9)()
        overflow = ctypes.c_int16(0)
        try:
            units = self.sdk.USBTC08_UNITS["USBTC08_UNITS_CENTIGRADE"]
            status = self.sdk.usb_tc08_get_single(
                self._handle, ctypes.byref(temperatures), ctypes.byref(overflow), units
            )
            self._check_success(status, "temperature read")
        except (AttributeError, OSError, RuntimeError, KeyError) as exc:
            self.close()
            if isinstance(exc, HILError):
                raise
            raise HILError(f"Pico USB TC-08 temperature read failed: {exc}") from exc
        results: dict[str, float] = {}
        for name in channels:
            channel_number, _ = self.channel_types[name]
            if overflow.value & (1 << channel_number):
                self.close()
                raise HILError(f"Pico USB TC-08 reports overflow on channel {channel_number}")
            value = float(temperatures[channel_number])
            if not math.isfinite(value):
                self.close()
                raise HILError(f"Pico USB TC-08 returned a non-finite temperature on {name}")
            results[name] = value
        return results

    def close(self) -> None:
        handle, self._handle = self._handle, None
        self._identity = None
        self.minimum_interval_ms = None
        if handle is not None:
            try:
                status = self.sdk.usb_tc08_close_unit(handle)
            except (AttributeError, OSError, RuntimeError) as exc:
                raise HILError(f"Pico USB TC-08 close failed: {exc}") from exc
            if status != 1:
                raise HILError(f"Pico USB TC-08 close failed (status {status!r})")

    def __enter__(self) -> "PicoTc08Driver":
        self.identify()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class UsbPdAnalyzerDriver(Protocol):
    """Vendor adapter contract for observed USB-C PD contract and VBUS data."""

    def identify(self) -> InstrumentIdentity: ...

    def read_contract(self) -> Mapping[str, object]: ...


class IndependentlyQualifiedCutoff(Protocol):
    """Hardware cutoff/control contract, separate from the host and S99."""

    def identify(self) -> InstrumentIdentity: ...

    def self_test(self) -> bool: ...

    def read_interlocks(self) -> Mapping[str, bool]: ...

    def latch_off(self, cause: str) -> None: ...

    def is_latched(self) -> bool: ...
