"""Command line entry point for offline HIL runs."""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

from .model import HILError, read_json
from .runner import load_catalogs, run_manifest


PACKAGE_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one noninteractive S99 HIL manifest.")
    parser.add_argument("--manifest", type=Path, required=True, help="test manifest JSON")
    parser.add_argument("--catalog-dir", type=Path, default=PACKAGE_DIR / "manifests", help="source/cable/debug/cartridge catalog directory")
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/s99-auto-tip-hil"), help="directory for immutable run bundles")
    parser.add_argument("--mode", choices=("dry-run", "live"), default="dry-run", help="defaults to offline dry-run")
    parser.add_argument("--fixture-config", type=Path, help="explicit, reviewed fixture configuration required for live mode")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = read_json(args.manifest)
        catalogs = load_catalogs(args.catalog_dir)
        fixture_config = read_json(args.fixture_config) if args.fixture_config else None
        run_dir, exit_code = run_manifest(manifest, catalogs, args.output_root, args.mode, fixture_config)
    except (HILError, OSError, json.JSONDecodeError) as exc:
        print(f"HIL run refused: {exc}", file=sys.stderr)
        return 3
    if run_dir is not None:
        print(f"HIL result: {run_dir}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
