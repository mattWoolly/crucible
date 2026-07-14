"""Suite-wide hermeticity: tests must never read the developer's real
crucible config (which may enable OTel and spin up real OTLP exporters)."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_crucible_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_CONFIG", str(tmp_path / "no-such-config.json"))
    monkeypatch.delenv("CRUCIBLE_OTEL", raising=False)
    monkeypatch.delenv("CRUCIBLE_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("CRUCIBLE_OTEL_ENV", raising=False)
    monkeypatch.delenv("CRUCIBLE_OTEL_TRACES", raising=False)
