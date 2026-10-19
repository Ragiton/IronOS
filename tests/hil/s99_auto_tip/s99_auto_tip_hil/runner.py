"""Fail-closed orchestration for a future independently protected HIL rig."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import hashlib
import json

from .model import AbortLatched, HILError, State, validate_approved_live_limits, validate_live_configuration, validate_manifest
from .simulator import SimulatedRig, SimulatedSafetyController


def load_catalogs(catalog_dir: Path) -> dict[str, dict[str, Any]]:
    filenames = {
        "sources": "sources.json",
        "cables": "cables.json",
        "debug_paths": "debug-paths.json",
        "cartridges": "cartridges.json",
    }
    catalogues: dict[str, dict[str, Any]] = {}
    for key, filename in filenames.items():
        data = json.loads((catalog_dir / filename).read_text(encoding="utf-8"))
        entries = data.get("entries")
        if not isinstance(entries, dict):
            raise HILError(f"catalog {filename} must contain an entries object")
        catalogues[key] = entries
    return catalogues


class Run:
    def __init__(
        self,
        manifest: Mapping[str, Any],
        catalogs: Mapping[str, Mapping[str, Any]],
        output_root: Path,
        mode: str,
        fixture_config: Mapping[str, Any] | None = None,
    ):
        self.manifest = dict(manifest)
        self.catalogs = catalogs
        self.output_root = output_root
        self.mode = mode
        self.fixture_config = fixture_config
        self.rig = SimulatedRig(self._selected_fault())
        self.safety = SimulatedSafetyController(self.rig, self._selected_fault())
        self.rig.bind_safety_controller(self.safety)
        self.state = State.CREATED
        self.events: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self._time_us = 0
        self._cause: str | None = None

    def _selected_fault(self) -> str | None:
        faults = self.manifest.get("fault_injection", [])
        return faults[0] if faults else None

    def _event(self, event_type: str, **details: Any) -> None:
        self.events.append({"time_us": self._time_us, "type": event_type, "details": details})
        self._time_us += 1000

    def _sample(self) -> None:
        telemetry = self.rig.read_telemetry()
        keys = (
            "tip_temperature_c", "tip_temperature_valid", "tip_adc_raw", "ambient_temperature_c",
            "handle_temperature_c", "mosfet_temperature_c", "heater_commanded", "pwm_duty_x256",
            "gate_source_v", "gate_state", "dut_input_voltage_v", "source_current_a", "heater_current_a",
            "source_state", "source_contract_voltage_v", "source_contract_current_a",
            "safety_controller_state", "interlock_closed", "estop_closed",
        )
        record = {key: telemetry.get(key) for key in keys}
        record["time_us"] = self._time_us
        self.samples.append(record)

    def _transition(self, state: State, **details: Any) -> None:
        if self.state == State.ABORT_LATCHED:
            raise AbortLatched("run is latched off; no further state transition is allowed")
        self.state = state
        self._event("state_transition", state=state.value, **details)

    def _heartbeat(self) -> None:
        if not self.safety.heartbeat():
            raise HILError("independent safety-controller heartbeat failed")

    def _checkpoint(self, checkpoint: str) -> None:
        # This hook exists only on the simulator today.  A future reviewed
        # hardware adapter must implement equivalent independent fault tests.
        check = getattr(self.rig, "checkpoint", None)
        if check is not None:
            check(checkpoint)

    def _assert_preflight(self) -> Mapping[str, Any]:
        self._transition(State.PREFLIGHT)
        if not self.safety.self_test():
            raise HILError("independent safety-controller self-test failed")
        self._heartbeat()
        snapshot = self.rig.snapshot()
        checks = {
            "all_sources_disabled": "a source output is enabled at preflight",
            "estop_closed": "E-stop is open",
            "interlock_closed": "safety interlock is open",
            "no_contact_verified": "no-contact fixture state is not verified",
            "cartridge_mounted": "cartridge is not confirmed mounted",
            "sensor_status_valid": "temperature sensor status is invalid or stale",
            "calibration_current": "instrument calibration is missing or expired",
            "debug_data_only_isolated": "debug path isolation is not verified",
            "fixture_ready": "fixture is not ready",
            "host_watchdog_ok": "host watchdog is not healthy",
            "safety_controller_watchdog_ok": "independent controller watchdog is not healthy",
        }
        for key, message in checks.items():
            if snapshot.get(key) is not True:
                raise HILError(message)
        if snapshot.get("connected_source_ids"):
            raise HILError("a source is already connected; preflight requires all sources isolated")
        if snapshot.get("dut_power_enabled") is not False:
            raise HILError("DUT power must be disabled at preflight")
        if not self.rig.verify_debug_isolated(self.manifest["debug_path"]["debug_path_id"]):
            raise HILError("debug path failed its data-only isolation check")
        self._checkpoint("preflight")
        return snapshot

    def _manual_tip_change_boundary(self) -> None:
        request = self.manifest.get("manual_tip_change_after_run", {})
        if request.get("required") is not True:
            return
        # The human changes the tip only after this process has stopped. The
        # runner verifies the safe boundary but never operates a tip changer.
        snapshot = self.rig.snapshot()
        if snapshot.get("dut_power_enabled") is not False or snapshot.get("connected_source_ids"):
            raise HILError("manual tip change requires heater-off and all sources disconnected")
        if not self.rig.verify_bus_discharged():
            raise HILError("manual tip change requires verified DUT bus discharge")
        tip_temperature = snapshot.get("tip_temperature_c")
        max_temperature = request["cooldown_max_tip_temperature_c"]
        timeout = request["cooldown_timeout_seconds"]
        if not isinstance(tip_temperature, (int, float)) or not self.rig.wait_for_tip_cooldown(max_temperature, timeout):
            raise HILError("manual tip change boundary was not reached before cooldown timeout")
        self._event(
            "manual_tip_change_boundary_reached",
            tip_temperature_c=tip_temperature,
            maximum_temperature_c=max_temperature,
            cooldown_timeout_seconds=timeout,
            source_power_off=True,
            bus_discharged=True,
            operator_action="replace the tip only after this run has exited",
        )

    def _switch_source_break_before_make(self) -> None:
        source_id = self.manifest["source"]["source_id"]
        cable_id = self.manifest["cable"]["cable_id"]
        self.rig.disable_all_sources()
        self.rig.disconnect_all_sources()
        self._event("all_sources_disabled_and_disconnected")
        if not self.rig.verify_bus_discharged():
            raise HILError("DUT bus did not discharge before source selection")
        self._transition(State.POWER_ISOLATED)
        snapshot = self.rig.snapshot()
        if snapshot.get("connected_source_ids") or snapshot.get("all_sources_disabled") is not True:
            raise HILError("break-before-make isolation did not leave exactly zero connected sources")
        self._checkpoint("source")
        self.rig.select_source(source_id)
        self._event("source_selected", source_id=source_id)
        if self.rig.snapshot().get("selected_source_id") != source_id:
            raise HILError("source selector did not confirm the requested source")
        self.rig.connect_source(source_id, cable_id)
        connected = self.rig.snapshot().get("connected_source_ids", [])
        if connected != [source_id]:
            raise HILError("exactly one selected source must be connected; parallel sources are forbidden")
        self._transition(State.SOURCE_SELECTED, source_id=source_id)
        if not self.rig.wait_source_stable():
            raise HILError("source contract or output did not become stable")
        self._heartbeat()
        self._transition(State.SOURCE_STABLE, source_id=source_id)
        self._event("source_stable", source_id=source_id, connection_mode="single_source_break_before_make")

    def execute(self) -> tuple[Path, int]:
        validate_manifest(self.manifest, **self.catalogs)
        if self.mode not in {"dry-run", "live"}:
            raise HILError("mode must be dry-run or live")
        if self.mode == "live":
            if self.fixture_config is None:
                raise HILError("live mode requires an explicit fixture configuration file")
            validate_live_configuration(self.fixture_config)
            validate_approved_live_limits(self.manifest)
            # No physical drivers have been qualified or included.  Refuse all
            # live power even when config claims are present; this is a safety
            # gate, not a placeholder that can silently energize the DUT.
            raise HILError("live operation is disabled: no hardware adapter or independently verified fixture driver exists in this package")

        run_dir = self.output_root / self.manifest["run_id"]
        if run_dir.exists():
            raise HILError(f"artifact directory already exists; refusing overwrite: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=False)
        self._event("run_started", mode="dry-run", stage=self.manifest["stage"], simulation_only=True)
        status = "complete"
        exit_code = 0
        try:
            self._assert_preflight()
            self._switch_source_break_before_make()
            self._heartbeat()
            self._transition(State.ARMED)
            self._transition(State.RUNNING, execution="offline_simulation_only")
            # This operation is simulation-only.  No physical heater command
            # or source enable is issued by this implementation.
            self.rig.start_probe()
            self._event("probe_schedule_simulated_without_energy", probe_profile=self.manifest["probe_profile"])
            self._checkpoint("probe")
            self.rig.stop_probe()
            self.rig.disable_all_sources()
            self.rig.disconnect_all_sources()
            if not self.rig.verify_bus_discharged():
                raise HILError("DUT bus did not discharge during simulated shutdown")
            self._event("shutdown_complete", heater_off=True, sources_disconnected=True)
            self._manual_tip_change_boundary()
            self._transition(State.COMPLETE)
        except Exception as exc:  # Any unplanned condition fails closed.
            status = "abort_latched"
            exit_code = 2
            self._cause = str(exc)
            self.safety.cut_power_and_latch(self._cause)
            self.state = State.ABORT_LATCHED
            self._event("independent_cutoff_latched", cause=self._cause, automatic_retries=0)
            # Host-side shutdown is best effort; the independent cutoff above
            # is authoritative and must not depend on these calls succeeding.
            for operation in (self.rig.stop_probe, self.rig.disable_all_sources, self.rig.disconnect_all_sources):
                try:
                    operation()
                except Exception as cleanup_error:  # pragma: no cover - exercised by future adapters
                    self._event("host_cleanup_error", detail=str(cleanup_error))
            self._event("run_stopped_without_retry", automatic_retries=0)

        self._sample()
        self._event("run_finished", status=status, final_state=self.state.value)
        self._write_bundle(run_dir, status, exit_code)
        return run_dir, exit_code

    def _trace(self) -> dict[str, Any]:
        manifest = self.manifest
        source = self.catalogs["sources"][manifest["source"]["source_id"]]
        return {
            "schema_version": "s99.trace.v1",
            "trace_id": manifest["run_id"],
            "trace_kind": "synthetic",
            "provenance": "synthetic_model",
            "metadata": {
                "run_id": manifest["run_id"],
                "device": {
                    "model": manifest["device"]["model"],
                    "hardware_revision": manifest["device"]["hardware_revision"],
                    "device_id": manifest["device"].get("device_id"),
                    "firmware_git_sha": manifest["device"]["firmware_git_sha256"],
                    "firmware_build_id": manifest["device"].get("firmware_build_id"),
                },
                "cartridge": {
                    "profile": manifest["cartridge"]["profile"],
                    "sample_id": manifest["cartridge"]["sample_id"],
                    "cold_resistance_ohm": manifest["cartridge"]["cold_resistance_ohm"],
                },
                "supply": {
                    "kind": source["kind"],
                    "model": manifest["source"]["instance_id"],
                    "contract_voltage_v": None,
                    "contract_current_a": None,
                    "state": "simulated_only",
                },
                "environment": {
                    "ambient_temperature_c": None,
                    "initial_tip_temperature_c": None,
                    "no_contact_verified": manifest["cartridge"]["no_contact_verified"],
                },
                "probe": {
                    "profile_id": manifest["probe_profile"],
                    "manual_tip_setting": manifest["manual_tip_setting"],
                },
                "instrumentation": {
                    "calibration_ids": manifest.get("instrument_calibration_ids", {}),
                    "timebase": "monotonic_since_start_us",
                },
            },
            "samples": self.samples,
            "events": self.events,
        }

    def _write_bundle(self, run_dir: Path, status: str, exit_code: int) -> None:
        (run_dir / "run-manifest.json").write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (run_dir / "trace.json").write_text(json.dumps(self._trace(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (run_dir / "events.jsonl").write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in self.events), encoding="utf-8")
        summary = {
            "schema_version": "s99.hil.result.v1",
            "run_id": self.manifest["run_id"],
            "mode": self.mode,
            "status": status,
            "exit_code": exit_code,
            "final_state": self.state.value,
            "abort_cause": self._cause,
            "fault_latched": self.safety.is_latched(),
            "automatic_retries": 0,
            "simulation_only": True,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "limitations": [
                "No S99 hardware was connected or controlled.",
                "No classifier, pulse schedule, or power-stage safety envelope was exercised.",
                "Synthetic telemetry is control-flow evidence only and is not measured hardware data.",
            ],
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sums = []
        for path in sorted(run_dir.iterdir()):
            if path.is_file():
                sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
        (run_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


def run_manifest(
    manifest: Mapping[str, Any],
    catalogs: Mapping[str, Mapping[str, Any]],
    output_root: Path,
    mode: str = "dry-run",
    fixture_config: Mapping[str, Any] | None = None,
) -> tuple[Path | None, int]:
    """Run one manifest.  Exit status 2 denotes a latched safety abort."""

    run = Run(manifest, catalogs, output_root, mode, fixture_config)
    try:
        return run.execute()
    except Exception as exc:
        # Configuration errors and live-mode refusals occur before creating an
        # output bundle, so no misleading run result is emitted.
        print(f"HIL run refused: {exc}")
        return None, 3
