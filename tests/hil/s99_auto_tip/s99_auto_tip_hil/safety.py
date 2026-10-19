"""Pure validation for synchronized measurements and approved run limits.

This module can reject bad evidence or an exceeded limit in host-side tests.
It is not a real-time protective device; the independently wired cutoff must
enforce the hard limits without relying on Python, the S99, or the host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import math

from .model import HILError


@dataclass(frozen=True)
class SafetyLimits:
    minimum_input_voltage_v: float
    maximum_input_voltage_v: float
    maximum_peak_current_a: float
    maximum_pulse_energy_j: float
    maximum_total_energy_j: float
    maximum_run_seconds: float
    maximum_sample_interval_s: float
    maximum_tip_temperature_c: float
    maximum_handle_temperature_c: float
    maximum_mosfet_temperature_c: float

    @classmethod
    def from_approved_manifest(cls, manifest: Mapping[str, Any]) -> "SafetyLimits":
        approval = manifest.get("approved_live_limits")
        if not isinstance(approval, Mapping) or approval.get("approved") is not True:
            raise HILError("runtime monitoring requires explicitly approved measured limits")
        limits = approval.get("limits")
        if not isinstance(limits, Mapping):
            raise HILError("approved live limits are missing")
        names = tuple(cls.__dataclass_fields__)
        parsed: dict[str, float] = {}
        for name in names:
            value = limits.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise HILError(f"approved live limit {name} must be finite and positive")
            parsed[name] = float(value)
        if parsed["minimum_input_voltage_v"] >= parsed["maximum_input_voltage_v"]:
            raise HILError("approved input voltage limits are inconsistent")
        return cls(**parsed)


@dataclass(frozen=True)
class SafetySample:
    """One time-aligned safety sample, including scope-derived pulse metrics."""

    time_s: float
    input_voltage_v: float
    peak_current_a: float
    pulse_energy_j: float
    total_energy_j: float
    tip_temperature_c: float
    handle_temperature_c: float
    mosfet_temperature_c: float
    tip_temperature_valid: bool
    source_stable: bool
    interlock_closed: bool
    estop_closed: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SafetySample":
        numeric_fields = (
            "time_s", "input_voltage_v", "peak_current_a", "pulse_energy_j", "total_energy_j",
            "tip_temperature_c", "handle_temperature_c", "mosfet_temperature_c",
        )
        parsed: dict[str, Any] = {}
        for name in numeric_fields:
            item = value.get(name)
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                raise HILError(f"safety sample {name} is missing or non-finite")
            parsed[name] = float(item)
        for name in ("tip_temperature_valid", "source_stable", "interlock_closed", "estop_closed"):
            item = value.get(name)
            if not isinstance(item, bool):
                raise HILError(f"safety sample {name} must be a boolean")
            parsed[name] = item
        if parsed["time_s"] < 0:
            raise HILError("safety sample time_s must not be negative")
        if any(parsed[name] < 0 for name in ("peak_current_a", "pulse_energy_j", "total_energy_j")):
            raise HILError("current and energy measurements must not be negative")
        return cls(**parsed)


class RuntimeSafetyMonitor:
    """Host-side fail-closed limit checker for a future continuously sampled run."""

    def __init__(self, limits: SafetyLimits):
        self.limits = limits
        self._last_time_s: float | None = None

    def accept(self, sample_value: Mapping[str, Any] | SafetySample) -> SafetySample:
        sample = sample_value if isinstance(sample_value, SafetySample) else SafetySample.from_mapping(sample_value)
        limits = self.limits
        if self._last_time_s is not None and sample.time_s <= self._last_time_s:
            raise HILError("safety telemetry is stale or out of order")
        if self._last_time_s is not None and sample.time_s - self._last_time_s > limits.maximum_sample_interval_s:
            raise HILError("safety telemetry sample gap exceeded the approved interval")
        if sample.time_s > limits.maximum_run_seconds:
            raise HILError("maximum run duration exceeded")
        if not limits.minimum_input_voltage_v <= sample.input_voltage_v <= limits.maximum_input_voltage_v:
            raise HILError("input voltage is outside the approved range")
        if sample.peak_current_a > limits.maximum_peak_current_a:
            raise HILError("measured peak current exceeded the approved limit")
        if sample.pulse_energy_j > limits.maximum_pulse_energy_j:
            raise HILError("measured pulse energy exceeded the approved limit")
        if sample.total_energy_j > limits.maximum_total_energy_j:
            raise HILError("measured total energy exceeded the approved limit")
        if sample.tip_temperature_c > limits.maximum_tip_temperature_c:
            raise HILError("tip temperature exceeded the approved limit")
        if sample.handle_temperature_c > limits.maximum_handle_temperature_c:
            raise HILError("handle temperature exceeded the approved limit")
        if sample.mosfet_temperature_c > limits.maximum_mosfet_temperature_c:
            raise HILError("MOSFET temperature exceeded the approved limit")
        if not sample.tip_temperature_valid:
            raise HILError("tip temperature telemetry is invalid")
        if not sample.source_stable:
            raise HILError("source contract or input voltage is unstable")
        if not sample.interlock_closed or not sample.estop_closed:
            raise HILError("safety interlock or emergency stop opened")
        self._last_time_s = sample.time_s
        return sample
