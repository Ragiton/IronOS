# S99 auto-tip HIL harness

This is a host-side control-flow and artifact harness for the staged plan in
[`docs/s99-auto-tip-detection-test-plan.md`](../../../docs/s99-auto-tip-detection-test-plan.md).
The committed runner is **simulation-only** and cannot energize a physical
output. The package now has a low-rate Pico TC-08 temperature-read adapter and
a query-only SCPI transport for retrieving instrument data, but no source
selector, power-supply actuation, USB-PD switching, S99/heater control, UART
control, or independently qualified cutoff driver. Synthetic traces and
instrument read adapters are not evidence that a probe is electrically or
thermally safe.

## Requested first hardware setup

The initial test fixture is one S99, one USB-C battery, one USB-C wall charger,
one bench DC supply, the three tips bundled with the S99, and three official
JBC tips. Give each tip its own ID and measure its cold resistance. Do not infer
tip resistance from brand or packaging. This setup can characterize that one
S99 and these six samples; it cannot qualify other S99 revisions or all JBC
tips.

Before connecting a source, identify its exact model and output ratings and
confirm the S99 input connection. In particular, verify the wiring, polarity,
and current/voltage rating of the S99's included cable or any XT60-to-USB-C
adapter. Do not use an unverified cable. The source selector and independent
cutoff cannot be selected or commissioned until this path and the source models
are known.

The rig needs a remotely controlled, one-source-at-a-time selector; isolated,
data-only UART; calibrated current/voltage/gate/temperature instrumentation; a
guarded no-contact fixture; and an independent fail-open hardware cutoff with
watchdogs and a latching E-stop. The human remains present to supervise every
powered run, but does not answer prompts, change sources, or handle a tip during
a run.

Once a reviewed hardware runner exists, each permitted tip's source batch should
run in this order: bench DC, USB-C wall charger, USB-C battery. It must shut
down, disconnect, verify bus discharge and tip cool-down, save the results, and
end the run before a tip change. The human changes tips only at that boundary;
the next tip uses a separate run. There is no automatic cartridge changer and
no human prompt inside a run.

## Equipment status

Measurement gear can be shortlisted by capability now: an oscilloscope with a
preliminary target of at least 50 MHz and 100 MS/s; appropriately rated current,
differential-voltage, and isolated gate probes; a four-wire meter; calibrated
tip/handle/MOSFET-area temperature sensors; and a USB-C PD analyzer. The new
Pico TC-08 adapter reads external thermocouples; it does not control power or
provide an independent cutoff. Final ratings must match the measured circuit
and observed waveforms. No final measurement-equipment product list is approved
yet.

The S99 manufacturer's [product page](https://sequremall.com/products/sequer-s99-soldering-iron-support-pd-qc-dc-pps-power-supply-compatible-with-c245-tip-for-drone-rc-model-welding-repair-tool-anti-static-welding-pen)
lists Type-C input, up to 21 V, and 150 W at 2.5 Ω / 75 W at the shipped
~5.5 Ω tip. Those are product ratings, not safe probe limits. The
[manufacturer manual index](https://sequremall.com/pages/instruction-manual)
does not settle the included cable's pinout or current rating. Verify the actual
lead before choosing connectors, selector, or cutoff.

One buyable analyzer candidate is the [ChargerLAB ET240](https://www.power-z.com/products/chargerlab-power-z-et240),
but its product page does not state a pass-through peak-current/pulse rating.
Do not put it inline on the S99 power path until that rating is confirmed. The
[Acroname S85 datasheet](https://acroname.com/sites/default/files/2025-07/S85-USB-C-SWITCH-v1.12.pdf)
specifies 5 A maximum bidirectional VBUS current and a 20 V recommended
operating maximum. That is not cleared for the full S99 range or the possible
2.5 Ω pulse; the idealized current at 20 V / 2.5 Ω is 8 A. The
[TI TPS259827LEVM](https://www.ti.com/tool/TPS259827LEVM) and
[TE 2071583-4 DC contactor](https://www.te.com/en/product-2071583-4.html)
are possible DC-path design leads only. They are not a commissioned fast
independent cutoff or a USB-C selector. None of these parts makes the rig ready
for powered tests.

There is currently no identified USB-C selector/cutoff cleared for automated
testing above 5 A. The S85 could be considered only for a specific measured
condition within its ratings; it is not a general solution for this fixture.

The source selector and cutoff are not ready to buy as a finished system. Their
connector, current/pulse rating, fail-open behavior, and commissioning depend
on the exact battery, wall charger, bench supply, and S99 cable. Do not assume a
USB-C analyzer or selector is safe to put inline just because it supports PD.

**Live testing is not ready.** Before even powered dummy-load tests, settle the
exact three source models, verify the S99 XT60-to-USB-C cable wiring/rating,
and commission the independent cutoff. The current runner has no physical
source-switch/heater/cutoff drivers and intentionally refuses `--mode live`
even if a fixture configuration claims to be complete.

## Test order

1. **Identify the setup:** record S99 board revision and firmware, the three
   source models/ratings, cable path, and IDs for all six tips.
2. **Offline review:** build the baseline firmware, review board/power details,
   run the model/trace replay, and check sensor noise with no heater power.
3. **Dummy-load safety checks:** with no real tip installed, test source
   switching and every independent cutoff/interlock/watchdog using dummy loads.
   Inject faults and confirm the run latches off without retrying.
4. **Passive tip checks:** with all power removed, measure all six tips and
   reject missing, open, shorted, unknown, warm, or contacted conditions.
5. **Output-stage checks:** on a sacrificial S99 and dummy load, measure peak
   current, gate behavior, voltage sag, pulse energy, and temperature. The
   single S99 in the initial setup is not automatically a sacrificial unit; if
   no sacrificial S99 is available, this stage is blocked.
6. **Bounded tip checks:** only after the previous gates pass, test measured
   5.5 Ω tips first on bench DC. Test a 2.5 Ω tip only after its worst-case
   current is covered by the dummy-load evidence. Unknown resistance stays
   heater-off.
7. **Source checks:** for a tip that passes on DC, test the wall charger and
   then the USB-C battery, one source per run condition. A battery source must
   be identified and its interface/protections confirmed first.
8. **Held-out review:** freeze the probe and classification rule and repeat on
   held-out samples/units. The one-S99 setup is exploratory and cannot complete
   cross-revision release qualification.

The plan does not set probe pulse timing, energy limits, temperature cutoffs,
tip-cooldown limit, or classifier thresholds. Those must come from measurements
and safety review; do not fill them with guesses. Lower supply voltage is not
automatically safer because it can weaken MOSFET gate drive.

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
live use. Before any eventual fixture integration, replace templates with the
one identified S99, three identified sources, six measured tips, and their
calibration/cutoff evidence. Keep one source selected per run; the runner
enforces disable, disconnect, bus-discharge, select, connect, and stable-source
order in simulation.

`manifests/supply-matrix.json` lists source classes covered by offline
control-flow tests. These are separate synthetic runs. They do not qualify the
specific bench supply, wall charger, USB-C battery, cable, or contract point in
the requested setup.

Tip changes are manual between completed runs. The agent must end a run only
after shutdown, source disconnect, bus-discharge verification, and cooldown.
The person can then change the tip and start a separate run; the runner must not
pause for or request an in-run tip change.

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
source-switch, heater-actuation, or qualified independent-cutoff adapter has
been reviewed or integrated. Do not remove that refusal until the physical
interlocks and cutoff have been independently verified, fault-injection
qualified, and integrated through reviewed drivers.

The `S99 auto-tip HIL harness` workflow runs only offline checks on GitHub-hosted
runners. Its explicit hardware request is constrained to self-hosted runners
tagged `s99-hil` and `hardware`, checks the runner's `S99_HIL_CONFIG` variable,
and currently records the fail-closed live-mode refusal. A future physical run
must remain noninteractive once started, have the independent human-supervision
interlock closed, and be physically monitored throughout every hot stage.
