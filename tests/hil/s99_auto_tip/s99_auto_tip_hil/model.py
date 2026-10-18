"""Data validation and narrow backend contracts for the S99 HIL runner."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
import json
import re


class HILError(RuntimeError):
    """A test could not safely continue."""


class AbortLatched(HILError):
    """The independent safety interface has latched the run off."""


class State(str, Enum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    POWER_ISOLATED = "power_isolated"
    SOURCE_SELECTED = "source_selected"
    SOURCE_STABLE = "source_stable"
    ARMED = "armed"
    RUNNING = "running"
    COMPLETE = "complete"
    ABORT_LATCHED = "abort_latched"


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HILError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HILError(f"JSON document must be an object: {path}")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HILError(message)


def validate_manifest(
    manifest: Mapping[str, Any],
    sources: Mapping[str, Any],
    cables: Mapping[str, Any],
    debug_paths: Mapping[str, Any],
    cartridges: Mapping[str, Any],
) -> None:
    require(manifest.get("schema_version") == "s99.hil.manifest.v1", "unsupported manifest schema")
    run_id = manifest.get("run_id")
    require(isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) is not None, "run_id has invalid characters or length")
    require(manifest.get("stage") in {f"stage-{i}" for i in range(8)}, "stage must be stage-0 through stage-7")

    device = manifest.get("device")
    require(isinstance(device, dict), "device must be an object")
    require(device.get("model") == "Sequre S99", "device.model must identify Sequre S99")
    require(isinstance(device.get("hardware_revision"), str) and device["hardware_revision"], "hardware_revision is required")
    firmware_hash = device.get("firmware_git_sha256")
    require(isinstance(firmware_hash, str) and _SHA256.fullmatch(firmware_hash) is not None, "firmware_git_sha256 must be a SHA-256 digest")

    source = manifest.get("source")
    require(isinstance(source, dict), "source must be an object")
    source_id = source.get("source_id")
    require(source_id in sources, f"unknown source_id: {source_id}")
    require(isinstance(source.get("instance_id"), str) and source["instance_id"], "source.instance_id is required")

    cable = manifest.get("cable")
    require(isinstance(cable, dict), "cable must be an object")
    cable_id = cable.get("cable_id")
    require(cable_id in cables, f"unknown cable_id: {cable_id}")
    require(isinstance(cable.get("instance_id"), str) and cable["instance_id"], "cable.instance_id is required")
    source_interface = sources[source_id].get("power_interface")
    cable_interface = cables[cable_id].get("power_interface")
    require(source_interface == cable_interface, "source and power cable interfaces do not match")

    debug_path = manifest.get("debug_path")
    require(isinstance(debug_path, dict), "debug_path must be an object")
    debug_id = debug_path.get("debug_path_id")
    require(debug_id in debug_paths, f"unknown debug_path_id: {debug_id}")
    require(isinstance(debug_path.get("instance_id"), str) and debug_path["instance_id"], "debug_path.instance_id is required")

    cartridge = manifest.get("cartridge")
    require(isinstance(cartridge, dict), "cartridge must be an object")
    cartridge_id = cartridge.get("cartridge_id")
    require(cartridge_id in cartridges, f"unknown cartridge_id: {cartridge_id}")
    require(isinstance(cartridge.get("sample_id"), str) and cartridge["sample_id"], "cartridge.sample_id is required")
    require(cartridge.get("profile") in {"c245_2p5_ohm", "c245_5p5_ohm", "unknown"}, "cartridge.profile is invalid")
    require(cartridges[cartridge_id].get("profile") == cartridge["profile"], "cartridge profile does not match its catalog record")
    require(isinstance(cartridge.get("cold_resistance_ohm"), (int, float)), "cold_resistance_ohm must be measured or simulated")
    require(cartridge["cold_resistance_ohm"] > 0, "cold_resistance_ohm must be positive")
    require(cartridge.get("mounted") is True, "cartridge must be mounted")
    require(cartridge.get("no_contact_verified") is True, "no-contact condition must be verified")

    require(isinstance(manifest.get("probe_profile"), str) and manifest["probe_profile"], "probe_profile is required")
    require(isinstance(manifest.get("manual_tip_setting"), str) and manifest["manual_tip_setting"], "manual_tip_setting is required")
    require(isinstance(manifest.get("instrument_calibration_ids", {}), dict), "instrument_calibration_ids must map instrument names to calibration IDs")
    require(manifest.get("repetitions", 0) == 1, "one invocation may run exactly one repetition; start a new reviewed run for another sample")
    faults = manifest.get("fault_injection", [])
    require(isinstance(faults, list) and len(faults) <= 1, "fault_injection must contain at most one fault per run")
    known_faults = {
        "debug_loss", "stale_temperature", "host_crash", "controller_reset", "open_load", "short_load",
        "overcurrent", "overtemperature", "telemetry_loss", "relay_fault", "source_detach",
        "pd_renegotiation", "bms_cutoff",
    }
    require(all(fault in known_faults for fault in faults), "fault_injection contains an unknown fault")

    change = manifest.get("cartridge_change", {"requested": False})
    require(isinstance(change, dict), "cartridge_change must be an object")
    if change.get("requested") is True:
        require(isinstance(change.get("cooldown_max_tip_temperature_c"), (int, float)), "cartridge change requires a cooldown temperature limit")
        require(change["cooldown_max_tip_temperature_c"] >= 0, "cooldown temperature limit must be nonnegative")
        require(change.get("automatic_changer_id"), "cartridge change requires a preconfigured automatic changer")

    authorization = manifest.get("approved_live_limits")
    if authorization is not None:
        require(isinstance(authorization, dict), "approved_live_limits must be an object")


def validate_live_configuration(config: Mapping[str, Any]) -> None:
    """Validate the explicit record required before any future live adapter.

    This only verifies configuration claims.  It does not establish that a
    particular cutoff, interlock, calibration, or supervisor is physically
    present.  The current package has no live adapter and will always refuse
    to energize a DUT.
    """

    require(config.get("schema_version") == "s99.hil.fixture.v1", "unsupported fixture configuration schema")
    require(config.get("enabled") is True, "live fixture configuration is not explicitly enabled")
    require(bool(config.get("fixture_id")), "fixture_id is required")
    require(bool(config.get("station_id")), "station_id is required")
    require(config.get("human_supervision_interlock_verified") is True, "physical human-supervision interlock is not verified")
    require(config.get("debug_data_only_isolation_verified") is True, "debug data-only isolation is not verified")
    require(config.get("source_selector_break_before_make_verified") is True, "break-before-make source selector is not verified")
    require(config.get("fail_open_power_disconnect_verified") is True, "fail-open power disconnect is not verified")
    cutoff = config.get("independent_cutoff")
    require(isinstance(cutoff, dict), "independent_cutoff configuration is required")
    require(cutoff.get("verified") is True, "independent cutoff is not verified")
    cutoff_ids = cutoff.get("device_ids", [])
    require(isinstance(cutoff_ids, list) and len(cutoff_ids) >= 1 and all(cutoff_ids), "independent cutoff device IDs are required")
    evidence = config.get("verification_evidence_sha256")
    require(isinstance(evidence, str) and _SHA256.fullmatch(evidence) is not None, "verification evidence digest is required")
    calibration_ids = config.get("calibration_ids")
    require(isinstance(calibration_ids, list) and calibration_ids and all(calibration_ids), "current calibration IDs are required")


def validate_approved_live_limits(manifest: Mapping[str, Any]) -> None:
    """Require reviewed, measured limits before a future hardware adapter."""

    approval = manifest.get("approved_live_limits")
    require(isinstance(approval, dict), "approved_live_limits record is required for live mode")
    require(approval.get("approved") is True, "live limits have not been approved")
    require(bool(approval.get("approval_record_id")), "live limits need an approval record ID")
    require(bool(approval.get("reviewed_by")), "live limits need an independent reviewer")
    evidence_sha256 = approval.get("evidence_sha256")
    require(isinstance(evidence_sha256, str) and _SHA256.fullmatch(evidence_sha256) is not None, "live limits need a SHA-256 evidence reference")
    limits = approval.get("limits")
    require(isinstance(limits, dict), "approved limits object is required")
    required = (
        "minimum_input_voltage_v", "maximum_input_voltage_v", "maximum_peak_current_a",
        "maximum_pulse_energy_j", "maximum_total_energy_j", "maximum_run_seconds",
        "maximum_tip_temperature_c", "maximum_handle_temperature_c", "maximum_mosfet_temperature_c",
    )
    for key in required:
        value = limits.get(key)
        require(isinstance(value, (int, float)) and value > 0, f"approved live limit {key} must be a positive measured value")
    require(limits["minimum_input_voltage_v"] < limits["maximum_input_voltage_v"], "input voltage limits are inconsistent")


class RigBackend(Protocol):
    """The narrow fixture boundary; live implementations require safety review."""

    def snapshot(self) -> Mapping[str, Any]: ...

    def disable_all_sources(self) -> None: ...

    def disconnect_all_sources(self) -> None: ...

    def verify_bus_discharged(self) -> bool: ...

    def select_source(self, source_id: str) -> None: ...

    def connect_source(self, source_id: str, cable_id: str) -> None: ...

    def wait_source_stable(self) -> bool: ...

    def verify_debug_isolated(self, debug_path_id: str) -> bool: ...

    def read_telemetry(self) -> Mapping[str, Any]: ...

    def start_probe(self) -> None: ...

    def stop_probe(self) -> None: ...

    def change_cartridge(self, cartridge_id: str) -> None: ...


class IndependentSafety(Protocol):
    """A separate fail-open cutoff and watchdog, never the DUT/host itself."""

    def self_test(self) -> bool: ...

    def heartbeat(self) -> bool: ...

    def cut_power_and_latch(self, cause: str) -> None: ...

    def is_latched(self) -> bool: ...
