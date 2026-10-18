# S99 C245 startup auto-detection: implementation and validation plan

**Status:** planning only. No firmware change or hardware test has been performed by this plan.

**Target:** distinguish the known 2.5 Ω and 5.5 Ω C245 cartridge profiles at S99 startup, with the iron cold and the tip unloaded. The candidate is a bounded thermal-response classifier. It would infer a cartridge class from the tip-temperature response to a controlled probe; it would not measure resistance directly.

**Decision rule:** do not implement or enable software-only detection unless the team demonstrates a repeatable, discriminating probe that stays within independently verified electrical and thermal limits on every supported S99 revision and source type. If the stock hardware cannot meet that bar, document the required hardware changes or reject the software-only feature.

## 1. Repository and current behavior

GitHub issue searches performed on 2026-10-18 for `S99 auto detect`, `C245 resistance`, and `S99 tip resistance` did not find an existing issue for this feature. The generic `auto-detect tip` search returned closed issue [#1387, “Calibrate Temperature”](https://github.com/Ralim/IronOS/issues/1387), which is about temperature calibration rather than S99 cartridge identification. No matching issue or pull request for S99 startup resistance auto-detection was found.

Relevant open work is safety-related and must remain an explicit integration gate:

- [Issue #2244](https://github.com/Ralim/IronOS/issues/2244) records the S99/JBC cartridge problem.
- [PR #2248](https://github.com/Ralim/IronOS/pull/2248) is experimental S99 2.5 Ω output-stage work. Its reports include 20 V / 2.5 Ω operation producing 8 A pulses, a measured MOSFET gate turn-off tail of roughly 35–48 µs, charger trips under several pulse profiles, MOSFET heating, and problematic gate drive at low input voltage. The proposed 15 V / 6 A or PPS/no-chop paths are not a validated safety guarantee.
- [Draft PR #2250](https://github.com/Ralim/IronOS/pull/2250) is PID tuning work stacked on #2248. The changed controller can alter the thermal transient used by a classifier, so the feature must be qualified against both controller configurations.

The current `dev` branch is the feature branch base. The two open PRs are not assumed to be merged or available as safety protection. Battery and power-bank qualification is an identified gap in the reported S99 experiments.

Code references on the current `dev` base:

- [`configuration.h`](../source/Core/BSP/Sequre/configuration.h#L280-L288) sets `TIP_RESISTANCE` to 55 (5.5 Ω), enables manual `TIPTYPE_C245` selection, and does not define `AUTO_TIP_SELECTION` or enable a S99 `Tip Sense` path. The S99 menu therefore supports selecting 5.5 Ω or 2.5 Ω manually; hardware auto-sense is unsupported.
- [`settings.yaml`](../source/Settings/settings.yaml#L272-L276) defaults `SolderingTipType` to enum value 0. [`Settings.h`](../source/Core/Inc/Settings.h#L170-L173) identifies that first C245 option as 5.5 Ω, and [`Settings.cpp`](../source/Core/Src/Settings.cpp#L323-L329) maps it to resistance value 55. This is the current startup profile unless a saved manual selection overrides it.
- [`BSP.cpp`](../source/Core/BSP/Sequre/BSP.cpp#L466-L492) uses the selected resistance or the compile-time default. The shared `COPPER_HEATER_COIL` branch at lines 467–480 contains a 5.0 Ω cold-start value, but the S99 configuration does not enable that macro; it is not the S99 startup behavior.
- [`getTipChopDutyX256()`](../source/Core/BSP/Sequre/BSP.cpp#L170-L221) estimates tip current from measured input voltage and configured resistance, compares it with negotiated PD source-current data or the user power limit, and caps PWM duty. This is a software estimate and an average-duty cap. It is not live current sensing, and it does not clamp the peak current inside each MOSFET pulse. Do not treat it as overcurrent protection for a detection pulse.

## 2. Safety questions to resolve before implementation

The thermal-response method is only a hypothesis until testing proves it. Response may vary with cartridge sample, cold resistance, ambient and initial tip temperature, sensor coupling, thermocouple noise, sampling phase, startup voltage, supply behavior, and firmware control-loop settings. Contact with a cold workpiece would change the response; all classifier trials must therefore use a mechanically verified no-contact fixture.

Before writing firmware, research the exact S99 revisions, schematics, BOM, gate-drive topology, thermocouple sampling sequence, MOSFET safe operating area, tip current waveform, and whether the live board has any hardware protection not represented in firmware. Current hardware review reports no onboard current-sense circuit or power-feedback loop, and the firmware exposes no live current-sense signal. Confirm those facts against each revision’s schematic and measurements; assume no active current feedback until proven otherwise. Confirm the debug interface and power-path isolation.

The low-resistance case is the limiting case. A low PWM duty reduces average energy but does not reduce the instantaneous current during the on portion of a pulse. At 20 V into 2.5 Ω, the idealized on-pulse current is 8 A. The gate delay and turn-off tail can make actual conduction longer than the command. A lower supply voltage is not automatically safer: #2248 reports weak gate drive and MOSFET heating in low-voltage/high-frequency cases. Do not run a real-tip ramp or a low-voltage live test until gate-source voltage and current waveforms establish a safe envelope.

An S99 power supply can trip before the MCU observes a thermal change. The probe schedule must never depend on the charger tolerating an unknown peak-current pulse. Define hard current, energy-per-pulse, total-energy, duration, input-voltage, MOSFET temperature, handle temperature, and tip-temperature limits from component ratings and instrumented measurements before the first powered probe.

## 3. Proposed firmware design after the safety gates

Keep the existing manual 5.5 Ω and 2.5 Ω choices. A manual choice bypasses detection and retains the existing behavior. Add an explicit Auto choice only after the research, model, and rig gates pass; keep it opt-in until qualification is complete. Preserve Auto as the user’s setting and hold the detected class in runtime state for that boot, rather than silently rewriting the saved manual selection.

Preserve settings compatibility. The current first C245 enum value and fresh-settings default are 5.5 Ω. Do not insert Auto ahead of existing enum values and reinterpret saved value 0. Append a new value or provide an explicit, tested migration; existing devices and fresh defaults must continue to select manual 5.5 Ω unless the user opts in.

The candidate detector should collect a heater-off baseline, execute only a pre-approved fixed probe schedule, and evaluate temperature change/slope over calibrated time windows. Freeze all pulse timing and decision thresholds from measured reference samples before held-out qualification. Account for input voltage, negotiated source state, sampling windows, and tip-temperature validity; do not infer available energy from PWM duty alone. A source-contract change or source instability invalidates the measurement and leaves heat disabled until a new safe start is explicitly requested.

The classifier returns only 2.5 Ω, 5.5 Ω, or unknown/ambiguous. Unknown, invalid, or low-confidence results leave the heater off and request manual selection. A recognized class may choose its matching profile only after the source is stable and the resulting power envelope has separately passed the applicable source and output-stage qualification. Detection must never increase the allowed power by itself. If these steps cannot be made safe on existing S99 hardware, do not add Auto mode; propose board changes or reject the feature.

## 4. Hardware and fixture plan

The rig has two independent layers: an agent-controlled experiment host and a fail-open safety controller that can remove input power without cooperation from the DUT, host, or firmware. A physically present human supervises every live test and has access to an E-stop. The human does not need to press buttons, answer prompts, reconnect cables, change supplies, or advance test stages during a run.

| Item | Required capability | Purpose and constraints |
|---|---|---|
| S99 test units | Multiple identified board revisions, including a sacrificial unit for electrical probing | Record exact model/revision, firmware, and device ID. Keep separate fixtures with preinstalled 5.5 Ω and 2.5 Ω cartridges so an agent can run both classes without an in-run human cartridge swap. Add a qualified automatic cartridge changer only if a later qualification requires it; it must prove power-off and cool state before moving a cartridge. |
| Cartridge set | Multiple genuine/known 5.5 Ω and 2.5 Ω C245 samples | Measure each sample cold using a four-wire meter; keep IDs tied to test logs. Include variation across samples and vendors if the feature intends to support clones. Unknown cartridges are negative-classification cases. |
| Programmable PD/PPS source | Programmable fixed PDO and PPS behavior with remote control and configurable limits | Reproduce fixed-PDO and PPS paths at 65 W and 100 W classes. Record negotiated contract and source-side VBUS/current. |
| Wall PD chargers | Representative 65 W and 100 W fixed-PDO and PPS chargers, including the Sequre-supplied unit and known comparison units such as the Lenovo 65 W unit if available | Validate charger-specific transient/OCP behavior. A successful test on one charger does not qualify other models. |
| Power banks | PD and PPS models from the intended use range | Cover source state, advertised PDOs/PPS range, and shutdown/retry behavior. Test only after the protected emulator and wall-source stages pass. |
| Protected DC source | Remotely programmable isolated bench supply with external enable and documented current-limit behavior | Exercise DC input without relying on a USB-PD contract. Its current limit must be independently characterized for pulse response; a bench supply’s display is not sufficient protection. |
| Battery emulator and packs | Isolated programmable battery emulator first; later, protected packs with BMS, fuse, and enclosure selected for the S99’s rated input range | Emulate battery impedance and sag before using chemical cells. Actual packs are a late-stage source and must not be loose or unprotected. Battery results are currently missing from the S99 experimental record. |
| Source selector | Remotely controlled, rated CC/VBUS switching with break-before-make, discharge verification, and source interlock | Exactly one power source may connect to a DUT at a time. Disable the old source, verify VBUS decay, isolate CC/VBUS, select the next source, reconnect, and renegotiate before a test starts. Never parallel or OR sources. |
| PD analyzer | Inline analyzer that logs CC negotiation, PDO/PPS contract, renegotiation, detach, and reset | Detect source selection failures, charger trips, and contract changes. Verify that the analyzer does not materially alter the power path. |
| Current and voltage instrumentation | Calibrated low-inductance shunt or current probe with bandwidth and sample rate sufficient for microsecond-scale pulse/tail measurement; differential VBUS and DUT input probes; oscilloscope with suitable bandwidth/sample rate | Resolve pulse peak current, turn-on/off timing, ringing, voltage sag, and input energy. Set acquisition requirements from measured edge times; a starting procurement target is at least 50 MHz analog bandwidth and 100 MS/s, subject to verification against probe response and the observed waveforms. |
| Gate instrumentation | Isolated gate-to-source probe on the sacrificial DUT; appropriate differential probe for drain/source behavior | Measure actual MOSFET enhancement, transition, and turn-off tail. Do not attach a grounded probe that can short a floating or switching node. |
| Temperature instrumentation | Calibrated tip, handle, and MOSFET-area sensors; thermal camera where useful | Cross-check MCU thermocouple readings and catch runaway or localized heating. Sensor placement must not change the tip’s thermal response or electrical isolation. |
| UART/debug path | Galvanically isolated UART with VBUS/power conductor omitted, attached to dedicated debug pads or a verified data-only path | Keep telemetry/debug connected while PD, PPS, DC, battery emulator, or power-bank power is applied and while the source selector changes. Prevent debug equipment from back-feeding or becoming a second source. Use SWD/bootloader connections only through a verified isolated path; otherwise flash with power removed. |
| Independent safety controller | Separate controller with watchdog, fail-open disconnect, fast hardware overcurrent cutoff (comparator/e-fuse or equivalent), hard energy/time limits, overtemperature input, interlock inputs, and latching E-stop | Its fast cutoff must respond within the approved pulse envelope without Python, host, or DUT cooperation. It must remove power if the host, DUT, telemetry, or its own communications fail. Set limits independently of firmware and make them impossible for the experiment script to raise above approved bounds. |
| Host, logger, and fixture | Dedicated computer, instrument-control interfaces, fire-resistant fixture, guarded hot area, and device/source identification | Run noninteractive test manifests, collect all data, and keep powered DUTs physically contained. Use separate, preloaded DUT stations for cartridge classes so tests run without human input. |

The electrical test path must account for every conductor from each supply, debug adapter, PD analyzer, oscilloscope, and safety controller. Verify isolation and back-feed behavior with dummy loads before connecting an S99. No source combination is allowed unless the test case explicitly identifies one source and every other attached interface is proven not to supply power.

## 5. Agent-run test harness and telemetry

Implement a declarative, noninteractive test runner. A test manifest should identify the DUT/revision, installed cartridge/sample ID, supply, cable, firmware/build hash, manual tip setting, probe profile, repetitions, and approved hard limits. One command must execute a stage, sequence instruments and source switching, capture traces, evaluate pass/fail, and export a signed or checksummed result bundle. No live-stage prompts or manual button presses are permitted.

The host state machine should require: safety controller self-test; E-stop/interlock closed; all source outputs disabled; exactly one source selected; debug path isolated; tip/handle/MOSFET-area temperatures valid; cartridge fixture position and no-contact state confirmed; instrument calibration current; firmware/build identity recorded; and source voltage/current settings within the manifest. The independent safety controller remains authoritative and fail-open.

Capture synchronized timestamps for at least:

- firmware build, S99 revision/device identity, active profile, settings, boot/reset count, and test-run ID;
- raw tip-temperature/ADC samples, measurement timing, tip sensor validity, ambient temperature, handle temperature, and external MOSFET temperature;
- PWM command and actual gate waveform, pulse widths, frequency, current waveform, VBUS/DUT input voltage, and computed energy per pulse/run;
- PD/PPS contract, source current limit, renegotiations/detach, source-side current, charger status, and any BMS/bench-source faults;
- UART/debug continuity, relay/selector state, interlock/E-stop state, watchdog events, and shutdown cause;
- cold four-wire resistance for the installed cartridge, measured before the run and tied to its cartridge ID.

On any fault, the safety controller must latch the disconnect, the host must save the current trace and event timeline, and the run must stop. The script must not automatically retry, reconnect, or continue to another test after a fault. A human may inspect and reset the fixture after the hot area is safe; that reset is not part of an agent test run.

## 6. Supply and operating-condition matrix

Run the source matrix sequentially. For every source change, the rig must open the current path, verify the DUT bus is discharged, break CC/VBUS, select exactly one next source, reconnect, and wait for a stable contract before permitting a pulse. UART telemetry remains connected through its isolated, data-only path. There is no simultaneous supply mixing.

| Source class | Planned cases | Entry gate |
|---|---|---|
| Protected bench DC | Current-limited, remotely controlled DC source at progressively approved input points | Output-stage waveform and external cutoff are proven first. Current limit and cutoff response are characterized with a dummy load. |
| USB-PD fixed PDO | Programmable 65 W and 100 W fixed-PDO cases; representative real 65 W and 100 W wall chargers | Scope confirms a safe pulse envelope for the specific voltage/current; PD analyzer confirms stable contract. Include the Sequre charger and at least one known comparison charger. |
| USB-PD PPS | Programmable PPS source and real 65 W/100 W PPS chargers across selected approved contract points | Treat PPS as a different power-path condition. Validate each chosen voltage/current point and whether chopping is active; no extrapolation from fixed PDO. |
| Power banks | PD and PPS banks across charged and lower state-of-charge conditions | Enter after wall/PPS and protected emulator gates; log contract, sag, shutdown, and recovery. |
| Battery emulation | Isolated emulator with programmed source resistance/sag and cutoff behavior | Enter after the same waveform limits are demonstrated on protected DC. Model battery/BMS response and cutoff before a live pack. |
| Protected battery packs | Enclosed, fused, BMS-protected pack(s) within the verified S99 input range | Last source class. Require human presence, independent hard cutoff, pack temperature/current monitoring, and successful emulator qualification. |

Cross each source class with the 5.5 Ω and 2.5 Ω samples, S99 hardware revisions, cold/ambient starting points, and relevant firmware configurations. Begin with a pairwise risk-based matrix. Expand to a full cross-product for release qualification wherever hardware or source behavior could interact. Exercise cable and connector variants separately. Do not represent a single charger or one board revision as qualification for all S99 units.

## 7. Staged validation sequence

Each stage runs from a test manifest without operator input. The human remains physically present during all live-power stages and monitors the guarded hot area. Stop at the first failed gate; preserve logs and do not retry automatically.

### Stage 0 — design and source audit

Identify every supported S99 revision, schematic/BOM, sensor/sample timing, output topology, MOSFET/gate-driver limits, and debug/power isolation. Trace startup behavior from settings load through `preStartChecks()` and first nonzero PWM. Determine which signals are available while the heater is off and while it is on. Verify the hardware has no assumed current-sense path or auto-ID input unless the schematic and measurements prove otherwise.

Review #2248 and #2250 as unmerged experimental work. Establish the baseline on current `dev` and make a separate integration test plan for the #2248 and #2250 patches. Do not take their proposed current limits, frequencies, or controller gains as safety-qualified.

### Stage 1 — build, offline model, and recorded-trace replay

Build the S99 firmware and preserve its image/hash as the baseline. Develop an offline thermal/electrical model for candidate pulses using measured cartridge resistance curves, sensor timing, thermal response, input voltage, gate delay, and uncertainty bands. Replay existing traces where available; collect no-power sensor and ADC noise data.

Sweep the model over cold resistance, cartridge/sample spread, ambient temperature, starting tip temperature, ADC noise, sensor coupling, PWM timing, thermocouple sample windows, and plausible supply response. Find whether the 2.5 Ω and 5.5 Ω response distributions can be separated before the energy or pulse envelope reaches the approved limits. If safe distributions overlap or only separate after an unsafe pulse, stop and assess a hardware change or reject software-only detection.

### Stage 2 — harness, isolation, and fault injection with dummy loads

With no S99 tip installed, verify source switching, CC/VBUS break-before-make, bus discharge, PD renegotiation, isolated UART continuity, logging, host watchdog, independent controller watchdog, E-stop, hard cutoff, and fail-open behavior. Use noninductive dummy loads and safe source settings to inject open/short load, overcurrent, overtemperature, telemetry loss, debug loss, host crash, controller reset, stale temperature, relay fault, source detach, PD renegotiation, and BMS/bench-source cutoff.

Pass only if each fault drops power and latches the run before any software-only recovery can energize the DUT. Verify the telemetry bundle contains enough information to diagnose each injected fault. Confirm all tests run without prompts or human switching.

### Stage 3 — cold passive characterization

With power removed, verify the fixture measures cartridge ID/position, four-wire resistance, continuity, ambient/tip/handle temperature, and no-contact position. Characterize multiple known 2.5 Ω and 5.5 Ω samples over the intended cold-start range. Measure normal plug/contact variation and sensor noise without energizing the heater. Confirm the machine can reject a missing, open, shorted, unknown, warm, or mechanically contacted cartridge state.

### Stage 4 — output-stage characterization on dummy loads

Use the sacrificial S99 and noninductive loads representing the lowest-resistance case before fitting a real cartridge. Start at the lowest approved energy and use the independent cutoff. Capture gate-source, drain/source, VBUS, and current waveforms at each approved input point. Establish actual peak current, pulse shape, gate rise/fall, turn-off tail, switching loss, and temperature behavior. Include any no-chop or fixed-on state that can occur at a candidate supply point.

Do not proceed to a live-tip probe until gate drive, pulse current, MOSFET operating point, and supply response stay inside the pre-approved limits with margin. Do not use low input voltage as a safety shortcut; qualify its gate-drive behavior independently. If safe limits cannot be established, stop and define the required board-level current limiter, gate-driver, current-sense, or cartridge-identification change.

### Stage 5 — bounded no-contact tip probes

Only after Stages 0–4 pass, fit preinstalled, measured cartridges in a no-contact fixture. Test the 5.5 Ω class first. Test 2.5 Ω only after the dummy-load evidence specifically covers its worst-case cold resistance and peak current. Apply one conservative, bounded probe schedule at a time; store the raw thermal and electrical traces. Do not infer safety from low PWM duty alone.

The classifier must return one of: known 2.5 Ω, known 5.5 Ω, or unknown/ambiguous. Unknown, stale, noisy, warm, externally heat-sunk, or out-of-range observations must fail closed: heater disabled and manual confirmation required. A low-confidence result must never be rounded to the 5.5 Ω profile. The test fixture must validate both correct classifications and every failure-to-detect path.

### Stage 6 — controlled source matrix

After the bounded tip probes pass on the protected DC source, execute the source matrix one source at a time: bench DC, fixed-PDO PD, PPS, wall chargers, power banks, battery emulator, and protected battery packs in that order. Initially keep probe energy unchanged; do not let the classifier itself raise the power envelope. Change one factor at a time during characterization so charger, cable, source mode, board revision, and cartridge effects remain identifiable.

Trip, reset, unexpected contract change, VBUS sag beyond its approved limit, source cutoff, abnormal gate waveform, or MOSFET/handle/tip temperature excursion is a stop condition. Save the trace, latch off, and require a reviewed cause before another run.

### Stage 7 — blinded held-out qualification

Freeze the probe schedule and classifier threshold before qualification. Use cartridges and S99 units withheld from model fitting. The early screening target is at least 30 cold starts per tip class per represented condition with zero dangerous errors, where a 2.5 Ω cartridge classified as 5.5 Ω is dangerous. Misclassifying 5.5 Ω as 2.5 Ω is also a failure for product acceptance, though it is not the same overcurrent hazard. Qualification must include multiple samples, S99 revisions, and source classes.

Run a larger blinded release sample with an explicit statistical target. A pooled zero-failure sample of 300 gives only an approximate 1% upper 95% bound on the failure rate; it is not, by itself, evidence for a safety claim or permission to enable full power. If the project needs a tighter bound, size the sample accordingly. Separate class, revision, and source strata when reporting results; do not hide a weak subgroup in a pooled pass rate.

## 8. Pass/fail criteria and fail-safe behavior

The plan may move to firmware implementation only when all of these gates pass:

1. **Hardware envelope:** independently measured current, energy, voltage, gate, MOSFET, and temperature limits exist for the worst-case cold 2.5 Ω cartridge. Margin is documented against component ratings and supply behavior.
2. **Discriminability:** held-out 2.5 Ω and 5.5 Ω response distributions separate within the safe probe envelope across represented samples and conditions. A dangerous 2.5 Ω-to-5.5 Ω classification is zero in the pre-registered acceptance set.
3. **Fail closed:** missing tip, unknown tip, ambiguous score, sensor fault, stale sample, hot start, workpiece contact, source instability, invalid PD state, telemetry loss, or fixture/interlock fault leaves heater output off and requires explicit manual action or service reset.
4. **Independent cutoff:** fault injection proves the safety controller removes power if IronOS, the S99 MCU, the host, UART, or instrument control freezes or disconnects. The firmware never raises hardware cutoff limits.
5. **Source qualification:** the selected supply matrix passes without charger/BMS trips, unexpected resets, unsafe voltage sag, thermal runaway, or gate-drive excursions. Each charger model/PPS point is reported separately.
6. **Reproducibility:** all traces, instrument calibration, firmware hashes, source profiles, cartridge resistance, and decisions are captured in machine-readable bundles. A second agent can replay the exact manifest and obtain the same classification and safety outcome without interactive input.
7. **Integration:** the feature passes against current upstream `dev`, the #2248 2.5 Ω output-stage work, and the #2250 PID draft (or their merged/rebased equivalents). PID changes must not invalidate the classifier response envelope. Re-run all affected stages after any output-stage, ADC timing, PD, or PID change.

Immediate abort-and-latch conditions include a hard current or energy limit; unexpected input voltage or source-current contract; PD detach/renegotiation or charger/BMS cutoff; MOSFET gate remaining partially enhanced; excessive turn-off tail; abnormal MOSFET, handle, or tip temperature/rate; thermal runaway; stale or missing telemetry; loss of any interlock; relay/source ambiguity; host or safety-controller watchdog event; or a classification confidence below the frozen threshold. Save traces and do not automatically retry.

Every live test stage requires a physically present human supervising heat risk and an accessible E-stop. Test scripts must be agent-executable from start to result without human input. Do not enable unattended hot testing unless the independent interlocks, hardware cutoff, fixture containment, and fault-injection suite have all been verified and explicitly reviewed.

## 9. CI, implementation gates, and release

The current CI in [`.github/workflows/push.yml`](../.github/workflows/push.yml) builds the model matrix and runs existing Python/BriefLZ checks; it has no S99 hardware-in-loop suite. Keep ordinary CI safe and reproducible without connected hardware:

- add deterministic host-side tests for classifier scoring, threshold boundaries, invalid/stale inputs, persistence/manual override behavior, and all fail-closed states;
- add model/replay tests using versioned input traces and assertions on classification, pulse schedule, energy, timeouts, and source-state transitions;
- retain the existing S99 and cross-model build matrix, checking flash/RAM impact and ensuring other Sequre models remain unchanged;
- create a separately gated hardware-runner job that accepts an explicit fixture/manifest, reports its calibration and interlock status, and uploads full traces. It must not silently run live power tests on a generic GitHub-hosted runner;
- make hardware results required before merge/release only after the rig is commissioned. Include a manual reviewer sign-off on the human-supervised safety evidence and a link to the machine-readable result bundle.

Begin firmware implementation only after the research/model gate, output-stage safety gate, and fixture gate are approved. Keep auto-detection disabled or opt-in until blinded qualification and #2248/#2250 integration pass. Preserve manual selection. Do not let detection alone select a higher power envelope; any subsequent power increase must remain within separately qualified supply and output-stage limits.

If no safe, discriminating probe exists on stock S99 hardware, stop software-only work. Produce a hardware-change proposal—such as a real current-sense path with fast independent cutoff, stronger/verified gate drive, or a direct cartridge-ID mechanism—or close the feature as infeasible for existing hardware.
