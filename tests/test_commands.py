from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from azure_openai_cache_benchmark.cli import build_parser, main

from tests.helpers import ROOT


class CommandTests(unittest.TestCase):
    def test_parser_defaults_follow_supplied_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            dry = build_parser(base).parse_args(["dry-run"])
            sample = build_parser(base).parse_args(
                ["sample", "summary.json"]
            )
        self.assertEqual(base / "runs", dry.output_root)
        self.assertEqual(base / ".env", dry.env_file)
        self.assertEqual(base / "examples", sample.output_dir)

    def test_incomplete_price_override_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "dry-run",
                    "--run-id",
                    "bad-price",
                    "--output-root",
                    directory,
                    "--input-price",
                    "1",
                ]
            )
        self.assertEqual(2, result)

    def test_all_entrypoints_share_semantics_and_path_policies(self) -> None:
        console = Path(sys.executable).with_name(
            "azure-openai-cache-benchmark"
        )
        self.assertTrue(console.is_file(), console)
        root_run_id = f"root-{uuid.uuid4().hex}"
        root_run_dir = ROOT / "runs" / root_run_id
        manifests = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                foreign = Path(directory)
                commands = (
                    (
                        [
                            sys.executable,
                            str(ROOT / "benchmark.py"),
                            "dry-run",
                            "--skip-pricing",
                            "--run-id",
                            root_run_id,
                        ],
                        root_run_dir,
                    ),
                    (
                        [
                            sys.executable,
                            "-m",
                            "azure_openai_cache_benchmark",
                            "dry-run",
                            "--skip-pricing",
                            "--run-id",
                            "module-entrypoint",
                        ],
                        foreign / "runs" / "module-entrypoint",
                    ),
                    (
                        [
                            str(console),
                            "dry-run",
                            "--skip-pricing",
                            "--run-id",
                            "console-entrypoint",
                        ],
                        foreign / "runs" / "console-entrypoint",
                    ),
                )
                for command, run_dir in commands:
                    result = subprocess.run(
                        command,
                        cwd=foreign,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    manifest_path = run_dir / "manifest.json"
                    self.assertTrue(manifest_path.is_file(), manifest_path)
                    manifests.append(
                        json.loads(manifest_path.read_text(encoding="utf-8"))
                    )
                self.assertFalse(
                    (foreign / "runs" / root_run_id).exists()
                )
            semantics = [
                (
                    item["mode"],
                    item["validation"],
                    item["suite"]["allocation"],
                    item["prompt"]["path"],
                    item["prompt"]["sha256"],
                    item["pricing"],
                )
                for item in manifests
            ]
            self.assertEqual(semantics[0], semantics[1])
            self.assertEqual(semantics[1], semantics[2])
        finally:
            shutil.rmtree(root_run_dir, ignore_errors=True)
