"""JSON and CSV serialization for `s99.trace.v1` records."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from .trace import SAMPLE_FIELDS, TraceValidationError, validate_trace


_CSV_TRACE_FIELDS = (
    "schema_version",
    "trace_id",
    "trace_kind",
    "provenance",
    "metadata_json",
    "events_json",
)
_CSV_FIELDS = _CSV_TRACE_FIELDS + SAMPLE_FIELDS
_FLOAT_FIELDS = {
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
_INT_FIELDS = {"tip_adc_raw", "pwm_duty_x256"}
_BOOL_FIELDS = {
    "tip_temperature_valid",
    "heater_commanded",
    "gate_state",
    "interlock_closed",
    "estop_closed",
}
_STRING_FIELDS = {"source_state", "safety_controller_state"}


def _reject_non_finite(value: str) -> None:
    raise TraceValidationError(f"JSON contains non-standard numeric value {value}")


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_json(path: str | Path) -> dict[str, Any]:
    """Load and validate one canonical JSON trace."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            trace = json.load(stream, parse_constant=_reject_non_finite)
    except (OSError, json.JSONDecodeError) as error:
        raise TraceValidationError(f"could not read trace JSON: {error}") from error
    validate_trace(trace)
    return trace


def write_json(trace: dict[str, Any], path: str | Path) -> None:
    """Validate and write one canonical JSON trace."""
    validate_trace(trace)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(trace, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def write_csv(trace: dict[str, Any], path: str | Path) -> None:
    """Write a row-oriented CSV companion, repeating trace-level JSON fields."""
    validate_trace(trace)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata_json = _json_dump(trace["metadata"])
    events_json = _json_dump(trace["events"])
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        for sample in trace["samples"]:
            row: dict[str, Any] = {
                "schema_version": trace["schema_version"],
                "trace_id": trace["trace_id"],
                "trace_kind": trace["trace_kind"],
                "provenance": trace["provenance"],
                "metadata_json": metadata_json,
                "events_json": events_json,
            }
            row.update(sample)
            for field in _CSV_FIELDS:
                value = row.get(field)
                if value is None:
                    row[field] = ""
                elif field in {"metadata_json", "events_json"}:
                    continue
                elif isinstance(value, bool):
                    row[field] = "true" if value else "false"
            writer.writerow(row)


def _parse_json_cell(value: str, field: str) -> Any:
    try:
        return json.loads(value, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as error:
        raise TraceValidationError(f"CSV {field} is not valid JSON: {error}") from error


def _parse_cell(row: dict[str, str], field: str) -> Any:
    value = row[field]
    if value == "":
        return None
    if field in _INT_FIELDS:
        try:
            return int(value)
        except ValueError as error:
            raise TraceValidationError(f"CSV {field} must be an integer") from error
    if field == "time_us":
        try:
            parsed = float(value)
        except ValueError as error:
            raise TraceValidationError("CSV time_us must be numeric") from error
        if not math.isfinite(parsed):
            raise TraceValidationError("CSV time_us must be finite")
        return int(parsed) if parsed.is_integer() else parsed
    if field in _FLOAT_FIELDS:
        try:
            parsed = float(value)
        except ValueError as error:
            raise TraceValidationError(f"CSV {field} must be numeric") from error
        if not math.isfinite(parsed):
            raise TraceValidationError(f"CSV {field} must be finite")
        return parsed
    if field in _BOOL_FIELDS:
        if value not in {"true", "false"}:
            raise TraceValidationError(f"CSV {field} must be 'true' or 'false'")
        return value == "true"
    if field in _STRING_FIELDS:
        return value
    return value


def load_csv(path: str | Path) -> dict[str, Any]:
    """Load and validate one CSV companion."""
    try:
        stream = Path(path).open("r", encoding="utf-8", newline="")
    except OSError as error:
        raise TraceValidationError(f"could not read trace CSV: {error}") from error
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(_CSV_FIELDS):
            raise TraceValidationError("CSV header does not match the s99.trace.v1 column order")
        rows = list(reader)

    if not rows:
        raise TraceValidationError("CSV trace has no sample rows")
    first = rows[0]
    trace = {
        "schema_version": first["schema_version"],
        "trace_id": first["trace_id"],
        "trace_kind": first["trace_kind"],
        "provenance": first["provenance"],
        "metadata": _parse_json_cell(first["metadata_json"], "metadata_json"),
        "events": _parse_json_cell(first["events_json"], "events_json"),
        "samples": [],
    }
    invariant = (trace["schema_version"], trace["trace_id"], trace["trace_kind"], trace["provenance"])
    metadata_cell = first["metadata_json"]
    events_cell = first["events_json"]
    for row_number, row in enumerate(rows, start=2):
        current = (row["schema_version"], row["trace_id"], row["trace_kind"], row["provenance"])
        if current != invariant or row["metadata_json"] != metadata_cell or row["events_json"] != events_cell:
            raise TraceValidationError(f"CSV trace-level fields changed on row {row_number}")
        sample = {field: _parse_cell(row, field) for field in SAMPLE_FIELDS}
        trace["samples"].append(sample)
    validate_trace(trace)
    return trace
