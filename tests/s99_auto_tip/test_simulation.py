"""Offline-only tests for the synthetic model and trace replay contract."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from s99_auto_tip.analysis import compare_observed_ranges, trace_features
from s99_auto_tip.cli import main as cli_main
from s99_auto_tip.model import ModelParameters, SimulationInputError, simulate_scenario, simulate_trace
from s99_auto_tip.trace import TraceValidationError, validate_trace
from s99_auto_tip.trace_io import load_csv, load_json, write_csv, write_json


# These values are deliberately arbitrary unit-test inputs, not S99 measurements
# and not recommended probe settings.
PARAMS = {
    "resistance_ohm": 5.5,
    "thermal_mass_j_per_k": 7.0,
    "thermal_resistance_k_per_w": 4.0,
    "heater_coupling_efficiency": 0.4,
    "sensor_time_constant_ms": 250.0,
    "sensor_noise_std_c": 0.0,
    "initial_tip_temperature_c": 22.0,
    "ambient_temperature_c": 22.0,
    "nominal_supply_voltage_v": 12.0,
    "supply_voltage_noise_std_v": 0.0,
    "supply_internal_resistance_ohm": 0.1,
    "sample_interval_ms": 100.0,
}
SCHEDULE = [
    {"start_ms": 0, "end_ms": 300, "duty_x256": 0},
    {"start_ms": 300, "end_ms": 1300, "duty_x256": 3},
]


def synthetic_trace(
    trace_id: str,
    profile: str,
    params: dict = PARAMS,
    seed: int = 1,
) -> dict:
    return simulate_trace(trace_id, profile, params, SCHEDULE, seed)


def measured_trace(trace_id: str, profile: str, values: list[float]) -> dict:
    trace = synthetic_trace(trace_id, profile)
    trace["trace_kind"] = "hardware_replay"
    trace["provenance"] = "measured_hardware"
    trace["metadata"].pop("simulation", None)
    trace["metadata"]["environment"]["no_contact_verified"] = True
    trace["samples"] = [
        {
            "time_us": index * 100_000,
            "tip_temperature_c": value,
            "tip_temperature_valid": True,
            "source_state": "stable",
        }
        for index, value in enumerate(values)
    ]
    validate_trace(trace)
    return trace


class ThermalModelTests(unittest.TestCase):
    def test_model_emits_valid_synthetic_trace_without_claiming_measured_current(self) -> None:
        trace = synthetic_trace("synthetic-1", "c245_5p5_ohm")
        validate_trace(trace)
        self.assertEqual(trace["schema_version"], "s99.trace.v1")
        self.assertEqual(trace["trace_kind"], "synthetic")
        self.assertEqual(trace["provenance"], "synthetic_model")
        self.assertTrue(all(sample["heater_current_a"] is None for sample in trace["samples"]))
        self.assertTrue(all(sample["source_current_a"] is not None for sample in trace["samples"]))
        self.assertTrue(
            trace["events"][0]["details"]["source_current_a_is_average_demand_surrogate"]
        )
        self.assertGreater(trace_features(trace)["temperature_rise_c"], 0)

    def test_model_is_deterministic_for_same_seed_and_variates_noise_for_new_seed(self) -> None:
        noisy = dict(PARAMS, sensor_noise_std_c=0.2, supply_voltage_noise_std_v=0.3)
        first = synthetic_trace("a", "c245_5p5_ohm", noisy, 4)
        repeat = synthetic_trace("a", "c245_5p5_ohm", noisy, 4)
        other_seed = synthetic_trace("a", "c245_5p5_ohm", noisy, 5)
        self.assertEqual(first, repeat)
        self.assertNotEqual(
            [sample["tip_temperature_c"] for sample in first["samples"]],
            [sample["tip_temperature_c"] for sample in other_seed["samples"]],
        )

    def test_case_parameters_allow_sensitivity_sweeps_without_defaults(self) -> None:
        lower_resistance = dict(PARAMS, resistance_ohm=2.5)
        higher_thermal_mass = dict(PARAMS, thermal_mass_j_per_k=14.0)
        slow_sensor = dict(PARAMS, sensor_time_constant_ms=800.0)
        base = synthetic_trace("base", "unknown", PARAMS)
        resistance_case = synthetic_trace("resistance", "unknown", lower_resistance)
        mass_case = synthetic_trace("mass", "unknown", higher_thermal_mass)
        sensor_case = synthetic_trace("sensor", "unknown", slow_sensor)
        self.assertNotEqual(trace_features(base)["temperature_rise_c"], trace_features(resistance_case)["temperature_rise_c"])
        self.assertNotEqual(trace_features(base)["temperature_rise_c"], trace_features(mass_case)["temperature_rise_c"])
        self.assertNotEqual(trace_features(base)["temperature_rise_c"], trace_features(sensor_case)["temperature_rise_c"])

    def test_average_supply_variation_changes_synthetic_bus_voltage(self) -> None:
        variable = dict(PARAMS, supply_voltage_noise_std_v=0.5)
        trace = synthetic_trace("variable-supply", "unknown", variable, seed=7)
        voltages = [sample["dut_input_voltage_v"] for sample in trace["samples"]]
        self.assertGreater(max(voltages) - min(voltages), 0)

    def test_no_non_calibrated_model_defaults_are_accepted(self) -> None:
        incomplete = dict(PARAMS)
        del incomplete["thermal_mass_j_per_k"]
        with self.assertRaisesRegex(SimulationInputError, "missing model parameters"):
            ModelParameters.from_dict(incomplete)

    def test_schedule_must_be_contiguous_and_bounded(self) -> None:
        with self.assertRaisesRegex(SimulationInputError, "contiguous"):
            simulate_trace("bad", "unknown", PARAMS, [{"start_ms": 1, "end_ms": 3, "duty_x256": 0}], 1)
        with self.assertRaisesRegex(SimulationInputError, "between 0 and 256"):
            simulate_trace("bad", "unknown", PARAMS, [{"start_ms": 0, "end_ms": 3, "duty_x256": 257}], 1)

    def test_scenario_runner_emits_each_explicit_parameter_case(self) -> None:
        scenario = {
            "schema_version": "s99.simulation.v1",
            "schedule": SCHEDULE,
            "metadata": {},
            "cases": [
                {"trace_id": "class-a", "cartridge_profile": "c245_2p5_ohm", "seed": 1, "model": dict(PARAMS, resistance_ohm=2.5)},
                {
                    "trace_id": "class-b",
                    "cartridge_profile": "c245_5p5_ohm",
                    "seed": 2,
                    "model": PARAMS,
                    "metadata": {"supply": {"kind": "battery_emulator"}},
                },
            ],
        }
        traces = simulate_scenario(scenario)
        self.assertEqual([trace["trace_id"] for trace in traces], ["class-a", "class-b"])
        self.assertEqual(traces[0]["metadata"]["cartridge"]["cold_resistance_ohm"], 2.5)
        self.assertEqual(traces[1]["metadata"]["supply"]["kind"], "battery_emulator")

    def test_scenario_runner_expands_list_values_as_parameter_sweep(self) -> None:
        model = dict(PARAMS, resistance_ohm=[2.5, 5.5], sensor_noise_std_c=[0.0, 0.2])
        scenario = {
            "schema_version": "s99.simulation.v1",
            "schedule": SCHEDULE,
            "metadata": {},
            "cases": [
                {
                    "trace_id": "sweep",
                    "cartridge_profile": "unknown",
                    "seed": [1, 2],
                    "model": model,
                }
            ],
        }
        traces = simulate_scenario(scenario)
        self.assertEqual(len(traces), 8)
        self.assertEqual(len({trace["trace_id"] for trace in traces}), 8)
        self.assertEqual(
            {trace["metadata"]["cartridge"]["cold_resistance_ohm"] for trace in traces},
            {2.5, 5.5},
        )


class TraceContractTests(unittest.TestCase):
    def test_times_are_strictly_increasing(self) -> None:
        trace = synthetic_trace("bad-time", "unknown")
        trace["samples"][1]["time_us"] = trace["samples"][0]["time_us"]
        with self.assertRaisesRegex(TraceValidationError, "strictly increasing"):
            validate_trace(trace)

    def test_fractional_microsecond_scope_times_are_supported(self) -> None:
        trace = synthetic_trace("fractional-time", "unknown")
        trace["samples"][1]["time_us"] = 100.01
        trace["events"].append({"time_us": 100.005, "type": "scope_sample", "details": {}})
        validate_trace(trace)
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "trace.csv"
            write_csv(trace, csv_path)
            replayed = load_csv(csv_path)
        self.assertEqual(replayed["samples"][1]["time_us"], 100.01)
        self.assertEqual(replayed["events"][1]["time_us"], 100.005)

    def test_schema_rejects_unversioned_or_unknown_sample_channels(self) -> None:
        trace = synthetic_trace("bad-schema", "unknown")
        trace["schema_version"] = "s99.trace.v0"
        with self.assertRaisesRegex(TraceValidationError, "unsupported schema_version"):
            validate_trace(trace)
        trace = synthetic_trace("bad-channel", "unknown")
        trace["samples"][0]["unreviewed_temperature_guess"] = 27.0
        with self.assertRaisesRegex(TraceValidationError, "unknown fields"):
            validate_trace(trace)

    def test_json_and_csv_replay_round_trip(self) -> None:
        trace = synthetic_trace("csv round trip", "c245_5p5_ohm", dict(PARAMS, sensor_noise_std_c=0.1), seed=11)
        with tempfile.TemporaryDirectory() as temporary_directory:
            json_path = Path(temporary_directory) / "trace.json"
            csv_path = Path(temporary_directory) / "trace.csv"
            replay_path = Path(temporary_directory) / "replay.json"
            write_json(trace, json_path)
            loaded_json = load_json(json_path)
            write_csv(loaded_json, csv_path)
            loaded_csv = load_csv(csv_path)
            write_json(loaded_csv, replay_path)
            replay = load_json(replay_path)
        self.assertEqual(loaded_csv["trace_id"], trace["trace_id"])
        self.assertEqual(loaded_csv["metadata"], trace["metadata"])
        self.assertEqual(loaded_csv["events"], trace["events"])
        self.assertEqual(loaded_csv["samples"], trace["samples"])
        self.assertEqual(replay, loaded_csv)

    def test_checked_in_trace_fixture_is_valid_and_synthetic_only(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "s99.trace.v1.synthetic-fixture.json"
        trace = load_json(fixture_path)
        self.assertEqual(trace["trace_kind"], "synthetic")
        self.assertEqual(trace["provenance"], "synthetic_model")
        self.assertEqual(compare_observed_ranges([trace])["result"], "unknown")

    def test_csv_rejects_metadata_changes_between_rows(self) -> None:
        trace = synthetic_trace("csv tamper", "unknown")
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "trace.csv"
            write_csv(trace, csv_path)
            with csv_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            rows[1]["metadata_json"] = rows[1]["metadata_json"].replace(
                '"profile":"unknown"', '"profile":"c245_2p5_ohm"'
            )
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(TraceValidationError, "trace-level fields changed"):
                load_csv(csv_path)

    def test_analysis_reports_overlap_as_unknown(self) -> None:
        parameters = dict(PARAMS, sensor_noise_std_c=0)
        a = synthetic_trace("overlap-a", "c245_2p5_ohm", parameters, seed=1)
        b = synthetic_trace("overlap-b", "c245_5p5_ohm", parameters, seed=1)
        result = compare_observed_ranges([a, b])
        self.assertTrue(result["observed_ranges_overlap"])
        self.assertEqual(result["result"], "unknown")
        self.assertEqual(result["reason"], "observed_feature_ranges_overlap")
        self.assertIsNone(result["classifier_threshold"])

    def test_disjoint_synthetic_ranges_still_do_not_qualify_hardware(self) -> None:
        low = synthetic_trace("low", "c245_2p5_ohm", dict(PARAMS, resistance_ohm=2.5))
        high = synthetic_trace("high", "c245_5p5_ohm", PARAMS)
        result = compare_observed_ranges([low, high])
        self.assertFalse(result["observed_ranges_overlap"])
        self.assertEqual(result["result"], "unknown")
        self.assertIn("synthetic", result["reason"])

    def test_measured_disjoint_observations_are_not_a_classifier_result(self) -> None:
        low = measured_trace("measured-low", "c245_2p5_ohm", [20.0, 21.0, 22.0])
        high = measured_trace("measured-high", "c245_5p5_ohm", [20.0, 24.0, 27.0])
        result = compare_observed_ranges([low, high])
        self.assertFalse(result["observed_ranges_overlap"])
        self.assertEqual(result["result"], "separated_in_observed_samples_unqualified")
        self.assertIsNone(result["classifier_threshold"])

    def test_hardware_trace_with_missing_contact_or_unstable_source_is_unknown(self) -> None:
        trace = measured_trace("unstable-source", "c245_2p5_ohm", [20.0, 21.0])
        trace["samples"][1]["source_state"] = "renegotiating"
        self.assertEqual(trace_features(trace)["feature_status"], "unknown_source_state_not_stable_or_missing")
        trace["samples"][1]["source_state"] = "stable"
        trace["metadata"]["environment"]["no_contact_verified"] = None
        self.assertEqual(trace_features(trace)["feature_status"], "unknown_no_contact_state_not_verified")

    def test_insufficient_temperature_samples_are_unknown(self) -> None:
        trace = synthetic_trace("invalid-sensor", "unknown")
        for sample in trace["samples"]:
            sample["tip_temperature_valid"] = False
        self.assertIsNone(trace_features(trace)["temperature_rise_c"])

    def test_cli_scenario_generation_and_replay_are_offline(self) -> None:
        scenario = {
            "schema_version": "s99.simulation.v1",
            "schedule": SCHEDULE,
            "metadata": {},
            "cases": [
                {"trace_id": "cli-low", "cartridge_profile": "c245_2p5_ohm", "seed": 1, "model": PARAMS},
                {"trace_id": "cli-high", "cartridge_profile": "c245_5p5_ohm", "seed": 1, "model": PARAMS},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario_path = root / "scenario.json"
            output_directory = root / "traces"
            report_path = root / "report.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            self.assertEqual(
                cli_main(["simulate", "--config", str(scenario_path), "--out-dir", str(output_directory)]),
                0,
            )
            trace_paths = sorted(output_directory.glob("*.json"))
            self.assertEqual(len(trace_paths), 2)
            self.assertEqual(
                cli_main(["analyze", *map(str, trace_paths), "--output", str(report_path)]),
                0,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "unknown")
        self.assertTrue(report["observed_ranges_overlap"])
        self.assertIsNone(report["classifier_threshold"])


if __name__ == "__main__":
    unittest.main()
