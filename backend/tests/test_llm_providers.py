"""Every LLM vendor adapter drives the same two routes. Fake SDK clients stand in for the network."""

import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from src import config, llm
from src.llm_providers import Anthropic, Gemini, OpenAI


class Recorder:
    """An async create() that replays canned responses and records each request."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def __call__(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


# Each fake builds a vendor-shaped response holding the given (name, args) tool calls.

def gemini(responses):
    rec = Recorder([NS(function_calls=[NS(id=None, name=n, args=a) for n, a in calls],
                       candidates=[NS(content=f"model turn {i}")], text="")
                    for i, calls in enumerate(responses)])
    return Gemini(client=NS(aio=NS(models=NS(generate_content=rec))), model="g"), rec


def openai_(responses):
    import json
    rec = Recorder([NS(choices=[NS(message=NS(content=None, tool_calls=[
        NS(id=f"call_{i}_{j}", function=NS(name=n, arguments=json.dumps(a))) for j, (n, a) in enumerate(calls)]))])
        for i, calls in enumerate(responses)])
    return OpenAI(client=NS(chat=NS(completions=NS(create=rec))), model="o"), rec


def anthropic_(responses):
    rec = Recorder([NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id=f"tu_{i}_{j}", name=n, input=a) for j, (n, a) in enumerate(calls)])
        for i, calls in enumerate(responses)])
    return Anthropic(client=NS(beta=NS(messages=NS(create=rec))), model="claude-opus-5-5"), rec


FAKES = {"gemini": gemini, "openai": openai_, "anthropic": anthropic_}


@pytest.fixture
def use(monkeypatch):
    def _use(vendor, responses):
        provider, rec = FAKES[vendor](responses)
        monkeypatch.setattr(llm, "_provider", provider)
        return rec
    return _use


@pytest.mark.parametrize("vendor", FAKES)
def test_team_utterance_is_classified_with_proposals(use, vendor):
    use(vendor, [[("record_message", {"classification": "team_message", "summary": "Ben: 4 mics to stage left."}),
                  ("create_request", {"description": "Bring 4 mics to stage left"})]])
    result = asyncio.run(llm.interpret_team("{}"))
    assert result.classification == "team_message"
    assert result.summary == "Ben: 4 mics to stage left."
    assert result.proposals == [{"type": "create_request", "description": "Bring 4 mics to stage left"}]


@pytest.mark.parametrize("vendor", FAKES)
def test_missing_classification_fails(use, vendor):
    use(vendor, [[("create_request", {"description": "x"})]])
    with pytest.raises(RuntimeError):
        asyncio.run(llm.interpret_team("{}"))


@pytest.mark.parametrize("vendor", FAKES)
def test_question_runs_one_lookup_round_then_answers(use, vendor):
    rec = use(vendor, [[("get_status", {})],
                       [("answer", {"text": "Ben owes four mics.", "source_message_ids": ["msg_1"]})]])
    seen = []

    async def run_tool(name, args):
        seen.append(name)
        return {"open": 1}

    ans = asyncio.run(llm.answer_question("{}", run_tool))
    assert (ans.text, ans.source_message_ids, seen) == ("Ben owes four mics.", ["msg_1"], ["get_status"])
    assert len(rec.requests) == 2
    second = rec.requests[1]
    # The tool result reaches the model on the second call, in that vendor's format.
    if vendor == "openai":
        assert second["messages"][-1]["role"] == "tool"
        assert second["tool_choice"] == {"type": "function", "function": {"name": "answer"}}
    elif vendor == "anthropic":
        blocks = second["messages"][-1]["content"]
        assert blocks[0]["type"] == "tool_result" and blocks[0]["tool_use_id"] == "tu_0_0"
        assert blocks[-1] == {"type": "text", "text": "Now call the answer tool."}
        assert second["tool_choice"] == {"type": "auto"}
    else:
        cfg = second["config"].tool_config.function_calling_config
        assert cfg.allowed_function_names == ["answer"]
        assert second["contents"][-1].parts[0].function_response.name == "get_status"


def test_plain_text_reply_counts_as_the_answer(monkeypatch):
    rec = Recorder([NS(stop_reason="end_turn", content=[NS(type="text", text="Nothing is outstanding.")])])
    monkeypatch.setattr(llm, "_provider", Anthropic(client=NS(beta=NS(messages=NS(create=rec))), model="m"))
    ans = asyncio.run(llm.answer_question("{}", None))
    assert ans.text == "Nothing is outstanding."


def test_anthropic_refusal_fails_the_call(monkeypatch):
    rec = Recorder([NS(stop_reason="refusal", content=[])])
    monkeypatch.setattr(llm, "_provider", Anthropic(client=NS(beta=NS(messages=NS(create=rec))), model="m"))
    with pytest.raises(RuntimeError):
        asyncio.run(llm.interpret_team("{}"))


def test_anthropic_fallbacks_only_on_models_that_support_them(monkeypatch):
    for model, expect in [("claude-opus-5-5", True), ("claude-haiku-5-5", False)]:
        rec = Recorder([NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t", name="record_message",
                                                                input={"classification": "team_message", "summary": ""})])])
        monkeypatch.setattr(llm, "_provider", Anthropic(client=NS(beta=NS(messages=NS(create=rec))), model=model))
        asyncio.run(llm.interpret_team("{}"))
        assert ("fallbacks" in rec.requests[0]) is expect


def test_provider_selection():
    keys = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    assert config.pick_llm_provider("", keys) == "openai"
    assert config.pick_llm_provider("", {"ANTHROPIC_API_KEY"}) == "anthropic"
    assert config.pick_llm_provider("", set()) == "gemini"
    assert config.pick_llm_provider("anthropic", keys) == "anthropic"
    with pytest.raises(ValueError):
        config.pick_llm_provider("llama", keys)
