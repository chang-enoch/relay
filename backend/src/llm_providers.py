"""LLM provider adapters: Gemini, OpenAI (or any OpenAI-compatible endpoint) and Anthropic.

Each adapter turns the shared tool specs into one vendor's function-calling API and
normalizes replies to ToolCall lists, so llm.py never sees a vendor type. A provider
start()s a Chat; a chat step()s (one model call) and takes tool results back.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai
from google import genai
from google.genai import types

from . import config

Spec = list[tuple[str, str, dict]]  # (name, description, JSON schema)
TIMEOUT_S = 30  # bounded: a hung call would otherwise hold the room lock
RETRIES = 1


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""  # plain-text reply, only when the model called no tools


def _args(raw: Any) -> dict:
    """Tool arguments arrive as a dict or a JSON string, depending on the vendor."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


# --- Gemini ------------------------------------------------------------------

class Gemini:
    name = "gemini"

    def __init__(self, client: Any = None, model: str | None = None):
        self._client = client
        self.model = model or config.GEMINI_MODEL

    def client(self) -> genai.Client:
        if self._client is None:
            if not config.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(
                timeout=TIMEOUT_S * 1000, retry_options=types.HttpRetryOptions(attempts=RETRIES + 1)))
        return self._client

    def start(self, system: str, user: str) -> "_GeminiChat":
        return _GeminiChat(self, system, user)


class _GeminiChat:
    def __init__(self, provider: Gemini, system: str, user: str):
        self.p, self.system = provider, system
        self.contents: list = [types.Content(role="user", parts=[types.Part(text=user)])]

    async def step(self, spec: Spec, only: str | None = None) -> Turn:
        cfg = types.GenerateContentConfig(
            system_instruction=self.system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=n, description=d, parameters_json_schema=p) for n, d, p in spec])],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="ANY", allowed_function_names=[only] if only else None)),
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = await self.p.client().aio.models.generate_content(
            model=self.p.model, contents=self.contents, config=cfg)
        fcs = resp.function_calls or []
        if not fcs:
            return Turn(text=getattr(resp, "text", None) or "")
        self.contents.append(resp.candidates[0].content)
        return Turn([ToolCall(c.id or c.name, c.name, dict(c.args or {})) for c in fcs])

    def add_results(self, calls: list[ToolCall], results: list[Any]) -> None:
        self.contents.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name=c.name, response={"result": r})
            for c, r in zip(calls, results)]))


# --- OpenAI ------------------------------------------------------------------

class OpenAI:
    """OpenAI Chat Completions. OPENAI_BASE_URL points it at any compatible endpoint
    (Azure, Groq, Mistral, DeepSeek, xAI, OpenRouter, a local Ollama...)."""
    name = "openai"

    def __init__(self, client: Any = None, model: str | None = None):
        self._client = client
        self.model = model or config.OPENAI_MODEL

    def client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            if not config.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not set")
            self._client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY,
                                              base_url=config.OPENAI_BASE_URL or None,
                                              timeout=TIMEOUT_S, max_retries=RETRIES)
        return self._client

    def start(self, system: str, user: str) -> "_OpenAIChat":
        return _OpenAIChat(self, system, user)


class _OpenAIChat:
    def __init__(self, provider: OpenAI, system: str, user: str):
        self.p = provider
        self.messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def step(self, spec: Spec, only: str | None = None) -> Turn:
        resp = await self.p.client().chat.completions.create(
            model=self.p.model,
            messages=self.messages,
            tools=[{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
                   for n, d, p in spec],
            tool_choice={"type": "function", "function": {"name": only}} if only else "required",
        )
        msg = resp.choices[0].message
        raw = msg.tool_calls or []
        if not raw:
            return Turn(text=msg.content or "")
        self.messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in raw]})
        return Turn([ToolCall(tc.id, tc.function.name, _args(tc.function.arguments)) for tc in raw])

    def add_results(self, calls: list[ToolCall], results: list[Any]) -> None:
        self.messages += [{"role": "tool", "tool_call_id": c.id, "content": _dump(r)}
                          for c, r in zip(calls, results)]


# --- Anthropic ---------------------------------------------------------------

# Models that accept server-side refusal fallbacks (rerun on another model if declined).
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")


class Anthropic:
    """Anthropic Messages API. Current Claude models reject forced tool choice, so tools
    are offered with tool_choice auto and the system prompts name the tool to call."""
    name = "anthropic"

    def __init__(self, client: Any = None, model: str | None = None):
        self._client = client
        self.model = model or config.ANTHROPIC_MODEL

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not config.ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY,
                                                    timeout=TIMEOUT_S, max_retries=RETRIES)
        return self._client

    def start(self, system: str, user: str) -> "_AnthropicChat":
        return _AnthropicChat(self, system, user)


class _AnthropicChat:
    def __init__(self, provider: Anthropic, system: str, user: str):
        self.p, self.system = provider, system
        self.messages: list[dict] = [{"role": "user", "content": user}]

    async def step(self, spec: Spec, only: str | None = None) -> Turn:
        if only:  # no forced tool choice: ask for it in the turn instead
            last = self.messages[-1]
            if last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append({"type": "text", "text": f"Now call the {only} tool."})
        kwargs: dict[str, Any] = dict(
            model=self.p.model, max_tokens=16000, system=self.system, messages=self.messages,
            tools=[{"name": n, "description": d, "input_schema": p} for n, d, p in spec],
            tool_choice={"type": "auto"},
        )
        if config.ANTHROPIC_EFFORT:
            kwargs["output_config"] = {"effort": config.ANTHROPIC_EFFORT}
        if self.p.model.startswith(_FALLBACK_MODELS):
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        resp = await self.p.client().beta.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        calls = [ToolCall(b.id, b.name, _args(b.input)) for b in resp.content if b.type == "tool_use"]
        if not calls:
            return Turn(text="".join(b.text for b in resp.content if b.type == "text"))
        # Pass the full content back (thinking blocks included) as the API expects.
        self.messages.append({"role": "assistant", "content": resp.content})
        return Turn(calls)

    def add_results(self, calls: list[ToolCall], results: list[Any]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": _dump(r)} for c, r in zip(calls, results)]})


PROVIDERS = {p.name: p for p in (Gemini, OpenAI, Anthropic)}


def create(name: str):
    return PROVIDERS[name]()
