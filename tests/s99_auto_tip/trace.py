"""Validation helpers for the shared S99 trace contract."""

from __future__ import annotations

import math
from typing import Any


SCHEMA_VERSION = "s99.trace.v1"
TRACE_KINDS = {"synthetic", "hardware_replay", "hardware_capture"}
PROVENANCE = {"synthetic_model", "measured_hardware"}
CARTRIDGE_PROFILES = {"c245_2p5_ohm", "c245_5p5_ohm", "unknown"}
SUPPLY_KINDS = {
    "bench_dc",
    "usb_pd_fixed",
    "usb_pd_pps",
    "wall_charger",
    "power_bank",
    "battery_emulator",
    "battery_pack",
    "unknown",
}

SAMPLE_FIELDS = (
    "time_us",
    "tip_temperature_c",
    "tip_temperature_valid",
    "tip_adc_raw",
    "ambient_temperature_c",
    "handle_temperature_c",
    "mosfet_temperature_c",
    "heater_commanded",
    "pwm_duty_x256",
    "gate_source_v",
    "gate_state",
    "dut_input_voltage_v",
    "source_current_a",
    "heater_current_a",
    "source_state",
    "source_contract_voltage_v",
    "source_contract_current_a",
    "safety_controller_state",
    "interlock_closed",
    "estop_closed",
)

_NUMERIC_SAMPLE_FIELDS = {
    "tip_temperature_c",
    "ambient_temperature_c",
    "handle_temperature_c",
    "mosfet_temperature_c",
    "gate_source_v",
    "dut_input_voltage_v",
    "source_current_a",
    "heater_current_a",
    "source_contract_voltage_v",
    "source_contract_current_a",
}
_INTEGER_SAMPLE_FIELDS = {"tip_adc_raw", "pwm_duty_x256"}
_BOOLEAN_SAMPLE_FIELDS = {
    "tip_temperature_valid",
    "heater_commanded",
    "gate_state",
    "interlock_closed",
    "estop_closed",
}


class TraceValidationError(ValueError):
    """Raised when a trace is not compliant with `s99.trace.v1`."""


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_trace(trace: dict[str, Any]) -> None:
    """Validate the stable trace shape and basic channel invariants.

    The JSON Schema is the machine-readable contract. This dependency-free
    validator also checks time ordering and finite values for host tools/CI.
    """
    if not isinstance(trace, dict):
        raise TraceValidationError("trace must be an object")

    required = {
        "schema_version",
        "trace_id",
        "trace_kind",
        "provenance",
        "metadata",
        "samples",
        "events",
    }
    missing = required - trace.keys()
    extra = trace.keys() - required
    if missing:
        raise TraceValidationError(f"missing top-level fields: {', '.join(sorted(missing))}")
    if extra:
        raise TraceValidationError(f"unknown top-level fields: {', '.join(sorted(extra))}")
    if trace["schema_version"] != SCHEMA_VERSION:
        raise TraceValidationError(f"unsupported schema_version: {trace['schema_version']!r}")
    if not isinstance(trace["trace_id"], str) or not trace["trace_id"].strip():
        raise TraceValidationError("trace_id must be a non-empty string")
    if not isinstance(trace["trace_kind"], str) or trace["trace_kind"] not in TRACE_KINDS:
        raise TraceValidationError(f"unsupported trace_kind: {trace['trace_kind']!r}")
    if not isinstance(trace["provenance"], str) or trace["provenance"] not in PROVENANCE:
        raise TraceValidationError(f"unsupported provenance: {trace['provenance']!r}")
    if (trace["trace_kind"] == "synthetic") != (trace["provenance"] == "synthetic_model"):
        raise TraceValidationError("synthetic trace_kind and synthetic_model provenance must be paired")

    metadata = trace["metadata"]
    if not isinstance(metadata, dict):
        raise TraceValidationError("metadata must be an object")
    for field in (
        "device",
        "cartridge",
        "supply",
        "environment",
        "probe",
        "instrumentation",
        "simulation",
    ):
        if field in metadata and not isinstance(metadata[field], dict):
            raise TraceValidationError(f"metadata.{field} must be an object")
    cartridge = metadata.get("cartridge", {})
    if "profile" in cartridge and (
        not isinstance(cartridge["profile"], str) or cartridge["profile"] not in CARTRIDGE_PROFILES
    ):
        raise TraceValidationError(f"unsupported cartridge profile: {cartridge['profile']!r}")
    supply = metadata.get("supply", {})
    if "kind" in supply and (
        not isinstance(supply["kind"], str) or supply["kind"] not in SUPPLY_KINDS
    ):
        raise TraceValidationError(f"unsupported supply kind: {supply['kind']!r}")
    environment = metadata.get("environment", {})
    if "no_contact_verified" in environment and environment["no_contact_verified"] is not None:
        if not isinstance(environment["no_contact_verified"], bool):
            raise TraceValidationError("metadata.environment.no_contact_verified must be boolean or null")
    instrumentation = metadata.get("instrumentation", {})
    if "calibration_ids" in instrumentation:
        calibration_ids = instrumentation["calibration_ids"]
        if calibration_ids is not None and not isinstance(calibration_ids, dict):
            raise TraceValidationError("metadata.instrumentation.calibration_ids must be an object or null")
    if "timebase" in instrumentation:
        if instrumentation["timebase"] != "monotonic_since_start_us":
            raise TraceValidationError(
                "metadata.instrumentation.timebase must be monotonic_since_start_us"
            )

    samples = trace["samples"]
    if not isinstance(samples, list) or not samples:
        raise TraceValidationError("samples must be a non-empty array")
    previous_time = -1
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise TraceValidationError(f"samples[{index}] must be an object")
        unknown_fields = sample.keys() - SAMPLE_FIELDS
        if unknown_fields:
            raise TraceValidationError(
                f"samples[{index}] has unknown fields: {', '.join(sorted(unknown_fields))}"
            )
        if "time_us" not in sample:
            raise TraceValidationError(f"samples[{index}] is missing time_us")
        time_us = sample["time_us"]
        if not _is_finite_number(time_us) or time_us < 0:
            raise TraceValidationError(f"samples[{index}].time_us must be a finite non-negative number")
        if time_us <= previous_time:
            raise TraceValidationError("sample time_us values must be strictly increasing")
        previous_time = time_us
        for field in _NUMERIC_SAMPLE_FIELDS:
            value = sample.get(field)
            if value is not None and not _is_finite_number(value):
                raise TraceValidationError(f"samples[{index}].{field} must be finite or null")
        for field in _INTEGER_SAMPLE_FIELDS:
            value = sample.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise TraceValidationError(f"samples[{index}].{field} must be an integer or null")
        duty = sample.get("pwm_duty_x256")
        if duty is not None and not 0 <= duty <= 256:
            raise TraceValidationError(f"samples[{index}].pwm_duty_x256 must be between 0 and 256")
        for field in _BOOLEAN_SAMPLE_FIELDS:
            value = sample.get(field)
            if value is not None and not isinstance(value, bool):
                raise TraceValidationError(f"samples[{index}].{field} must be boolean or null")
        for field in ("source_state", "safety_controller_state"):
            value = sample.get(field)
            if value is not None and not isinstance(value, str):
                raise TraceValidationError(f"samples[{index}].{field} must be a string or null")

    events = trace["events"]
    if not isinstance(events, list):
        raise TraceValidationError("events must be an array")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"time_us", "type", "details"}:
            raise TraceValidationError(f"events[{index}] must have time_us, type, and details")
        if not _is_finite_number(event["time_us"]) or event["time_us"] < 0:
            raise TraceValidationError(f"events[{index}].time_us must be a finite non-negative number")
        if not isinstance(event["type"], str) or not event["type"]:
            raise TraceValidationError(f"events[{index}].type must be a non-empty string")
        if not isinstance(event["details"], dict):
            raise TraceValidationError(f"events[{index}].details must be an object")
