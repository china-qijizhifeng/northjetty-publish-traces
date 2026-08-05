#!/usr/bin/env python3
"""Validate a generated NorthJetty trace site without opening a browser."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


MARKER_NAME = ".northjetty-trace-site"


def fail(message: str) -> None:
    raise ValueError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", help="Generated site directory.")
    args = parser.parse_args()
    site = Path(args.site).expanduser().resolve()

    try:
        if not site.is_dir():
            fail(f"site directory not found: {site}")
        for required in ("index.html", "manifest.json", MARKER_NAME):
            if not (site / required).is_file():
                fail(f"missing required file: {required}")

        manifest = json.loads((site / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != 1:
            fail(f"unsupported manifest version: {manifest.get('version')!r}")
        chunk_bytes = manifest.get("chunk_bytes")
        if not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
            fail("manifest chunk_bytes must be a positive integer")
        groups = manifest.get("groups")
        traces = manifest.get("traces")
        if not isinstance(groups, list) or not isinstance(traces, list):
            fail("manifest groups and traces must be arrays")

        group_ids = {group.get("id") for group in groups if isinstance(group, dict)}
        if None in group_ids or len(group_ids) != len(groups):
            fail("manifest group IDs must be present and unique")

        total_bytes = 0
        referenced_parts: set[Path] = set()
        for index, trace in enumerate(traces):
            if not isinstance(trace, dict):
                fail(f"trace #{index} is not an object")
            if trace.get("group") not in group_ids:
                fail(f"trace #{index} references unknown group {trace.get('group')!r}")
            expected_size = trace.get("size")
            parts = trace.get("parts")
            if not isinstance(expected_size, int) or expected_size <= 0:
                fail(f"trace #{index} has invalid size")
            if not isinstance(parts, list) or not parts:
                fail(f"trace #{index} has no parts")

            actual_size = 0
            for raw_part in parts:
                if not isinstance(raw_part, str) or not raw_part:
                    fail(f"trace #{index} contains an invalid part path")
                part = (site / raw_part).resolve()
                try:
                    part.relative_to(site)
                except ValueError:
                    fail(f"part escapes site directory: {raw_part}")
                if not part.is_file():
                    fail(f"missing part: {raw_part}")
                part_size = part.stat().st_size
                if part_size <= 0 or part_size > chunk_bytes:
                    fail(f"invalid part size for {raw_part}: {part_size}")
                if part in referenced_parts:
                    fail(f"part referenced more than once: {raw_part}")
                referenced_parts.add(part)
                actual_size += part_size
            if actual_size != expected_size:
                fail(
                    f"trace #{index} size mismatch: manifest={expected_size}, parts={actual_size}"
                )
            total_bytes += actual_size

        data_dir = site / "data"
        disk_parts = {path.resolve() for path in data_dir.rglob("*.part*") if path.is_file()}
        orphaned = sorted(disk_parts - referenced_parts)
        if orphaned:
            fail(f"found {len(orphaned)} unreferenced part file(s)")

        print(f"Site: {site}")
        print(f"Groups: {len(groups)}")
        print(f"Traces: {len(traces)}")
        print(f"Parts: {len(referenced_parts)}")
        print(f"Trace bytes: {total_bytes}")
        print("Validation: OK")
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
