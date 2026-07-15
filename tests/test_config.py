from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from azure_openai_cache_benchmark.config import (
    DOTENV_ALLOWED_KEYS,
    load_allowed_env_file,
    normalize_base_url,
    redact_text,
    resolve_endpoint_env,
)
from azure_openai_cache_benchmark.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_url_normalization_and_endpoint_env(self) -> None:
        self.assertEqual(
            "https://example.openai.azure.com/openai/v1/",
            normalize_base_url("https://example.openai.azure.com"),
        )
        self.assertEqual(
            "http://127.0.0.1:8080/v1/",
            normalize_base_url("http://127.0.0.1:8080"),
        )
        resolved, source = resolve_endpoint_env(
            {"OPENAI_BASE_URL": "https://standard.openai.azure.com"}
        )
        self.assertEqual(
            "https://standard.openai.azure.com/openai/v1/",
            resolved,
        )
        self.assertEqual("OPENAI_BASE_URL", source)
        with self.assertRaisesRegex(ConfigurationError, "Set OPENAI_BASE_URL"):
            resolve_endpoint_env({"OPENAI_BASE_URL": "  "})

    def test_url_rejects_unsafe_shapes(self) -> None:
        invalid = (
            "http://example.openai.azure.com",
            "******localhost",
            "https://example.openai.azure.com?api-version=x",
            "https://example.openai.azure.com/#fragment",
            (
                "https://example.openai.azure.com/openai/deployments/"
                "gpt-5.4-mini"
            ),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    normalize_base_url(value)

    def test_redaction_removes_secret_url_and_hostname(self) -> None:
        secret = "unit-secret"
        url = "https://example.openai.azure.com/openai/v1/"
        redacted = redact_text(
            f"api_key={secret} endpoint={url} host=example.openai.azure.com",
            secrets=(secret,),
            base_url=url,
        )
        self.assertNotIn(secret, redacted)
        self.assertNotIn("example.openai.azure.com", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)
        self.assertIn("[REDACTED_ENDPOINT]", redacted)

    def test_dotenv_allowlist_and_process_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    (
                        "OPENAI_BASE_URL=https://dotenv.openai.azure.com",
                        "OPENAI_API_KEY=dotenv-secret",
                        "UNRELATED_SECRET=must-not-load",
                    )
                ),
                encoding="utf-8",
            )
            environ = {
                "OPENAI_BASE_URL": "https://process.openai.azure.com",
                "OPENAI_API_KEY": "process-secret",
            }
            info = load_allowed_env_file(path, environ)
        self.assertEqual(
            ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
            DOTENV_ALLOWED_KEYS,
        )
        self.assertEqual(
            "https://process.openai.azure.com",
            environ["OPENAI_BASE_URL"],
        )
        self.assertEqual("process-secret", environ["OPENAI_API_KEY"])
        self.assertNotIn("UNRELATED_SECRET", environ)
        self.assertEqual([], info["loaded_keys"])
        self.assertEqual(
            ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
            info["present_keys"],
        )
        serialized = json.dumps(info, sort_keys=True)
        self.assertNotIn("dotenv-secret", serialized)
        self.assertNotIn("process-secret", serialized)

    def test_dotenv_loads_allowed_values_when_process_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    (
                        "OPENAI_BASE_URL=https://dotenv.openai.azure.com",
                        "OPENAI_API_KEY=dotenv-secret",
                    )
                ),
                encoding="utf-8",
            )
            environ: dict[str, str] = {}
            info = load_allowed_env_file(path, environ)
        self.assertEqual(
            "https://dotenv.openai.azure.com",
            environ["OPENAI_BASE_URL"],
        )
        self.assertEqual("dotenv-secret", environ["OPENAI_API_KEY"])
        self.assertEqual(
            ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
            info["loaded_keys"],
        )
