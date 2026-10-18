# S99 offline thermal-response simulation and trace replay

This directory contains deterministic host-side tooling for exploring a proposed
S99 startup thermal-response classifier. It is deliberately an **offline
engineering aid**, not a source of safety limits or evidence that a probe is
safe on an S99. It must not be used to authorize live pulses.

The thermal model is a synthetic, lumped thermal approximation. It accepts all
physical parameters and heater schedule from an input scenario; it has no
S99-derived defaults. It models average heater demand and a first-order sensor
response, with optional seeded voltage variation and source resistance. It does
not model pulse peak current, switching transients, gate delay, charger OCP,
MOSFET SOA, PD negotiation, firmware sampling phase, or a real cartridge. A
`heater_current_a` field is therefore null for synthetic traces. The modeled
`source_current_a` is an average-demand surrogate, not a measured waveform.

No classifier threshold is embedded here. The analysis reports observed feature
ranges and their overlap. Overlap yields `unknown`; disjoint synthetic ranges
still do not support a hardware classification. A measured, pre-registered
calibration set and separate held-out data are required before classifier
thresholds can be proposed.

## Trace contract

The canonical JSON record is versioned as `s99.trace.v1`. Its required top-level
keys are `schema_version`, `trace_id`, `trace_kind`, `provenance`, `metadata`,
`samples`, and `events`. The JSON Schema is in
[`schemas/s99.trace.v1.schema.json`](schemas/s99.trace.v1.schema.json).

Each sample uses `time_us`, a monotonically increasing numeric timebase in
microseconds since the run started. Fractional microseconds are allowed for
high-rate instruments; firmware may emit whole microseconds. Thermal, electrical, source, gate, and safety channels
may be absent or `null` when they were not measured. This permits firmware,
simulator, and HIL capture to share one record without implying that channels
were sampled at the same physical rate; producers must preserve each channel's
measurement timestamp on the common timebase. Events have `time_us`, `type`,
and an object-valued `details` field.

The companion CSV is a row-oriented encoding of the same record: trace metadata
and event JSON are repeated on each row and sample channels occupy fixed columns.
Blank channel cells load as null (unmeasured). JSON remains the canonical format.
Both readers reject invalid schema versions, non-finite numbers, and
non-monotonic sample times.

[`fixtures/s99.trace.v1.synthetic-fixture.json`](fixtures/s99.trace.v1.synthetic-fixture.json)
is a small generated record for checking producer compatibility. It is marked
synthetic throughout and is not a hardware reference trace.

## Commands

Run host-only tests with:

```sh
PYTHONPATH=tests python3 -m unittest discover -s tests/s99_auto_tip -t tests -p 'test_*.py' -v
```

The command-line tool accepts an explicit scenario file and output directory:

```sh
PYTHONPATH=tests python3 -m s99_auto_tip.cli simulate --config scenario.json --out-dir traces
PYTHONPATH=tests python3 -m s99_auto_tip.cli analyze traces/*.json --output report.json
PYTHONPATH=tests python3 -m s99_auto_tip.cli convert trace.json --to-csv trace.csv
PYTHONPATH=tests python3 -m s99_auto_tip.cli convert trace.csv --to-json replay.json
```

A scenario file has `schema_version: "s99.simulation.v1"`, a `schedule` array
of contiguous `{start_ms, end_ms, duty_x256}` intervals, and one or more
`cases`. Each case supplies a trace ID prefix, a cartridge profile, every model
parameter, and a seed; optional case-level metadata can set a source class or
other run details. A model value may be a list; the simulator expands list
values and seed lists into a Cartesian sweep with generated trace IDs. Keep
separate cases for each cartridge profile so each model point retains the
corresponding label. `duty_x256` is only an input to the average model; its use
does not imply that a real S99 pulse at that duty is safe.
Model parameters are `resistance_ohm`, `thermal_mass_j_per_k`,
`thermal_resistance_k_per_w`, `heater_coupling_efficiency`,
`sensor_time_constant_ms`, `sensor_noise_std_c`, `initial_tip_temperature_c`,
`ambient_temperature_c`, `nominal_supply_voltage_v`,
`supply_voltage_noise_std_v`, `supply_internal_resistance_ohm`, and
`sample_interval_ms`. Cases are the explicit sweep dimensions; the tool does
not fill in missing physical values. Supply variation is limited to seeded
per-sample voltage noise and a static series-resistance surrogate. It does not
simulate battery state of charge, charger trips, or protocol renegotiation.

Analysis compares the range of the `temperature_rise_c` feature for known
cartridge labels. A range overlap is reported as `unknown` for the compared
classes. Non-overlapping synthetic ranges remain `unknown` because synthetic
traces cannot establish real-world separation. For measured data, a disjoint
observed range is reported only as `separated_in_observed_samples_unqualified`;
traces with unverified no-contact state, unstable/missing source state, invalid
temperature samples, or fault/interlock events are excluded and force an
`unknown` result. This is not a qualified threshold, classifier, or safety
result.

## Shared producer requirements

Firmware and HIL producers should use the field names in the JSON Schema and
keep `trace_kind`/`provenance` truthful. For a hardware capture use
`trace_kind: "hardware_capture"` and `provenance: "measured_hardware"`; for
archived measured traces replayed offline use `hardware_replay` with the same
provenance. Synthetic output uses `synthetic` and `synthetic_model`. Never label
synthetic values as measurements. Record source transitions, resets, trip and
interlock events instead of silently dropping them. Do not place calibration
thresholds in the raw trace.
