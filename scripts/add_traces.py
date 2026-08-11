#!/usr/bin/env python3
"""Copy traces into a canonical YYYY-MM-DD/SCENE trace store."""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from build_trace_site import (
    DEFAULT_PATTERNS,
    GROUP_PATTERN,
    SKILL_ROOT,
    BuildError,
    infer_timestamp,
    is_within,
    valid_date_name,
    validate_trace,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-root",
        required=True,
        help="Destination root. Files are stored below YYYY-MM-DD/SCENE/.",
    )
    parser.add_argument(
        "--scene",
        required=True,
        help="Scene ID used as the second-level directory.",
    )
    parser.add_argument(
        "--date",
        default="auto",
        help=(
            "Storage date in YYYY-MM-DD. Default 'auto' uses a timestamp in the "
            "filename, then the source mtime."
        ),
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=[],
        help=(
            "Recursive filename pattern for directory inputs. Repeat to add patterns. "
            "Defaults: *.trace.json and *.trace.json.gz."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print destinations without creating directories or copying files.",
    )
    parser.add_argument("sources", nargs="+", help="Trace files or directories to add.")
    return parser.parse_args()


def validate_trace_root(raw_root: str) -> Path:
    trace_root = Path(raw_root).expanduser().resolve()
    if trace_root in {Path("/").resolve(), Path.home().resolve()}:
        raise BuildError(f"Refusing broad trace root: {trace_root}")
    if is_within(trace_root, SKILL_ROOT):
        raise BuildError(
            "Trace root must be outside the installed skill so the skill remains zero-data"
        )
    if trace_root.exists() and not trace_root.is_dir():
        raise BuildError(f"Trace root exists and is not a directory: {trace_root}")
    return trace_root


def collect_inputs(raw_sources: list[str], patterns: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()
    for raw_source in raw_sources:
        source = Path(raw_source).expanduser().resolve()
        if not source.exists():
            raise BuildError(f"Trace source does not exist: {source}")
        if source.is_file():
            if not any(source.match(pattern) for pattern in patterns):
                raise BuildError(
                    f"Trace source does not match configured patterns: {source}"
                )
            found.add(source)
            continue
        if not source.is_dir():
            raise BuildError(f"Trace source is neither a file nor directory: {source}")
        for pattern in patterns:
            found.update(
                path.resolve() for path in source.rglob(pattern) if path.is_file()
            )
    if not found:
        raise BuildError("No trace files matched the supplied sources")
    return sorted(found, key=lambda path: str(path))


def files_are_same(source: Path, destination: Path) -> bool:
    if source == destination:
        return True
    return filecmp.cmp(source, destination, shallow=False)


def copy_trace(source: Path, destination: Path, dry_run: bool) -> str:
    if destination.exists():
        if files_are_same(source, destination):
            return "unchanged"
        raise BuildError(
            f"Refusing to overwrite a different trace at {destination}; rename the source "
            "or choose another scene/date"
        )
    if dry_run:
        return "planned"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.copy-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "added"


def main() -> int:
    args = parse_args()
    try:
        trace_root = validate_trace_root(args.trace_root)
        if not GROUP_PATTERN.fullmatch(args.scene):
            raise BuildError(
                "--scene must use 1-64 letters, digits, dot, underscore, or hyphen"
            )
        if args.date != "auto" and not valid_date_name(args.date):
            raise BuildError("--date must be 'auto' or a valid YYYY-MM-DD date")

        patterns = tuple(args.pattern) if args.pattern else DEFAULT_PATTERNS
        sources = collect_inputs(args.sources, patterns)
        counts = {"added": 0, "unchanged": 0, "planned": 0}
        for source in sources:
            validate_trace(source)
            timestamp_epoch, _ = infer_timestamp(source)
            date_name = (
                datetime.fromtimestamp(timestamp_epoch, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                )
                if args.date == "auto"
                else args.date
            )
            destination = trace_root / date_name / args.scene / source.name
            result = copy_trace(source, destination, args.dry_run)
            counts[result] += 1
            print(f"{result}: {source} -> {destination}")

        print(f"Trace root: {trace_root}")
        print(f"Added: {counts['added']}")
        print(f"Unchanged: {counts['unchanged']}")
        if args.dry_run:
            print(f"Planned: {counts['planned']}")
        return 0
    except (BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
