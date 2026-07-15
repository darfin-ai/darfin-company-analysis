from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import errors

from dart_pipeline import llm
from dart_pipeline.llm_runtime import generate_content
from dart_pipeline.risk_narrative import generate_narratives


class _Models:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class _Client:
    def __init__(self, responses):
        self.models = _Models(responses)


def _response(parsed):
    return SimpleNamespace(
        parsed=parsed,
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )


class LlmControlTests(unittest.TestCase):
    @patch("dart_pipeline.llm_runtime.time.sleep")
    @patch("dart_pipeline.llm_runtime.random.random", return_value=0)
    def test_transient_error_is_retried_once(self, _random, _sleep):
        client = _Client([errors.ServerError(503, {}), _response(None)])

        result = generate_content(
            client, operation="test", item_count=1, model="fake", contents="safe", config=None
        )

        self.assertIsNotNone(result)
        self.assertEqual(client.models.calls, 2)

    @patch("dart_pipeline.llm._extract_findings_batch")
    def test_chunked_findings_are_globally_deduped_and_capped(self, batch):
        batch.side_effect = [
            [llm.FindingCandidate([i], "low", "governance", f"low {i}") for i in range(10)],
            [llm.FindingCandidate([10], "high", "governance", "high")],
        ]
        evidence = [llm.EvidenceItem(i, "note", "section", "excerpt", "ref") for i in range(11)]

        result = llm.extract_findings(_Client([]), evidence)

        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].evidence_ids, [10])

    def test_missing_required_narrative_raises_before_persist(self):
        parsed = SimpleNamespace(
            results=[SimpleNamespace(index=0, narrative="유동성 설명", watch_next=None)]
        )
        states = [
            {"category": "liquidity", "state": "normal", "consecutive_qtrs": 1, "quant_signals": {}},
            {"category": "leverage", "state": "normal", "consecutive_qtrs": 1, "quant_signals": {}},
        ]

        with self.assertRaisesRegex(RuntimeError, "leverage"):
            generate_narratives(_Client([_response(parsed)]), states, [])

    @patch("dart_pipeline.llm._extract_risks_batch")
    def test_chunked_risks_are_globally_capped(self, batch):
        batch.side_effect = [
            [llm.RiskCandidate([i], "low", f"risk {i}", "low") for i in range(10)],
            [llm.RiskCandidate([10], "high", "important", "high")],
        ]
        evidence = [llm.EvidenceItem(i, "note", "section", "excerpt", "ref") for i in range(11)]

        result = llm.extract_risks(_Client([]), evidence)

        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].evidence_ids, [10])


if __name__ == "__main__":
    unittest.main()
