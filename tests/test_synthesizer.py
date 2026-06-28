import pytest

from orchestrator.config import Config
from orchestrator.constants import DEGRADED_CONFIDENCE
from orchestrator.llm import FakeLLMClient, llm_response
from orchestrator.models import Subtask, WorkerResult
from orchestrator.synthesizer import run_synthesis


def _critic_json(score, approved):
    return llm_response(f'{{"score": {score}, "approved": {str(approved).lower()}, "issues": [], "suggestions": []}}')


SYNTH_SUB = Subtask(id="s1", role="synthesizer", task="merge all outputs", depends_on=["r1", "c1"])
APPROVED = {
    "r1": WorkerResult(summary="research done", artifacts={"path": "a.py"}, confidence=0.9),
    "c1": WorkerResult(summary="code changed and tested", confidence=0.9),
}


async def test_synthesize_merges_and_passes_final_critic():
    fake = FakeLLMClient({
        "synthesizer": [llm_response('{"summary": "merged final answer", "confidence": 0.9}')],
        "critic": [_critic_json(9, True)],
    })
    config = Config(workspace="/tmp")
    out, score = await run_synthesis(fake, SYNTH_SUB, APPROVED, "the task", config)
    assert out.result.summary == "merged final answer"
    assert score.approved is True


async def test_final_critic_rejects_then_resynthesizes():
    fake = FakeLLMClient({
        "synthesizer": [
            llm_response('{"summary": "weak merge", "confidence": 0.5}'),
            llm_response('{"summary": "strong merge", "confidence": 0.9}'),
        ],
        "critic": [_critic_json(4, False), _critic_json(9, True)],
    })
    config = Config(workspace="/tmp", max_iterations_per_subtask=1)
    out, score = await run_synthesis(fake, SYNTH_SUB, APPROVED, "the task", config)
    assert out.result.summary == "strong merge"  # the re-synthesized, accepted answer
    assert score.approved is True


async def test_synth_sees_degraded_input_summary():
    degraded = {"r1": WorkerResult.degraded("parse fail", raw="garbage")}
    fake = FakeLLMClient({
        "synthesizer": [llm_response('{"summary": "merged despite weak input", "confidence": 0.5}')],
        "critic": [_critic_json(9, True)],
    })
    config = Config(workspace="/tmp")
    sub = Subtask(id="s1", role="synthesizer", task="merge", depends_on=["r1"])
    out, score = await run_synthesis(fake, sub, degraded, "task", config)
    # The synthesizer's user message must carry the low-confidence flag.
    synth_call = next(c for c in fake.calls if c["role"] == "synthesizer")
    user_msg = next(m for m in synth_call["messages"] if m["role"] == "user")
    assert "LOW CONFIDENCE" in user_msg["content"]
