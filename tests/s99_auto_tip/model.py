"""Synthetic lumped thermal-response model for offline sensitivity studies.

This is not a digital twin of the Sequre S99. In particular, PWM demand is
averaged before the electrical calculation and no pulse waveform, current
limiting, gate-drive behavior, charger protection, or component safety limit is
modeled. All physical parameters are caller supplied and uncalibrated.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from itertools import product
from typing import Any

from .trace import SCHEMA_VERSION, validate_trace


SIMULATION_SCHEMA_VERSION = "s99.simulation.v1"
MODEL_ID = "unqualified_lumped_average_demand_v1"
_MODEL_PARAMETERS = {
    "resistance_ohm",
    "thermal_mass_j_per_k",
    "thermal_resistance_k_per_w",
    "heater_coupling_efficiency",
    "sensor_time_constant_ms",
    "sensor_noise_std_c",
    "initial_tip_temperature_c",
    "ambient_temperature_c",
    "nominal_supply_voltage_v",
    "supply_voltage_noise_std_v",
    "supply_internal_resistance_ohm",
    "sample_interval_ms",
}
_PROFILES = {"c245_2p5_ohm", "c245_5p5_ohm", "unknown"}
_MAX_SWEEP_TRACES = 4096


class SimulationInputError(ValueError):
    """Raised for incomplete or inconsistent synthetic scenarios."""


@dataclass(frozen=True)
class ModelParameters:
    resistance_ohm: float
    thermal_mass_j_per_k: float
    thermal_resistance_k_per_w: float
    heater_coupling_efficiency: float
    sensor_time_constant_ms: float
    sensor_noise_std_c: float
    initial_tip_temperature_c: float
    ambient_temperature_c: float
    nominal_supply_voltage_v: float
    supply_voltage_noise_std_v: float
    supply_internal_resistance_ohm: float
    sample_interval_ms: float

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "ModelParameters":
        if not isinstance(values, dict):
            raise SimulationInputError("model parameters must be an object")
        missing = _MODEL_PARAMETERS - values.keys()
        extra = values.keys() - _MODEL_PARAMETERS
        if missing:
            raise SimulationInputError(f"missing model parameters: {', '.join(sorted(missing))}")
        if extra:
            raise SimulationInputError(f"unknown model parameters: {', '.join(sorted(extra))}")
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SimulationInputError(f"model parameter {key} must be a finite number")
        params = cls(**{key: float(value) for key, value in values.items()})
        if params.resistance_ohm <= 0:
            raise SimulationInputError("resistance_ohm must be greater than zero")
        if params.thermal_mass_j_per_k <= 0:
            raise SimulationInputError("thermal_mass_j_per_k must be greater than zero")
        if params.thermal_resistance_k_per_w <= 0:
            raise SimulationInputError("thermal_resistance_k_per_w must be greater than zero")
        if not 0 <= params.heater_coupling_efficiency <= 1:
            raise SimulationInputError("heater_coupling_efficiency must be between zero and one")
        if params.sensor_time_constant_ms < 0:
            raise SimulationInputError("sensor_time_constant_ms must be non-negative")
        if params.sensor_noise_std_c < 0:
            raise SimulationInputError("sensor_noise_std_c must be non-negative")
        if params.nominal_supply_voltage_v <= 0:
            raise SimulationInputError("nominal_supply_voltage_v must be greater than zero")
        if params.supply_voltage_noise_std_v < 0:
            raise SimulationInputError("supply_voltage_noise_std_v must be non-negative")
        if params.supply_internal_resistance_ohm < 0:
            raise SimulationInputError("supply_internal_resistance_ohm must be non-negative")
        if params.sample_interval_ms < 0.001:
            raise SimulationInputError("sample_interval_ms must be at least one microsecond")
        return params


def _validate_schedule(schedule: Any) -> list[dict[str, int]]:
    if not isinstance(schedule, list) or not schedule:
        raise SimulationInputError("schedule must be a non-empty array")
    result: list[dict[str, int]] = []
    previous_end = 0
    for index, segment in enumerate(schedule):
        if not isinstance(segment, dict) or set(segment) != {"start_ms", "end_ms", "duty_x256"}:
            raise SimulationInputError(
                f"schedule[{index}] must contain exactly start_ms, end_ms, and duty_x256"
            )
        start_ms = segment["start_ms"]
        end_ms = segment["end_ms"]
        duty_x256 = segment["duty_x256"]
        for name, value in (("start_ms", start_ms), ("end_ms", end_ms), ("duty_x256", duty_x256)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise SimulationInputError(f"schedule[{index}].{name} must be an integer")
        if start_ms != previous_end:
            raise SimulationInputError("schedule intervals must be contiguous and start at zero")
        if end_ms <= start_ms:
            raise SimulationInputError(f"schedule[{index}].end_ms must be after start_ms")
        if not 0 <= duty_x256 <= 256:
            raise SimulationInputError(f"schedule[{index}].duty_x256 must be between 0 and 256")
        result.append({"start_ms": start_ms, "end_ms": end_ms, "duty_x256": duty_x256})
        previous_end = end_ms
    return result


def _duty_for_interval(schedule: list[dict[str, int]], midpoint_ms: float) -> int:
    for segment in schedule:
        if segment["start_ms"] <= midpoint_ms < segment["end_ms"]:
            return segment["duty_x256"]
    raise SimulationInputError(f"schedule does not cover interval midpoint {midpoint_ms}")


def _supply_point(params: ModelParameters, duty: float, rng: random.Random) -> tuple[float, float]:
    """Return loaded voltage and average demand current for a static surrogate."""
    open_circuit_v = params.nominal_supply_voltage_v
    if params.supply_voltage_noise_std_v:
        open_circuit_v += rng.gauss(0.0, params.supply_voltage_noise_std_v)
    open_circuit_v = max(open_circuit_v, 0.0)
    resistance = params.resistance_ohm
    source_resistance = params.supply_internal_resistance_ohm
    loaded_v = open_circuit_v / (1.0 + source_resistance * duty / resistance)
    average_current_a = duty * loaded_v / resistance
    return loaded_v, average_current_a


def _sample_times_ms(duration_ms: int, interval_ms: float) -> list[float]:
    times = [0.0]
    while times[-1] + interval_ms < duration_ms:
        times.append(times[-1] + interval_ms)
    if times[-1] < duration_ms:
        times.append(float(duration_ms))
    return times


def _metadata(
    profile: str,
    parameters: ModelParameters,
    trace_metadata: dict[str, Any] | None,
    seed: int,
) -> dict[str, Any]:
    if not isinstance(profile, str) or profile not in _PROFILES:
        raise SimulationInputError(f"unsupported cartridge_profile: {profile!r}")
    metadata: dict[str, Any] = {
        "run_id": None,
        "device": {
            "model": "S99",
            "hardware_revision": None,
            "device_id": None,
            "firmware_git_sha": None,
            "firmware_build_id": None,
        },
        "cartridge": {
            "profile": profile,
            "sample_id": None,
            "cold_resistance_ohm": parameters.resistance_ohm,
        },
        "supply": {
            "kind": "unknown",
            "model": None,
            "contract_voltage_v": parameters.nominal_supply_voltage_v,
            "contract_current_a": None,
            "state": "synthetic_stable_assumption",
        },
        "environment": {
            "ambient_temperature_c": parameters.ambient_temperature_c,
            "initial_tip_temperature_c": parameters.initial_tip_temperature_c,
            "no_contact_verified": None,
        },
        "probe": {"profile_id": "caller_supplied_schedule", "manual_tip_setting": None},
        "instrumentation": {
            "calibration_ids": None,
            "timebase": "monotonic_since_start_us",
        },
        "simulation": {
            "model_id": MODEL_ID,
            "seed": seed,
            "parameters": asdict(parameters),
            "limits": "none; no safety or calibration limits are modeled",
            "omitted_effects": [
                "pulse_peak_current_and_waveform",
                "switching_transients_and_gate_delay",
                "charger_or_battery_protection",
                "MOSFET_safe_operating_area",
                "firmware_sampling_phase_and_control_loop",
                "contact_or_workpiece_heat_sinking",
                "real_cartridge_variation",
            ],
        },
    }
    if trace_metadata is not None:
        if not isinstance(trace_metadata, dict):
            raise SimulationInputError("metadata must be an object")
        for key, value in trace_metadata.items():
            if key == "run_id":
                if value is not None and not isinstance(value, str):
                    raise SimulationInputError("metadata.run_id must be a string or null")
                metadata[key] = value
                continue
            if key not in metadata or not isinstance(value, dict) or not isinstance(metadata[key], dict):
                raise SimulationInputError(f"metadata override {key!r} must be a known metadata object")
            metadata[key].update(value)
    # Simulation provenance and parameters are authoritative, not user-overridable.
    metadata["simulation"] = {
        "model_id": MODEL_ID,
        "seed": seed,
        "parameters": asdict(parameters),
        "limits": "none; no safety or calibration limits are modeled",
        "omitted_effects": [
            "pulse_peak_current_and_waveform",
            "switching_transients_and_gate_delay",
            "charger_or_battery_protection",
            "MOSFET_safe_operating_area",
            "firmware_sampling_phase_and_control_loop",
            "contact_or_workpiece_heat_sinking",
            "real_cartridge_variation",
        ],
    }
    metadata["cartridge"]["profile"] = profile
    metadata["cartridge"]["cold_resistance_ohm"] = parameters.resistance_ohm
    metadata["environment"]["ambient_temperature_c"] = parameters.ambient_temperature_c
    metadata["environment"]["initial_tip_temperature_c"] = parameters.initial_tip_temperature_c
    return metadata


def simulate_trace(
    trace_id: str,
    cartridge_profile: str,
    parameters: ModelParameters | dict[str, Any],
    schedule: list[dict[str, int]],
    seed: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate one synthetic trace from caller-provided, uncalibrated inputs."""
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise SimulationInputError("trace_id must be a non-empty string")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise SimulationInputError("seed must be an integer")
    params = parameters if isinstance(parameters, ModelParameters) else ModelParameters.from_dict(parameters)
    validated_schedule = _validate_schedule(schedule)
    duration_ms = validated_schedule[-1]["end_ms"]
    times_ms = _sample_times_ms(duration_ms, params.sample_interval_ms)
    rng = random.Random(seed)

    tip_temperature_c = params.initial_tip_temperature_c
    sensor_temperature_c = tip_temperature_c
    samples: list[dict[str, Any]] = []
    previous_time_ms = times_ms[0]
    previous_voltage = params.nominal_supply_voltage_v
    previous_average_current = 0.0

    for index, time_ms in enumerate(times_ms):
        if index:
            midpoint_ms = (previous_time_ms + time_ms) / 2.0
            duty_x256 = _duty_for_interval(validated_schedule, midpoint_ms)
            duty = duty_x256 / 256.0
            loaded_voltage, average_current = _supply_point(params, duty, rng)
            average_power_w = loaded_voltage * average_current
            heat_w = average_power_w * params.heater_coupling_efficiency
            thermal_tau_s = params.thermal_mass_j_per_k * params.thermal_resistance_k_per_w
            dt_s = (time_ms - previous_time_ms) / 1000.0
            thermal_fraction = 1.0 - math.exp(-dt_s / thermal_tau_s)
            equilibrium_c = params.ambient_temperature_c + heat_w * params.thermal_resistance_k_per_w
            tip_temperature_c += (equilibrium_c - tip_temperature_c) * thermal_fraction

            sensor_tau_s = params.sensor_time_constant_ms / 1000.0
            if sensor_tau_s == 0:
                sensor_temperature_c = tip_temperature_c
            else:
                sensor_fraction = 1.0 - math.exp(-dt_s / sensor_tau_s)
                sensor_temperature_c += (tip_temperature_c - sensor_temperature_c) * sensor_fraction
            previous_voltage = loaded_voltage
            previous_average_current = average_current
        else:
            duty_x256 = _duty_for_interval(validated_schedule, 0.0)

        measured_temperature = sensor_temperature_c
        if params.sensor_noise_std_c:
            measured_temperature += rng.gauss(0.0, params.sensor_noise_std_c)
        samples.append(
            {
                "time_us": round(time_ms * 1000),
                "tip_temperature_c": measured_temperature,
                "tip_temperature_valid": True,
                "tip_adc_raw": None,
                "ambient_temperature_c": params.ambient_temperature_c,
                "handle_temperature_c": None,
                "mosfet_temperature_c": None,
                "heater_commanded": duty_x256 > 0,
                "pwm_duty_x256": duty_x256,
                "gate_source_v": None,
                "gate_state": None,
                "dut_input_voltage_v": previous_voltage,
                "source_current_a": previous_average_current,
                "heater_current_a": None,
                "source_state": "stable_synthetic_assumption",
                "source_contract_voltage_v": params.nominal_supply_voltage_v,
                "source_contract_current_a": None,
                "safety_controller_state": None,
                "interlock_closed": None,
                "estop_closed": None,
            }
        )
        previous_time_ms = time_ms

    trace = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "trace_kind": "synthetic",
        "provenance": "synthetic_model",
        "metadata": _metadata(cartridge_profile, params, metadata, seed),
        "samples": samples,
        "events": [
            {
                "time_us": 0,
                "type": "synthetic_model_assumptions",
                "details": {
                    "model_id": MODEL_ID,
                    "source_current_a_is_average_demand_surrogate": True,
                    "hardware_safety_or_classifier_qualification": False,
                },
            }
        ],
    }
    validate_trace(trace)
    return trace


def simulate_scenario(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Run each case, expanding list-valued model fields as a Cartesian sweep."""
    if not isinstance(scenario, dict):
        raise SimulationInputError("scenario must be an object")
    if scenario.get("schema_version") != SIMULATION_SCHEMA_VERSION:
        raise SimulationInputError(f"scenario schema_version must be {SIMULATION_SCHEMA_VERSION!r}")
    allowed = {"schema_version", "schedule", "metadata", "cases"}
    extra = scenario.keys() - allowed
    missing = allowed - scenario.keys()
    if extra:
        raise SimulationInputError(f"unknown scenario fields: {', '.join(sorted(extra))}")
    if missing:
        raise SimulationInputError(f"missing scenario fields: {', '.join(sorted(missing))}")
    cases = scenario["cases"]
    if not isinstance(cases, list) or not cases:
        raise SimulationInputError("cases must be a non-empty array")
    traces = []
    seen_ids = set()
    for index, case in enumerate(cases):
        required_case_fields = {"trace_id", "cartridge_profile", "seed", "model"}
        allowed_case_fields = required_case_fields | {"metadata"}
        if not isinstance(case, dict) or not required_case_fields <= case.keys() or case.keys() - allowed_case_fields:
            raise SimulationInputError(
                f"cases[{index}] must contain trace_id, cartridge_profile, seed, and model; metadata is optional"
            )
        if not isinstance(case["trace_id"], str) or not case["trace_id"].strip():
            raise SimulationInputError(f"cases[{index}].trace_id must be a non-empty string")
        if not isinstance(case["model"], dict):
            raise SimulationInputError(f"cases[{index}].model must be an object")
        missing_model_fields = _MODEL_PARAMETERS - case["model"].keys()
        extra_model_fields = case["model"].keys() - _MODEL_PARAMETERS
        if missing_model_fields:
            raise SimulationInputError(
                f"cases[{index}] missing model parameters: {', '.join(sorted(missing_model_fields))}"
            )
        if extra_model_fields:
            raise SimulationInputError(
                f"cases[{index}] has unknown model parameters: {', '.join(sorted(extra_model_fields))}"
            )

        model_keys = sorted(case["model"])
        dimensions: list[list[Any]] = []
        for key in model_keys:
            value = case["model"][key]
            if isinstance(value, list):
                if not value:
                    raise SimulationInputError(f"cases[{index}].model.{key} sweep cannot be empty")
                dimensions.append(value)
            else:
                dimensions.append([value])
        seeds = case["seed"] if isinstance(case["seed"], list) else [case["seed"]]
        if not seeds or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds):
            raise SimulationInputError(f"cases[{index}].seed must be an integer or non-empty integer array")
        dimensions.append(seeds)
        combination_count = math.prod(len(dimension) for dimension in dimensions)
        if combination_count > _MAX_SWEEP_TRACES:
            raise SimulationInputError(
                f"cases[{index}] expands to {combination_count} traces; limit is {_MAX_SWEEP_TRACES}"
            )

        case_metadata = dict(scenario["metadata"])
        if "metadata" in case:
            if not isinstance(case["metadata"], dict):
                raise SimulationInputError(f"cases[{index}].metadata must be an object")
            for key, value in case["metadata"].items():
                if key == "run_id":
                    case_metadata[key] = value
                else:
                    existing = case_metadata.get(key, {})
                    if not isinstance(value, dict) or not isinstance(existing, dict):
                        raise SimulationInputError(
                            f"cases[{index}].metadata.{key} must override a metadata object"
                        )
                    case_metadata[key] = {**existing, **value}

        for combination_index, values in enumerate(product(*dimensions)):
            model_values = dict(zip(model_keys, values[:-1]))
            seed = values[-1]
            trace_id = case["trace_id"]
            if combination_count > 1:
                trace_id = f"{trace_id}__{combination_index:04d}"
            if trace_id in seen_ids:
                raise SimulationInputError(f"duplicate trace_id in cases: {trace_id}")
            seen_ids.add(trace_id)
            traces.append(
                simulate_trace(
                    trace_id=trace_id,
                    cartridge_profile=case["cartridge_profile"],
                    parameters=model_values,
                    schedule=scenario["schedule"],
                    seed=seed,
                    metadata=case_metadata,
                )
            )
    return traces
