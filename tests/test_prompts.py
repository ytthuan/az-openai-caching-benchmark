from __future__ import annotations

import unittest

from azure_openai_cache_benchmark.constants import (
    DEFAULT_PROMPT_LOGICAL_PATH,
    DYNAMIC_START,
)
from azure_openai_cache_benchmark.prompt_assets import (
    load_prompt_asset,
    render_system_prompt,
    split_prompt_boundary,
    validate_prompt_asset,
)


class PromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = load_prompt_asset()

    def test_packaged_prompt_metadata_is_stable(self) -> None:
        self.assertEqual(DEFAULT_PROMPT_LOGICAL_PATH, self.asset.path)
        self.assertEqual(
            "f99363a4d9e2bb7412f6b4c0747622b22331d36543331f8fddae352805183df4",
            self.asset.sha256,
        )
        self.assertEqual([], validate_prompt_asset(self.asset))
        self.assertEqual(1, self.asset.template.count(DYNAMIC_START))
        self.assertGreaterEqual(self.asset.word_count, 4_000)
        self.assertLessEqual(self.asset.word_count, 5_000)

    def test_rendered_prompt_resolves_dynamic_tail(self) -> None:
        rendered = render_system_prompt(
            self.asset,
            namespace="render-unit",
            case_id="render-unit",
            timestamp_utc="2026-07-14T10:00:00Z",
        )
        self.assertIn(DYNAMIC_START, rendered)
        self.assertIn("2026-07-14T10:00:00Z", rendered)
        _, dynamic_tail = split_prompt_boundary(rendered)
        self.assertNotRegex(dynamic_tail, r"\{\{[A-Z0-9_]+\}\}")
