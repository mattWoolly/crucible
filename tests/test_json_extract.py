from orchestrator.constants import DEGRADED_CONFIDENCE
from orchestrator.json_extract import extract_json, extract_worker_result


def test_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_code_fenced():
    text = '```json\n{"summary": "hi", "confidence": 0.5}\n```'
    assert extract_json(text) == {"summary": "hi", "confidence": 0.5}


def test_prose_then_real_object_prefers_largest():
    # An inline example object appears first; the real (larger) answer is last.
    text = (
        'Here is an example like {"x": 1}. Now my answer:\n'
        '{"summary": "the real answer", "artifacts": {"k": "v"}, "confidence": 0.9}'
    )
    obj = extract_json(text)
    assert obj["summary"] == "the real answer"
    assert obj["artifacts"] == {"k": "v"}


def test_trailing_text_after_object():
    text = '{"summary": "ok", "confidence": 0.7}\nThanks!'
    assert extract_json(text)["summary"] == "ok"


def test_invalid_escape_sanitized():
    # \d is not a valid JSON escape; sanitization should rescue it.
    text = r'{"summary": "path C:\dir", "confidence": 0.5}'
    obj = extract_json(text)
    assert obj is not None
    assert "summary" in obj


def test_total_garbage_returns_none():
    assert extract_json("not json at all, no braces") is None


def test_extract_worker_result_degraded_on_garbage():
    r = extract_worker_result("totally not json " * 50)
    assert r.confidence == DEGRADED_CONFIDENCE
    assert r.uncertainties and r.uncertainties[0].startswith("parse_failed:")
    assert len(r.summary) <= 500


def test_extract_worker_result_happy():
    r = extract_worker_result('{"summary": "done", "confidence": 0.8}')
    assert r.summary == "done"
    assert r.confidence == 0.8


def test_extract_worker_result_schema_mismatch_degrades():
    # Valid JSON object but missing required 'summary' -> degraded, not raise.
    r = extract_worker_result('{"confidence": 0.8}')
    assert r.confidence == DEGRADED_CONFIDENCE
