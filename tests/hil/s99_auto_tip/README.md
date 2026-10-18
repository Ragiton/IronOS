# S99 auto-tip HIL harness

This is a host-side control-flow and artifact harness for the staged plan in
[`docs/s99-auto-tip-detection-test-plan.md`](../../../docs/s99-auto-tip-detection-test-plan.md).
The committed implementation is **simulation-only**. It has no power-supply,
USB-PD, relay, UART, GPIO, S99, or safety-controller driver and cannot energize
a physical output. Synthetic traces are not measurements or evidence that a
probe is electrically or thermally safe.

## Run offline

Python 3.11 or newer is sufficient; the harness uses only the standard library.

```sh
PYTHONPATH=tests/hil/s99_auto_tip \
  python3 -m unittest discover -s tests/hil/s99_auto_tip/tests -v

PYTHONPATH=tests/hil/s99_auto_tip \
  python3 -m s99_auto_tip_hil \
    --manifest tests/hil/s99_auto_tip/s99_auto_tip_hil/manifests/stage-2-dummy-fault-smoke.json
```

`--mode dry-run` is the default. Each invocation executes exactly one manifest
and creates a new immutable directory containing `run-manifest.json`,
`trace.json` (`s99.trace.v1`), `events.jsonl`, `summary.json`, and `SHA256SUMS`.
An injected or unexpected fault latches the simulated safety controller, records
the abort, and returns exit status 2. The harness does not retry or move to
another test after a fault.

The source, cable, debug-path, and cartridge catalogs are identity templates,
not a record of qualified hardware. Catalog entries are marked not eligible for
live use. Before any eventual fixture integration, replace templates with
individually identified assets and add the measured contract, cable, cartridge,
calibration, revision, and cutoff evidence required by the test plan. Keep one
source selected per run; the runner enforces disable, disconnect, bus-discharge,
select, connect, and stable-source order.

`manifests/supply-matrix.json` lists the source classes covered by offline
control-flow tests: protected DC, fixed-PDO PD, PPS, wall chargers, PD/PPS power
banks, battery emulation, and protected packs. These are separate synthetic
runs. They do not qualify a particular charger, cable, bank, BMS, pack, or
contract point.

Cartridge changes require a preconfigured automatic changer plus a manifest
cooldown limit. The runner checks heater-off state, disconnected sources,
discharged DUT bus, and the tip-temperature limit before requesting a change.
The package provides only the simulator implementation of that operation.

## Fault injection

Set one name in `fault_injection` in a copied manifest to exercise a single
abort path: `debug_loss`, `stale_temperature`, `host_crash`,
`controller_reset`, `open_load`, `short_load`, `overcurrent`,
`overtemperature`, `telemetry_loss`, `relay_fault`, `source_detach`,
`pd_renegotiation`, or `bms_cutoff`. Each fault must use a unique `run_id`.
Fault injection verifies orchestration and artifact behavior only; it does not
validate actual relays, cutoffs, waveforms, chargers, or battery protection.

## Live-mode gate

`--mode live` requires an explicit fixture JSON with verified human-supervision
interlock, isolated data-only debug path, break-before-make selector, fail-open
disconnect, independent cutoff device IDs, calibration IDs, and a SHA-256
reference to the reviewed evidence bundle. The example fixture file is
intentionally disabled. Even a complete configuration is refused because no
hardware adapter has been reviewed or implemented. Do not remove that refusal
until the physical interlocks and cutoff have been independently verified,
fault-injection qualified, and integrated through reviewed drivers.

The `S99 auto-tip HIL harness` workflow runs only offline checks on GitHub-hosted
runners. Its explicit hardware request is constrained to self-hosted runners
tagged `s99-hil` and `hardware`, checks the runner's `S99_HIL_CONFIG` variable,
and currently records the fail-closed live-mode refusal. A future physical run
must remain noninteractive once started, have the independent human-supervision
interlock closed, and be physically monitored throughout every hot stage.
