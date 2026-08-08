#!/usr/bin/env python3
"""Build a zero-data or trace-backed static Perfetto viewer for NorthJetty."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_CHUNK_BYTES = 8_000_000
DEFAULT_PATTERNS = ("*.trace.json", "*.trace.json.gz")
MARKER_NAME = ".northjetty-trace-site"
MIN_TRACE_EPOCH = 946_684_800  # 2000-01-01 UTC
MAX_TRACE_EPOCH = 4_102_444_800  # 2100-01-01 UTC
SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = SKILL_ROOT / "assets" / "index.html"


class BuildError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a static Torch/Chrome trace browser. With no --group arguments, "
            "the result is a valid empty viewer that still accepts local drag-and-drop."
        )
    )
    parser.add_argument("--output", required=True, help="Output site directory.")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="ID=PATH",
        help=(
            "Trace group and source path. PATH may be a trace file or directory; "
            "repeat to add groups or multiple paths to one group."
        ),
    )
    parser.add_argument(
        "--group-label",
        action="append",
        default=[],
        metavar="ID=LABEL",
        help="Optional display label for a group; repeat as needed.",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=[],
        help=(
            "Recursive filename pattern for directory sources. Repeat to add patterns. "
            "Defaults: *.trace.json and *.trace.json.gz."
        ),
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=DEFAULT_CHUNK_BYTES,
        help=f"Part size in bytes. Default: {DEFAULT_CHUNK_BYTES}.",
    )
    parser.add_argument(
        "--title",
        default="Torch Trace Viewer",
        help="Browser title stored in manifest.json.",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="Viewer index.html template.",
    )
    return parser.parse_args()


def split_assignment(raw: str, kind: str) -> tuple[str, str]:
    if "=" not in raw:
        raise BuildError(f"Invalid {kind} {raw!r}; expected ID=VALUE")
    key, value = raw.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key or not value:
        raise BuildError(f"Invalid {kind} {raw!r}; ID and value must be non-empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key):
        raise BuildError(
            f"Invalid group ID {key!r}; use 1-64 letters, digits, dot, underscore, or hyphen"
        )
    return key, value


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output(raw_output: str) -> Path:
    output = Path(raw_output).expanduser().resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve()}
    if output in forbidden:
        raise BuildError(f"Refusing broad output directory: {output}")
    if is_within(output, SKILL_ROOT):
        raise BuildError(
            "Output must be outside the installed skill so the skill remains zero-data and portable"
        )
    if output.exists() and not output.is_dir():
        raise BuildError(f"Output exists and is not a directory: {output}")
    if output.exists():
        entries = list(output.iterdir())
        if entries and not (output / MARKER_NAME).is_file():
            raise BuildError(
                f"Refusing to replace non-generated directory: {output}. "
                "Choose an empty directory or one created by this script."
            )
    return output


def collect_sources(
    raw_groups: list[str], output: Path, patterns: tuple[str, ...]
) -> OrderedDict[str, list[Path]]:
    groups: OrderedDict[str, list[Path]] = OrderedDict()
    for raw in raw_groups:
        group, raw_path = split_assignment(raw, "--group")
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise BuildError(f"Trace source does not exist: {source}")
        if source == output or is_within(source, output):
            raise BuildError(f"Trace source cannot be inside the generated output: {source}")
        files: Iterable[Path]
        if source.is_file():
            files = [source]
        elif source.is_dir():
            found: set[Path] = set()
            for pattern in patterns:
                found.update(path for path in source.rglob(pattern) if path.is_file())
            files = sorted(found)
        else:
            raise BuildError(f"Trace source is neither a file nor directory: {source}")
        groups.setdefault(group, []).extend(files)

    for group, files in groups.items():
        unique = sorted(set(files), key=lambda path: str(path))
        by_name: dict[str, Path] = {}
        for path in unique:
            previous = by_name.get(path.name)
            if previous is not None and previous != path:
                raise BuildError(
                    f"Group {group!r} contains duplicate filename {path.name!r}: "
                    f"{previous} and {path}. Rename one file or use separate groups."
                )
            by_name[path.name] = path
        groups[group] = unique
    return groups


def infer_rank(filename: str) -> str:
    match = re.search(r"TP-(\d+)-PP-(\d+)", filename, flags=re.IGNORECASE)
    if match:
        return f"T{match.group(1)}P{match.group(2)}"
    match = re.search(r"TP-(\d+)-DP-(\d+)", filename, flags=re.IGNORECASE)
    if match:
        return f"TP{match.group(1)}"
    match = re.search(r"(?:^|[-_.])rank[-_.]?(\d+)(?:[-_.]|$)", filename, flags=re.IGNORECASE)
    if match:
        return f"rank{match.group(1)}"
    name = filename
    for suffix in (".trace.json.gz", ".trace.json", ".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or "trace"
    return Path(name).stem or "trace"


def infer_timestamp(source: Path) -> tuple[float, str]:
    """Prefer a Unix timestamp embedded in the filename, then use source mtime."""
    match = re.search(r"(?<!\d)(\d{10}(?:\.\d+)?)(?!\d)", source.name)
    if match:
        timestamp = float(match.group(1))
        if MIN_TRACE_EPOCH <= timestamp < MAX_TRACE_EPOCH:
            return timestamp, "filename"
    return source.stat().st_mtime, "mtime"


def validate_trace(path: Path) -> None:
    """Catch obvious corrupt or mislabeled JSON traces without parsing large files."""
    if path.stat().st_size <= 0:
        raise BuildError(f"Trace is empty: {path}")
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rb") as stream:
                stream.read(1)
        else:
            with path.open("rb") as stream:
                stream.read(1)
    except (OSError, EOFError) as exc:
        raise BuildError(f"Cannot read trace {path}: {exc}") from exc


def write_parts(source: Path, target_dir: Path, chunk_bytes: int) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    with source.open("rb") as stream:
        index = 0
        while True:
            payload = stream.read(chunk_bytes)
            if not payload:
                break
            part_name = f"{source.name}.part{index:03d}"
            (target_dir / part_name).write_bytes(payload)
            parts.append(part_name)
            index += 1
    return parts


def build_stage(
    stage: Path,
    template: Path,
    groups: OrderedDict[str, list[Path]],
    labels: dict[str, str],
    title: str,
    chunk_bytes: int,
) -> dict[str, object]:
    shutil.copy2(template, stage / "index.html")
    data_dir = stage / "data"
    data_dir.mkdir()

    manifest_groups: list[dict[str, object]] = []
    traces: list[dict[str, object]] = []
    for group, paths in groups.items():
        group_dir = data_dir / group
        for source in paths:
            validate_trace(source)
            part_names = write_parts(source, group_dir, chunk_bytes)
            timestamp_epoch, timestamp_source = infer_timestamp(source)
            traces.append(
                {
                    "group": group,
                    "rank": infer_rank(source.name),
                    "file": source.name,
                    "size": source.stat().st_size,
                    "timestamp_epoch": timestamp_epoch,
                    "timestamp_source": timestamp_source,
                    "encoding": "gzip" if source.name.endswith(".gz") else "identity",
                    "parts": [f"data/{group}/{name}" for name in part_names],
                }
            )
        manifest_groups.append(
            {
                "id": group,
                "label": labels.get(group, group.replace("-", " ").replace("_", " ")),
                "trace_count": len(paths),
            }
        )

    manifest: dict[str, object] = {
        "version": 1,
        "site_title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chunk_bytes": chunk_bytes,
        "groups": manifest_groups,
        "traces": traces,
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (stage / MARKER_NAME).write_text(
        json.dumps({"format": "northjetty-trace-site", "version": 1}) + "\n",
        encoding="utf-8",
    )
    return manifest


def install_stage(stage: Path, output: Path) -> None:
    backup: Path | None = None
    if output.exists():
        backup = output.with_name(f".{output.name}.previous-{os.getpid()}")
        if backup.exists():
            raise BuildError(f"Unexpected backup path already exists: {backup}")
        output.rename(backup)
    try:
        stage.rename(output)
    except Exception:
        if backup is not None and backup.exists() and not output.exists():
            backup.rename(output)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def main() -> int:
    args = parse_args()
    try:
        if args.chunk_bytes <= 0:
            raise BuildError("--chunk-bytes must be positive")
        if args.chunk_bytes > 9_000_000:
            raise BuildError(
                "--chunk-bytes must not exceed 9,000,000 for the current NorthJetty edge limit"
            )
        output = validate_output(args.output)
        template = Path(args.template).expanduser().resolve()
        if not template.is_file():
            raise BuildError(f"Viewer template not found: {template}")
        patterns = tuple(args.pattern) if args.pattern else DEFAULT_PATTERNS
        labels = dict(split_assignment(raw, "--group-label") for raw in args.group_label)
        groups = collect_sources(args.group, output, patterns)
        unknown_labels = sorted(set(labels) - set(groups))
        if unknown_labels:
            raise BuildError(
                "--group-label refers to groups not provided by --group: "
                + ", ".join(unknown_labels)
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=output.parent))
        try:
            manifest = build_stage(
                stage=stage,
                template=template,
                groups=groups,
                labels=labels,
                title=args.title.strip() or "Torch Trace Viewer",
                chunk_bytes=args.chunk_bytes,
            )
            install_stage(stage, output)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

        total_bytes = sum(int(trace["size"]) for trace in manifest["traces"])
        print(f"Site: {output}")
        print(f"Groups: {len(manifest['groups'])}")
        print(f"Traces: {len(manifest['traces'])}")
        print(f"Trace bytes: {total_bytes}")
        if not manifest["traces"]:
            print("Mode: empty viewer (local drag-and-drop remains available)")
        return 0
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
