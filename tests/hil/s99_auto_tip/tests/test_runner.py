from __future__ import annotations

from pathlib import Path
import copy
import contextlib
import hashlib
import io
import json
import tempfile
import unittest

from s99_auto_tip_hil.model import HILError, read_json, validate_approved_live_limits, validate_live_configuration, validate_manifest
from s99_auto_tip_hil.runner import load_catalogs, run_manifest
from s99_auto_tip_hil.safety import RuntimeSafetyMonitor, SafetyLimits


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "s99_auto_tip_hil"
MANIFEST = PACKAGE / "manifests" / "stage-2-dummy-fault-smoke.json"


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalogs = load_catalogs(PACKAGE / "manifests")
        cls.manifest = read_json(MANIFEST)

    def run_case(self, manifest: dict) -> tuple[Path | None, int]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return run_manifest(manifest, self.catalogs, Path(temporary.name), "dry-run")

    def test_dry_run_emits_synthetic_trace_and_checksums(self) -> None:
        run_dir, exit_code = self.run_case(copy.deepcopy(self.manifest))
        self.assertEqual(exit_code, 0)
        assert run_dir is not None
        trace = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["schema_version"], "s99.trace.v1")
        self.assertEqual(trace["trace_kind"], "synthetic")
        self.assertEqual(trace["provenance"], "synthetic_model")
        self.assertTrue(trace["events"])
        self.assertTrue(trace["samples"])
        self.assertIsInstance(trace["samples"][0]["gate_state"], bool)
        self.assertIsInstance(trace["metadata"]["instrumentation"]["calibration_ids"], dict)
        self.assertFalse(summary["fault_latched"])
        self.assertEqual(summary["automatic_retries"], 0)
        self.assertTrue(summary["simulation_only"])
        for line in (run_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            expected, filename = line.split("  ", 1)
            self.assertEqual(expected, hashlib.sha256((run_dir / filename).read_bytes()).hexdigest())

    def test_source_switch_is_break_before_make_and_single_source(self) -> None:
        run_dir, exit_code = self.run_case(copy.deepcopy(self.manifest))
        self.assertEqual(exit_code, 0)
        assert run_dir is not None
        events = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))["events"]
        types = [event["type"] for event in events]
        self.assertLess(types.index("all_sources_disabled_and_disconnected"), types.index("source_selected"))
        source_stable = next(event for event in events if event["type"] == "source_stable")
        self.assertEqual(source_stable["details"]["connection_mode"], "single_source_break_before_make")

    def test_offline_supply_matrix_runs_each_source_class_separately(self) -> None:
        matrix = read_json(PACKAGE / "manifests" / "supply-matrix.json")
        seen_kinds = set()
        for index, case in enumerate(matrix["cases"]):
            with self.subTest(source=case["source_id"]):
                manifest = copy.deepcopy(self.manifest)
                manifest["run_id"] = f"supply-matrix-{index}"
                manifest["source"].update(source_id=case["source_id"], instance_id=f"SIM-{case['source_id']}")
                manifest["cable"].update(cable_id=case["cable_id"], instance_id=f"SIM-{case['cable_id']}")
                run_dir, exit_code = self.run_case(manifest)
                self.assertEqual(exit_code, 0)
                assert run_dir is not None
                trace = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
                seen_kinds.add(trace["metadata"]["supply"]["kind"])
                selected = [event for event in trace["events"] if event["type"] == "source_selected"]
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["details"]["source_id"], case["source_id"])
        self.assertEqual(seen_kinds, {"bench_dc", "usb_c_wall_source", "usb_c_battery_source"})

    def test_initial_asset_matrix_covers_three_sources_and_six_unqualified_tips(self) -> None:
        matrix = read_json(PACKAGE / "manifests" / "initial-asset-matrix.json")
        inventory = read_json(PACKAGE / "manifests" / "tip-inventory.json")
        sources = read_json(PACKAGE / "manifests" / "sources.json")["entries"]
        source_ids = {
            "user_bench_dc_template",
            "user_usb_c_wall_template",
            "user_usb_c_battery_template",
        }
        tip_ids = {tip["tip_id"] for tip in inventory["tips"]}
        self.assertEqual(len(tip_ids), 6)
        self.assertEqual(len(matrix["cases"]), 18)
        self.assertEqual(set(sources), source_ids)
        self.assertEqual(set(tip["brand"] for tip in inventory["tips"]), {"included_with_s99", "JBC"})
        self.assertTrue(all(tip["live_eligible"] is False for tip in inventory["tips"]))
        pairs = {(case["source_id"], case["tip_id"]) for case in matrix["cases"]}
        self.assertEqual(pairs, {(source_id, tip_id) for source_id in source_ids for tip_id in tip_ids})
        self.assertTrue(all(case["status"] == "awaiting_source_and_tip_qualification" for case in matrix["cases"]))
        self.assertFalse(matrix["live_eligible"])

    def test_each_fault_latches_and_never_retries(self) -> None:
        fault_names = (
            "debug_loss", "stale_temperature", "host_crash", "controller_reset", "open_load", "short_load",
            "overcurrent", "overtemperature", "telemetry_loss", "relay_fault", "source_detach",
            "pd_renegotiation", "bms_cutoff",
        )
        for fault in fault_names:
            with self.subTest(fault=fault):
                manifest = copy.deepcopy(self.manifest)
                manifest["run_id"] = f"fault-{fault}"
                manifest["fault_injection"] = [fault]
                run_dir, exit_code = self.run_case(manifest)
                self.assertEqual(exit_code, 2)
                assert run_dir is not None
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                trace = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], "abort_latched")
                self.assertTrue(summary["fault_latched"])
                self.assertEqual(summary["automatic_retries"], 0)
                self.assertEqual(summary["final_state"], "abort_latched")
                self.assertIn("independent_cutoff_latched", [event["type"] for event in trace["events"]])

    def test_manual_tip_swap_is_only_a_cooled_power_off_batch_boundary(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["run_id"] = "tip-boundary-too-hot"
        manifest["manual_tip_change_after_run"] = {
            "required": True,
            "cooldown_max_tip_temperature_c": 20,
            "cooldown_timeout_seconds": 60,
        }
        run_dir, exit_code = self.run_case(manifest)
        self.assertEqual(exit_code, 2)
        assert run_dir is not None
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("cooldown timeout", summary["abort_cause"])
        self.assertTrue(summary["fault_latched"])

        manifest["run_id"] = "tip-boundary-cool-and-off"
        manifest["manual_tip_change_after_run"]["cooldown_max_tip_temperature_c"] = 25
        run_dir, exit_code = self.run_case(manifest)
        self.assertEqual(exit_code, 0)
        assert run_dir is not None
        event_types = [event["type"] for event in json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))["events"]]
        self.assertIn("manual_tip_change_boundary_reached", event_types)
        self.assertNotIn("cartridge_change_fixture_completed", event_types)

    def test_automatic_tip_changer_manifest_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cartridge_change"] = {"requested": True, "automatic_changer_id": "not-used"}
        with self.assertRaisesRegex(HILError, "manual and may only happen"):
            validate_manifest(manifest, **self.catalogs)

    def test_mismatched_source_cable_and_multiple_faults_are_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cable"]["cable_id"] = "sim_usb_c_to_s99_adapter_template"
        with self.assertRaises(HILError):
            validate_manifest(manifest, **self.catalogs)
        manifest = copy.deepcopy(self.manifest)
        manifest["fault_injection"] = ["open_load", "overcurrent"]
        with self.assertRaises(HILError):
            validate_manifest(manifest, **self.catalogs)

    def test_live_configuration_requires_independent_cutoff_claims(self) -> None:
        incomplete = read_json(PACKAGE / "manifests" / "fixture-example.json")
        with self.assertRaises(HILError):
            validate_live_configuration(incomplete)
        complete = {
            "schema_version": "s99.hil.fixture.v1",
            "enabled": True,
            "fixture_id": "fixture-test",
            "station_id": "station-test",
            "human_supervision_interlock_verified": True,
            "debug_data_only_isolation_verified": True,
            "source_selector_break_before_make_verified": True,
            "fail_open_power_disconnect_verified": True,
            "hardware_manifest_sha256": "c" * 64,
            "instrument_manifest_sha256": "d" * 64,
            "independent_cutoff": {
                "verified": True,
                "device_ids": ["cutoff-1"],
                "qualification_evidence_sha256": "e" * 64,
            },
            "verification_evidence_sha256": "a" * 64,
            "calibration_ids": ["cal-1"],
        }
        validate_live_configuration(complete)
        # Even this fully populated record cannot enable power: the package has
        # no verified device adapter, and tests run with no hardware connected.
        with self.assertRaises(HILError):
            validate_approved_live_limits(self.manifest)
        approved_manifest = copy.deepcopy(self.manifest)
        approved_manifest["approved_live_limits"] = {
            "approved": True,
            "approval_record_id": "review-1",
            "reviewed_by": "independent-reviewer",
            "evidence_sha256": "b" * 64,
            "limits": {
                "minimum_input_voltage_v": 5,
                "maximum_input_voltage_v": 20,
                "maximum_peak_current_a": 1,
                "maximum_pulse_energy_j": 0.1,
                "maximum_total_energy_j": 1,
                "maximum_run_seconds": 1,
                "maximum_sample_interval_s": 0.2,
                "maximum_tip_temperature_c": 100,
                "maximum_handle_temperature_c": 60,
                "maximum_mosfet_temperature_c": 80,
            },
        }
        validate_approved_live_limits(approved_manifest)

    def test_live_mode_never_actuates_even_with_complete_claimed_configuration(self) -> None:
        fixture = {
            "schema_version": "s99.hil.fixture.v1",
            "enabled": True,
            "fixture_id": "fixture-test",
            "station_id": "station-test",
            "human_supervision_interlock_verified": True,
            "debug_data_only_isolation_verified": True,
            "source_selector_break_before_make_verified": True,
            "fail_open_power_disconnect_verified": True,
            "hardware_manifest_sha256": "c" * 64,
            "instrument_manifest_sha256": "d" * 64,
            "independent_cutoff": {
                "verified": True,
                "device_ids": ["cutoff-1"],
                "qualification_evidence_sha256": "e" * 64,
            },
            "verification_evidence_sha256": "a" * 64,
            "calibration_ids": ["cal-1"],
        }
        manifest = copy.deepcopy(self.manifest)
        manifest["approved_live_limits"] = {
            "approved": True,
            "approval_record_id": "review-1",
            "reviewed_by": "independent-reviewer",
            "evidence_sha256": "b" * 64,
            "limits": {
                "minimum_input_voltage_v": 5,
                "maximum_input_voltage_v": 20,
                "maximum_peak_current_a": 1,
                "maximum_pulse_energy_j": 0.1,
                "maximum_total_energy_j": 1,
                "maximum_run_seconds": 1,
                "maximum_sample_interval_s": 0.2,
                "maximum_tip_temperature_c": 100,
                "maximum_handle_temperature_c": 60,
                "maximum_mosfet_temperature_c": 80,
            },
        }
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            output_root = Path(temporary)
            run_dir, exit_code = run_manifest(manifest, self.catalogs, output_root, "live", fixture)
            self.assertIsNone(run_dir)
            self.assertEqual(exit_code, 3)
            self.assertEqual(list(output_root.iterdir()), [])

    def test_runtime_measurement_monitor_fails_closed(self) -> None:
        limits = SafetyLimits(10, 20, 4, 0.1, 1, 10, 1, 300, 60, 80)
        sample = {
            "time_s": 1,
            "input_voltage_v": 12,
            "peak_current_a": 2,
            "pulse_energy_j": 0.01,
            "total_energy_j": 0.1,
            "tip_temperature_c": 25,
            "handle_temperature_c": 25,
            "mosfet_temperature_c": 25,
            "tip_temperature_valid": True,
            "source_stable": True,
            "interlock_closed": True,
            "estop_closed": True,
        }
        monitor = RuntimeSafetyMonitor(limits)
        monitor.accept(sample)

        with self.assertRaisesRegex(HILError, "peak current"):
            monitor.accept(dict(sample, time_s=2, peak_current_a=4.1))

        stale_monitor = RuntimeSafetyMonitor(limits)
        stale_monitor.accept(sample)
        with self.assertRaisesRegex(HILError, "stale or out of order"):
            stale_monitor.accept(sample)

        with self.assertRaisesRegex(HILError, "interlock"):
            RuntimeSafetyMonitor(limits).accept(dict(sample, time_s=2, interlock_closed=False))

        gap_monitor = RuntimeSafetyMonitor(limits)
        gap_monitor.accept(sample)
        with self.assertRaisesRegex(HILError, "sample gap"):
            gap_monitor.accept(dict(sample, time_s=2.1))

    def test_runtime_measurement_monitor_rejects_missing_or_nonfinite_values(self) -> None:
        limits = SafetyLimits(10, 20, 4, 0.1, 1, 10, 1, 300, 60, 80)
        sample = {
            "time_s": 1,
            "input_voltage_v": 12,
            "peak_current_a": 2,
            "pulse_energy_j": 0.01,
            "total_energy_j": 0.1,
            "tip_temperature_c": 25,
            "handle_temperature_c": 25,
            "mosfet_temperature_c": 25,
            "tip_temperature_valid": True,
            "source_stable": True,
            "interlock_closed": True,
            "estop_closed": True,
        }
        with self.assertRaisesRegex(HILError, "input_voltage_v"):
            RuntimeSafetyMonitor(limits).accept(dict(sample, input_voltage_v=float("nan")))
        incomplete = dict(sample)
        del incomplete["peak_current_a"]
        with self.assertRaisesRegex(HILError, "peak_current_a"):
            RuntimeSafetyMonitor(limits).accept(incomplete)


if __name__ == "__main__":
    unittest.main()
