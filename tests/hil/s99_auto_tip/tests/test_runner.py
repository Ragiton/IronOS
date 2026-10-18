from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import json
import tempfile
import unittest

from s99_auto_tip_hil.model import HILError, read_json, validate_live_configuration, validate_manifest
from s99_auto_tip_hil.runner import load_catalogs, run_manifest


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
        self.assertEqual(seen_kinds, {
            "bench_dc", "usb_pd_fixed", "usb_pd_pps", "wall_charger", "power_bank", "battery_emulator", "battery_pack"
        })

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

    def test_cartridge_change_requires_power_off_and_cooldown(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["run_id"] = "cartridge-too-hot"
        manifest["cartridge_change"] = {
            "requested": True,
            "cooldown_max_tip_temperature_c": 20,
            "automatic_changer_id": "preloaded-station-changer",
        }
        run_dir, exit_code = self.run_case(manifest)
        self.assertEqual(exit_code, 2)
        assert run_dir is not None
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("cooldown", summary["abort_cause"])
        self.assertTrue(summary["fault_latched"])

        manifest["run_id"] = "cartridge-cool-and-off"
        manifest["cartridge_change"]["cooldown_max_tip_temperature_c"] = 25
        run_dir, exit_code = self.run_case(manifest)
        self.assertEqual(exit_code, 0)
        assert run_dir is not None
        event_types = [event["type"] for event in json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))["events"]]
        self.assertIn("cartridge_change_preconditions_passed", event_types)
        self.assertIn("cartridge_change_fixture_completed", event_types)

    def test_mismatched_source_cable_and_multiple_faults_are_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cable"]["cable_id"] = "sim_usb_c_inline"
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
            "independent_cutoff": {"verified": True, "device_ids": ["cutoff-1"]},
            "verification_evidence_sha256": "a" * 64,
            "calibration_ids": ["cal-1"],
        }
        validate_live_configuration(complete)
        # Even this fully populated record cannot enable power: the package has
        # no verified device adapter, and tests run with no hardware connected.


if __name__ == "__main__":
    unittest.main()
