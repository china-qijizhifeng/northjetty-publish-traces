from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADD_SCRIPT = REPO_ROOT / "scripts" / "add_traces.py"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_trace_site.py"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_trace_site.py"


def run_script(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class TraceStoreTests(unittest.TestCase):
    def test_empty_trace_root_builds_valid_zero_data_site(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "traces"
            output = root / "site"
            trace_root.mkdir()

            build = run_script(
                BUILD_SCRIPT,
                "--output",
                str(output),
                "--trace-root",
                str(trace_root),
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            validate = run_script(VALIDATE_SCRIPT, str(output))
            self.assertEqual(validate.returncode, 0, validate.stderr)
            self.assertIn("Traces: 0", validate.stdout)

    def test_add_and_build_uses_date_scene_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rank-0.trace.json"
            source.write_text('{"traceEvents": []}\n', encoding="utf-8")
            timestamp = datetime(2026, 8, 11, 8, 9, tzinfo=timezone.utc).timestamp()
            os.utime(source, (timestamp, timestamp))
            trace_root = root / "traces"

            add = run_script(
                ADD_SCRIPT,
                "--trace-root",
                str(trace_root),
                "--scene",
                "prefill",
                str(source),
            )
            self.assertEqual(add.returncode, 0, add.stderr)
            archived = trace_root / "2026-08-11" / "prefill" / source.name
            self.assertTrue(archived.is_file())

            output = root / "site"
            build = run_script(
                BUILD_SCRIPT,
                "--output",
                str(output),
                "--trace-root",
                str(trace_root),
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["storage_layout"], "date/scene")
            self.assertEqual(manifest["groups"][0]["id"], "prefill")
            trace = manifest["traces"][0]
            self.assertEqual(trace["storage_date"], "2026-08-11")
            self.assertTrue(trace["parts"][0].startswith("data/2026-08-11/prefill/"))
            validate = run_script(VALIDATE_SCRIPT, str(output))
            self.assertEqual(validate.returncode, 0, validate.stderr)

    def test_compact_filename_timestamp_selects_storage_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "capture-20260810T134700Z-rank-1.trace.json.gz"
            with gzip.open(source, "wb") as stream:
                stream.write(b'{"traceEvents": []}\n')
            trace_root = root / "traces"
            add = run_script(
                ADD_SCRIPT,
                "--trace-root",
                str(trace_root),
                "--scene",
                "decode",
                str(source),
            )
            self.assertEqual(add.returncode, 0, add.stderr)
            self.assertTrue(
                (trace_root / "2026-08-10" / "decode" / source.name).is_file()
            )

    def test_trace_outside_date_scene_layout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "traces"
            trace_root.mkdir()
            (trace_root / "misplaced.trace.json").write_text(
                '{"traceEvents": []}\n', encoding="utf-8"
            )
            build = run_script(
                BUILD_SCRIPT,
                "--output",
                str(root / "site"),
                "--trace-root",
                str(trace_root),
            )
            self.assertEqual(build.returncode, 2)
            self.assertIn("outside YYYY-MM-DD/SCENE layout", build.stderr)


if __name__ == "__main__":
    unittest.main()
