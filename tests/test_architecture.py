from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path

from tests.helpers import ROOT

PACKAGE = "azure_openai_cache_benchmark"
PACKAGE_ROOT = ROOT / "src" / PACKAGE
LAYER_MAP = {
    PACKAGE: 0,
    f"{PACKAGE}.constants": 0,
    f"{PACKAGE}.errors": 0,
    f"{PACKAGE}.serialization": 0,
    f"{PACKAGE}.config": 0,
    f"{PACKAGE}.tokens": 0,
    f"{PACKAGE}.prompt_assets": 0,
    f"{PACKAGE}.schemas": 0,
    f"{PACKAGE}.specs": 0,
    f"{PACKAGE}.usage": 0,
    f"{PACKAGE}.prompts": 0,
    f"{PACKAGE}.pricing": 1,
    f"{PACKAGE}.pricing.models": 1,
    f"{PACKAGE}.pricing.costs": 1,
    f"{PACKAGE}.pricing.retail": 1,
    f"{PACKAGE}.suite": 1,
    f"{PACKAGE}.suite.builder": 1,
    f"{PACKAGE}.suite.qualification": 1,
    f"{PACKAGE}.suite.stability": 1,
    f"{PACKAGE}.suite.cache_behavior": 1,
    f"{PACKAGE}.suite.latency": 1,
    f"{PACKAGE}.suite.validation": 1,
    f"{PACKAGE}.suite.plan": 1,
    f"{PACKAGE}.runtime": 2,
    f"{PACKAGE}.runtime.budget": 2,
    f"{PACKAGE}.runtime.events": 2,
    f"{PACKAGE}.runtime.streaming": 2,
    f"{PACKAGE}.runtime.runner": 2,
    f"{PACKAGE}.runtime.execution": 2,
    f"{PACKAGE}.analysis": 2,
    f"{PACKAGE}.analysis.metrics": 2,
    f"{PACKAGE}.analysis.diagnostics": 2,
    f"{PACKAGE}.analysis.summary": 2,
    f"{PACKAGE}.output": 3,
    f"{PACKAGE}.output.artifacts": 3,
    f"{PACKAGE}.output.manifest": 3,
    f"{PACKAGE}.output.sanitizer": 3,
    f"{PACKAGE}.output.report_sections": 3,
    f"{PACKAGE}.output.report": 3,
    f"{PACKAGE}.commands": 4,
    f"{PACKAGE}.commands.context": 4,
    f"{PACKAGE}.commands.dry_run": 4,
    f"{PACKAGE}.commands.live": 4,
    f"{PACKAGE}.commands.reports": 4,
    f"{PACKAGE}.cli": 5,
    f"{PACKAGE}.__main__": 6,
}


def module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((PACKAGE, *parts)) if parts else PACKAGE


def internal_imports(module: str, path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                keep = len(parts) - (node.level - 1)
                base = parts[:keep]
                target = ".".join(
                    (*base, *((node.module or "").split(".")))
                ).rstrip(".")
            else:
                target = node.module
            if any(alias.name == "*" for alias in node.names):
                raise AssertionError(f"Wildcard import in {path}:{node.lineno}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    imports.add(alias.name)
        if target and (
            target == PACKAGE or target.startswith(PACKAGE + ".")
        ):
            imports.add(target)
    return imports


class ArchitectureTests(unittest.TestCase):
    def test_production_python_files_are_at_most_300_lines(self) -> None:
        paths = [ROOT / "benchmark.py", *PACKAGE_ROOT.rglob("*.py")]
        violations = {
            str(path.relative_to(ROOT)): len(
                path.read_text(encoding="utf-8").splitlines()
            )
            for path in paths
            if len(path.read_text(encoding="utf-8").splitlines()) > 300
        }
        self.assertEqual({}, violations)

    def test_explicit_layer_map_has_no_upward_imports_or_cycles(self) -> None:
        paths = list(PACKAGE_ROOT.rglob("*.py"))
        discovered = {module_name(path): path for path in paths}
        self.assertEqual(set(LAYER_MAP), set(discovered))
        graph: dict[str, set[str]] = {}
        for module, path in discovered.items():
            imports = internal_imports(module, path)
            unknown = imports - set(LAYER_MAP)
            self.assertEqual(set(), unknown, f"{module} has unmapped imports")
            graph[module] = imports
            for target in imports:
                self.assertLessEqual(
                    LAYER_MAP[target],
                    LAYER_MAP[module],
                    f"upward import: {module} -> {target}",
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module: str) -> None:
            if module in visiting:
                self.fail(f"import cycle reaches {module}")
            if module in visited:
                return
            visiting.add(module)
            for target in graph[module]:
                visit(target)
            visiting.remove(module)
            visited.add(module)

        for module in graph:
            visit(module)

    def test_argparse_namespace_is_confined_to_cli(self) -> None:
        offenders = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            if path.name == "cli.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "argparse.Namespace" in text or "from argparse import Namespace" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_package_metadata_and_legacy_removal(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(">=3.11", metadata["project"]["requires-python"])
        self.assertEqual(["dependencies"], metadata["project"]["dynamic"])
        self.assertIn("build>=1.2.2,<2.0.0", metadata["project"]["optional-dependencies"]["dev"])
        self.assertEqual(
            "azure_openai_cache_benchmark.cli:main",
            metadata["project"]["scripts"]["azure-openai-cache-benchmark"],
        )
        self.assertFalse((ROOT / "benchmark_core.py").exists())

    def test_removed_url_alias_never_appears(self) -> None:
        offenders = []
        suffixes = {".py", ".md", ".toml", ".txt", ".json", ".example"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(part in {".git", ".venv", "__pycache__", "runs"} for part in path.parts):
                continue
            if "OPENAI_" + "BASER_URL" in path.read_text(
                encoding="utf-8",
                errors="ignore",
            ):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)
