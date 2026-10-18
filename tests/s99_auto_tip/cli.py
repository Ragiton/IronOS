"""Command-line entry points for offline S99 trace simulation and replay."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .analysis import compare_observed_ranges
from .model import SimulationInputError, simulate_scenario
from .trace import TraceValidationError
from .trace_io import load_csv, load_json, write_csv, write_json


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric value {value}")


def _load_scenario(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, parse_constant=_reject_non_finite)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SimulationInputError(f"could not read scenario JSON: {error}") from error


def _load_trace(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".csv":
        return load_csv(path)
    return load_json(path)


def _safe_stem(trace_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", trace_id).strip("._")
    return stem or "trace"


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    simulate = commands.add_parser("simulate", help="generate synthetic traces from explicit scenario inputs")
    simulate.add_argument("--config", type=Path, required=True, help="s99.simulation.v1 JSON scenario")
    simulate.add_argument("--out-dir", type=Path, required=True, help="directory for JSON and CSV traces")

    analyze = commands.add_parser("analyze", help="replay traces and report observed feature overlap")
    analyze.add_argument("traces", type=Path, nargs="+", help="s99.trace.v1 JSON or CSV records")
    analyze.add_argument("--feature", choices=("temperature_rise_c", "maximum_temperature_rise_c"), default="temperature_rise_c")
    analyze.add_argument("--output", type=Path, help="report JSON path (defaults to stdout)")

    convert = commands.add_parser("convert", help="convert a trace between canonical JSON and companion CSV")
    convert.add_argument("source", type=Path, help="input JSON or CSV trace")
    target = convert.add_mutually_exclusive_group(required=True)
    target.add_argument("--to-csv", type=Path, metavar="PATH")
    target.add_argument("--to-json", type=Path, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "simulate":
            traces = simulate_scenario(_load_scenario(args.config))
            args.out_dir.mkdir(parents=True, exist_ok=True)
            stems = [_safe_stem(trace["trace_id"]) for trace in traces]
            if len(set(stems)) != len(stems):
                raise SimulationInputError("trace IDs collide after conversion to safe output filenames")
            for trace, stem in zip(traces, stems):
                write_json(trace, args.out_dir / f"{stem}.json")
                write_csv(trace, args.out_dir / f"{stem}.csv")
            print(f"wrote {len(traces)} synthetic trace(s) to {args.out_dir}")
            return 0
        if args.command == "analyze":
            traces = [_load_trace(path) for path in args.traces]
            report = compare_observed_ranges(traces, feature=args.feature)
            _write_report(report, args.output)
            return 0
        if args.command == "convert":
            trace = _load_trace(args.source)
            if args.to_csv is not None:
                write_csv(trace, args.to_csv)
            else:
                write_json(trace, args.to_json)
            return 0
    except (OSError, TraceValidationError, SimulationInputError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
