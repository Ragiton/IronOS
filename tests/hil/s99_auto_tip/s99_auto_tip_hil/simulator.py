"""Offline-only rig and independent safety-controller simulator.

No simulator method can energize a physical output.  Its event stream exists to
exercise runner logic and fault handling, not to model or qualify the S99 power
stage.
"""

from __future__ import annotations

from typing import Any, Mapping

from .model import HILError


FAULT_CHECKPOINT = {
    "debug_loss": "preflight",
    "stale_temperature": "preflight",
    "host_crash": "probe",
    "controller_reset": "probe",
    "open_load": "probe",
    "short_load": "probe",
    "overcurrent": "probe",
    "overtemperature": "probe",
    "telemetry_loss": "probe",
    "relay_fault": "source",
    "source_detach": "probe",
    "pd_renegotiation": "probe",
    "bms_cutoff": "probe",
}


class InjectedFault(HILError):
    def __init__(self, fault_name: str):
        super().__init__(f"simulated fault injected: {fault_name}")
        self.fault_name = fault_name


class SimulatedRig:
    """Deterministic rig simulation with no external side effects."""

    def __init__(self, fault_name: str | None = None):
        self.fault_name = fault_name
        self.sources_enabled: set[str] = set()
        self.sources_connected: set[str] = set()
        self.selected_source: str | None = None
        self.power_enabled = False
        self.probe_started = False
        self.latch = False
        self._fault_fired = False
        self._independent_safety: SimulatedSafetyController | None = None
        self._sample_index = 0

    def bind_safety_controller(self, safety: "SimulatedSafetyController") -> None:
        self._independent_safety = safety

    def checkpoint(self, name: str) -> None:
        if self.fault_name and FAULT_CHECKPOINT[self.fault_name] == name and not self._fault_fired:
            self._fault_fired = True
            # Model the separate watchdog/cutoff actor tripping before the
            # runner receives its fault indication. No DUT output exists.
            if self._independent_safety is not None:
                self._independent_safety.cut_power_and_latch(f"simulated independent trip: {self.fault_name}")
            raise InjectedFault(self.fault_name)

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "mode": "simulation",
            "all_sources_disabled": not self.sources_enabled,
            "connected_source_ids": sorted(self.sources_connected),
            "selected_source_id": self.selected_source,
            "dut_power_enabled": self.power_enabled,
            "estop_closed": True,
            "interlock_closed": True,
            "no_contact_verified": True,
            "cartridge_mounted": True,
            "sensor_status_valid": True,
            "calibration_current": True,
            "debug_data_only_isolated": True,
            "fixture_ready": True,
            "host_watchdog_ok": True,
            "safety_controller_watchdog_ok": not self.latch,
            "tip_temperature_c": 22.0,
            "handle_temperature_c": 22.0,
            "mosfet_temperature_c": 22.0,
            "source_contract_stable": True,
        }

    def disable_all_sources(self) -> None:
        self.sources_enabled.clear()
        self.power_enabled = False
        self.probe_started = False

    def disconnect_all_sources(self) -> None:
        self.sources_connected.clear()
        self.selected_source = None

    def verify_bus_discharged(self) -> bool:
        return not self.sources_connected and not self.power_enabled

    def select_source(self, source_id: str) -> None:
        if self.sources_connected or self.sources_enabled or self.selected_source is not None:
            raise HILError("simulator rejected source selection without break-before-make isolation")
        self.selected_source = source_id

    def connect_source(self, source_id: str, cable_id: str) -> None:
        if source_id != self.selected_source or self.sources_connected:
            raise HILError("simulator rejected ambiguous or multiple-source connection")
        self.sources_connected.add(source_id)

    def wait_source_stable(self) -> bool:
        return len(self.sources_connected) == 1 and self.selected_source in self.sources_connected

    def verify_debug_isolated(self, debug_path_id: str) -> bool:
        return debug_path_id == "sim_isolated_uart"

    def read_telemetry(self) -> Mapping[str, Any]:
        # Temperatures are invented simulator values; trace provenance records
        # that they are not measurements from an S99.
        return {
            "tip_temperature_c": 22.0,
            "tip_temperature_valid": True,
            "tip_adc_raw": None,
            "ambient_temperature_c": 22.0,
            "handle_temperature_c": 22.0,
            "mosfet_temperature_c": 22.0,
            "heater_commanded": False,
            "pwm_duty_x256": 0,
            "gate_source_v": None,
            "gate_state": False,
            "dut_input_voltage_v": 0.0,
            "source_current_a": 0.0,
            "heater_current_a": 0.0,
            "source_state": "simulated_disconnected",
            "source_contract_voltage_v": None,
            "source_contract_current_a": None,
            "source_contract_stable": True,
            "safety_controller_state": "latched" if self.latch else "ready",
            "interlock_closed": True,
            "estop_closed": True,
        }

    def read_safety_sample(self) -> Mapping[str, Any]:
        """Return synthetic-only samples for testing monitor control flow."""

        self._sample_index += 1
        return {
            "time_s": self._sample_index * 0.1,
            "input_voltage_v": 12.0,
            "peak_current_a": 1.0,
            "pulse_energy_j": 0.001,
            "total_energy_j": 0.001 * self._sample_index,
            "tip_temperature_c": 22.0,
            "handle_temperature_c": 22.0,
            "mosfet_temperature_c": 22.0,
            "tip_temperature_valid": True,
            "source_stable": True,
            "interlock_closed": True,
            "estop_closed": True,
        }

    def wait_for_tip_cooldown(self, maximum_temperature_c: float, timeout_seconds: float) -> bool:
        """Evaluate the synthetic temperature only; never waits on real hardware."""

        temperature = self.snapshot().get("tip_temperature_c")
        return isinstance(temperature, (int, float)) and temperature <= maximum_temperature_c

    def start_probe(self) -> None:
        # A simulated probe is an event only.  It never toggles a real output.
        self.probe_started = True

    def stop_probe(self) -> None:
        self.probe_started = False
        self.power_enabled = False

class SimulatedSafetyController:
    """Separate software model of the independent cutoff interface."""

    def __init__(self, rig: SimulatedRig, fault_name: str | None = None):
        self.rig = rig
        self.fault_name = fault_name

    def self_test(self) -> bool:
        return not self.rig.latch

    def heartbeat(self) -> bool:
        return not self.rig.latch

    def configure_hard_limits(self, limits: Mapping[str, float]) -> bool:
        # This only exercises the software contract. The simulator is not an
        # independently qualified hardware cutoff.
        return bool(limits) and not self.rig.latch

    def cut_power_and_latch(self, cause: str) -> None:
        self.rig.latch = True
        self.rig.power_enabled = False
        self.rig.probe_started = False
        self.rig.sources_enabled.clear()

    def is_latched(self) -> bool:
        return self.rig.latch
