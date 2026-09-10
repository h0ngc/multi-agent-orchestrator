from dataclasses import FrozenInstanceError, fields
import json
import math
from pathlib import Path

import pytest

from mao_core.errors import MaoError
from mao_core.review import (
    CriticJob,
    CriticOutcome,
    Decision,
    DeduplicatedFinding,
    Finding,
    ReviewResult,
    deduplicate_findings,
    finding_fingerprint,
    parse_review,
)


FIXTURES = Path(__file__).parent / "fixtures"


def finding(**overrides) -> Finding:
    values = {
        "severity": "major",
        "category": "correctness",
        "file": "src/a.py",
        "line": 10,
        "evidence": "Null dereference",
        "reason": "The value can be null.",
        "suggested_fix": "Guard the value.",
        "confidence": 0.9,
        "needs_context": (),
    }
    values.update(overrides)
    return Finding(**values)


def review_value(**overrides):
    value = json.loads((FIXTURES / "review-valid.json").read_text())
    value.update(overrides)
    return value


def assert_invalid(value):
    with pytest.raises(MaoError) as caught:
        parse_review(value)
    assert caught.value.code == "INVALID_RESULT"


def test_review_types_have_exact_frozen_fields():
    assert [item.name for item in fields(Finding)] == [
        "severity", "category", "file", "line", "evidence", "reason",
        "suggested_fix", "confidence", "needs_context",
    ]
    assert [item.name for item in fields(ReviewResult)] == [
        "summary", "findings", "usage", "review_complete",
    ]
    assert [item.name for item in fields(Decision)] == [
        "fingerprint", "verdict", "rationale",
    ]
    assert [item.name for item in fields(CriticJob)] == [
        "critic", "round_number", "provider", "transport", "request",
    ]
    assert [item.name for item in fields(CriticOutcome)] == [
        "critic", "call_number", "status", "review", "error",
    ]
    with pytest.raises(FrozenInstanceError):
        finding().severity = "minor"


def test_valid_review_fixture_parses():
    parsed = parse_review(review_value())
    assert parsed == ReviewResult("No issues found.", (), {}, True)


def test_invalid_review_missing_evidence_is_rejected():
    assert_invalid(json.loads((FIXTURES / "review-invalid.json").read_text()))


@pytest.mark.parametrize(
    "value",
    [
        [],
        review_value(extra=True),
        {"summary": "x"},
        review_value(review_complete=1),
        review_value(review_complete=False),
        review_value(usage=[]),
        review_value(usage={"tokens": math.nan}),
    ],
)
def test_review_rejects_invalid_top_level_or_usage(value):
    assert_invalid(value)


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "high"},
        {"category": "style"},
        {"line": 0},
        {"line": True},
        {"confidence": True},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": math.inf},
        {"file": "../secret"},
        {"file": "/absolute.py"},
        {"file": "C:\\secret.py"},
        {"file": "src/a.py\nignored"},
        {"needs_context": ["../secret"]},
        {"needs_context": ["../secret?"]},
        {"needs_context": ["Which\tbehavior?"]},
        {"needs_context": ["question\nsecond line?"]},
    ],
)
def test_review_rejects_invalid_finding_values(change):
    raw = {
        "severity": "major",
        "category": "correctness",
        "file": "src/a.py",
        "line": 2,
        "evidence": "wrong",
        "reason": "wrong",
        "suggested_fix": "fix",
        "confidence": 0.9,
        "needs_context": [],
    }
    raw.update(change)
    assert_invalid(review_value(findings=[raw]))


def test_review_accepts_safe_context_path_and_concise_question():
    raw = {
        "severity": "minor",
        "category": "requirements",
        "file": "src/a.py",
        "line": None,
        "evidence": "Need context",
        "reason": "Cannot prove behavior",
        "suggested_fix": "Supply context",
        "confidence": 0.5,
        "needs_context": ["src/config.py", "Which behavior is intended?"],
    }
    parsed = parse_review(review_value(findings=[raw]))
    assert parsed.findings[0].needs_context == (
        "src/config.py", "Which behavior is intended?",
    )


def test_equivalent_findings_share_fingerprint():
    left = finding(file="src/a.py", line=10, evidence="null   dereference")
    right = finding(file="./src/a.py", line=11, evidence=" Null Dereference ")
    assert finding_fingerprint(left) == finding_fingerprint(right)


def test_different_finding_region_or_category_has_different_fingerprint():
    base = finding()
    assert finding_fingerprint(base) != finding_fingerprint(
        finding(line=15)
    )
    assert finding_fingerprint(base) != finding_fingerprint(
        finding(category="security")
    )
    assert finding_fingerprint(base) != finding_fingerprint(
        finding(file="src/A.py")
    )


def test_deduplication_preserves_providers_and_highest_severity():
    duplicate_minor = finding(severity="minor", line=11, evidence="NULL dereference")
    unique = finding(file="src/b.py", evidence="race")
    result = deduplicate_findings(
        (("claude", finding()), ("agy", duplicate_minor), ("agy", unique))
    )
    assert all(isinstance(item, DeduplicatedFinding) for item in result)
    assert len(result) == 2
    duplicate = next(item for item in result if item.finding.file == "src/a.py")
    assert duplicate.providers == ("claude", "agy")
    assert duplicate.finding.severity == "major"


def test_bundled_schema_is_strict_and_versioned():
    schema_path = (
        Path(__file__).parents[1]
        / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/review.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("/0.1.0/review.schema.json")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["findings"]["items"]["additionalProperties"] is False
    assert "oneOf" in schema["properties"]["findings"]["items"]["properties"]["line"]
    assert schema["properties"]["review_complete"] == {"const": True}
