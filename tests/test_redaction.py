from orchestrator.redaction import REDACTED, redact, redact_preview


def test_openai_key_scrubbed():
    out = redact("my key is sk-abcdef1234567890XYZ done")
    assert "sk-abcdef" not in out and REDACTED in out


def test_env_line_scrubbed():
    out = redact("API_KEY=supersecretvalue123")
    assert "supersecretvalue123" not in out and REDACTED in out


def test_bearer_token_scrubbed():
    out = redact("Authorization: Bearer abcdef0123456789xyz")
    assert "abcdef0123456789xyz" not in out


def test_jwt_scrubbed():
    jwt = "eyJhbGciOiJ.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"
    assert REDACTED in redact(jwt)


def test_ordinary_text_untouched():
    assert redact("just a normal sentence about files") == "just a normal sentence about files"


def test_nested_dict_walked():
    out = redact({"a": {"b": ["sk-deadbeef12345678"]}})
    assert out["a"]["b"][0] == REDACTED or REDACTED in out["a"]["b"][0]


def test_field_allowlist_drops_unlisted():
    out = redact({"keep": "hi", "drop": "secretvalue"}, field_allowlist={"keep"})
    assert out["keep"] == "hi"
    assert out["drop"] == REDACTED


def test_preview_truncates_and_scrubs():
    out = redact_preview("sk-abcdef1234567890 " + "x" * 5000, max_len=50)
    assert "truncated" in out
    assert "sk-abcdef" not in out
