"""Offline tests for the dual-provider fallback wiring (ADR-004).

Two layers, and they fail for different reasons on purpose:

1. **Our wiring** (`build_dynamic_model`): the fallback fires when the primary
   raises, and is *not* wired when the fallback has no credentials or is the
   primary itself. Fake chat models stand in for the providers, so nothing here
   touches Ollama, Gemini or the network.
2. **The library contract this design rests on.** ADR-004 hands
   ``create_react_agent`` a dynamic-model callable because a
   ``RunnableWithFallbacks`` passed as ``model`` used to be rejected outright.
   That is a claim about an installed third-party library, and it has now
   drifted twice — see the ADR's two amendments. What holds today is measured
   here: the callable is passed through untouched, while the direct pass works
   only when the model's ``bind_tools`` is annotated as returning a
   ``Runnable``. The callable does not depend on that annotation; that is the
   reason to keep it.

This second layer is why ``langgraph`` carries an upper bound. A red here means
"re-read ADR-004, the library moved", not "production is broken".
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableWithFallbacks
from langchain_core.tools import StructuredTool

from src.agent.graph import build_dynamic_model
from src.core.config import Settings

# --- fixtures ---------------------------------------------------------------


def _ledger(sql: str) -> str:
    return "rows"


TOOLS = [StructuredTool.from_function(_ledger, name="query_ledger", description="run SQL")]

PRIMARY_DOWN = "primary is down"
FROM_FALLBACK = "from the fallback"


class FakeModel(BaseChatModel):
    """Answers with `reply`, or raises `RuntimeError(PRIMARY_DOWN)` if `broken`."""

    reply: str = "ok"
    broken: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeModel:
        # Returning `self` keeps object identity, so a test can assert *which*
        # model came back — the discriminating assertion for "no fallback wired".
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.broken:
            raise RuntimeError(PRIMARY_DOWN)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _providers(monkeypatch, **by_provider: BaseChatModel) -> list[str]:
    """Patch `build_chat_model` to hand out fakes; return the call log.

    `has_credentials` is deliberately **not** patched — it is under test.
    """
    built: list[str] = []

    def _fake_build(provider: str, settings: Settings) -> BaseChatModel:
        built.append(provider)
        return by_provider[provider]

    monkeypatch.setattr("src.agent.graph.build_chat_model", _fake_build)
    return built


def _settings(**kwargs: Any) -> Settings:
    # Explicit values beat the ambient .env / environment (init args win in
    # pydantic-settings), so a developer's real GEMINI_API_KEY cannot turn the
    # "no credentials" case green.
    return Settings(**kwargs)


# --- the fallback fires -----------------------------------------------------


def test_fallback_fires_when_the_primary_raises(monkeypatch):
    """The point of ADR-004: a dead primary is answered by the backup."""
    built = _providers(
        monkeypatch,
        ollama=FakeModel(broken=True),
        gemini=FakeModel(reply=FROM_FALLBACK),
    )
    settings = _settings(
        primary_provider="ollama", fallback_provider="gemini", gemini_api_key="key"
    )

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert built == ["ollama", "gemini"]
    assert isinstance(model, RunnableWithFallbacks)
    assert model.invoke([("user", "hi")]).content == FROM_FALLBACK


def test_the_primary_answers_when_it_is_healthy(monkeypatch):
    """The fallback is a backup, not a preference: a healthy primary wins."""
    _providers(
        monkeypatch,
        ollama=FakeModel(reply="from the primary"),
        gemini=FakeModel(reply=FROM_FALLBACK),
    )
    settings = _settings(
        primary_provider="ollama", fallback_provider="gemini", gemini_api_key="key"
    )

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert model.invoke([("user", "hi")]).content == "from the primary"


def test_the_order_is_config_driven(monkeypatch):
    """`PRIMARY_PROVIDER=gemini` inverts dev↔deploy with no code change."""
    built = _providers(
        monkeypatch,
        gemini=FakeModel(broken=True),
        ollama=FakeModel(reply=FROM_FALLBACK),
    )
    settings = _settings(
        primary_provider="gemini", fallback_provider="ollama", gemini_api_key="key"
    )

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert built == ["gemini", "ollama"]
    assert model.invoke([("user", "hi")]).content == FROM_FALLBACK


def test_the_tools_reach_both_providers(monkeypatch):
    """Each provider is bound to the tools separately — LangGraph binds none."""
    seen: list[Any] = []

    class Recorder(FakeModel):
        def bind_tools(self, tools, **kwargs):
            seen.append(tools)
            return self

    _providers(monkeypatch, ollama=Recorder(broken=True), gemini=Recorder(reply=FROM_FALLBACK))
    settings = _settings(
        primary_provider="ollama", fallback_provider="gemini", gemini_api_key="key"
    )

    build_dynamic_model(settings, TOOLS)

    assert seen == [TOOLS, TOOLS]


# --- the fallback is NOT wired ----------------------------------------------
#
# These are the cases the relaxing mutations attack. Asserting only "the call
# raises" would keep them green: with `has_credentials` dropped the fake backup
# answers happily, and with the `fb != primary` check dropped the primary simply
# falls back to *itself* and raises exactly the same error. So each test asserts
# the *reason* — which providers were constructed, and that what came back is the
# bare primary rather than a fallback wrapper.


def test_no_fallback_when_the_backup_has_no_credentials(monkeypatch):
    """An Ollama-only box must still build — and must not wire a keyless Gemini."""
    built = _providers(
        monkeypatch,
        ollama=FakeModel(broken=True),
        gemini=FakeModel(reply=FROM_FALLBACK),
    )
    settings = _settings(primary_provider="ollama", fallback_provider="gemini", gemini_api_key=None)

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert built == ["ollama"], "a keyless Gemini must never be constructed"
    assert not isinstance(model, RunnableWithFallbacks)
    with pytest.raises(RuntimeError, match=PRIMARY_DOWN):
        model.invoke([("user", "hi")])


def test_no_self_fallback_when_both_slots_name_the_same_provider(monkeypatch):
    """Falling back from a provider to itself buys nothing and doubles the wait."""
    primary = FakeModel(broken=True)
    built = _providers(monkeypatch, ollama=primary)
    settings = _settings(primary_provider="ollama", fallback_provider="ollama")

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert built == ["ollama"], "the same provider must not be constructed twice"
    assert model is primary


def test_no_fallback_when_none_is_configured(monkeypatch):
    """`FALLBACK_PROVIDER` unset is a supported single-provider deployment."""
    primary = FakeModel(broken=True)
    built = _providers(monkeypatch, ollama=primary)
    settings = _settings(primary_provider="ollama", fallback_provider=None)

    model = build_dynamic_model(settings, TOOLS)(None, None)

    assert built == ["ollama"]
    assert model is primary


# --- the LangGraph contract ADR-004 rests on --------------------------------


def _react_agent(model: Any):
    from langgraph.prebuilt import create_react_agent

    return create_react_agent(model, tools=TOOLS, prompt="x")


def _answer(agent: Any) -> str:
    return agent.invoke({"messages": [("user", "hi")]})["messages"][-1].content


def test_langgraph_contract_callable_keeps_the_fallback_live():
    """The workaround: a `(state, runtime) -> model` callable is passed through.

    This is the load-bearing half. If it goes red, the agent is silently running
    without a backup provider.
    """
    wrapped = FakeModel(broken=True).with_fallbacks([FakeModel(reply=FROM_FALLBACK)])

    assert wrapped.invoke([("user", "hi")]).content == FROM_FALLBACK, "control: the wrapper works"
    assert _answer(_react_agent(lambda state, runtime: wrapped)) == FROM_FALLBACK


class _Annotated(FakeModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable:
        return self


class _Unannotated(FakeModel):
    def bind_tools(self, tools, **kwargs):  # deliberately no return annotation
        return self


@pytest.mark.parametrize(
    ("model_cls", "expected"),
    [(_Annotated, FROM_FALLBACK), (_Unannotated, None)],
    ids=["annotated-keeps-it", "unannotated-loses-it"],
)
def test_the_direct_pass_survives_only_by_a_return_annotation(model_cls, expected):
    """Why the callable stays even though the direct pass now *works*.

    `create_react_agent` does not know about fallbacks: it sees something that is
    not a `RunnableBinding`, so it calls `.bind_tools(tools)` on it.
    `RunnableWithFallbacks.__getattr__` then decides what that means by reading
    the **return type annotation** of the wrapped model's `bind_tools`
    (`_returns_runnable`): annotated as a `Runnable` → the call is broadcast to
    the fallbacks and the wrapper survives; unannotated → the primary's bound
    method is handed back alone and the backup is dropped in silence.

    Both models below are identical apart from that annotation. The design does
    not rely on the outcome either way — the dynamic callable never reaches this
    code path — and this test exists to record that the direct pass hangs on a
    type hint, which is a thin thing to bet a production fallback on.
    """
    primary, backup = model_cls(broken=True), model_cls(reply=FROM_FALLBACK)
    wrapped = primary.bind_tools(TOOLS).with_fallbacks([backup.bind_tools(TOOLS)])

    if expected is None:
        with pytest.raises(RuntimeError, match=PRIMARY_DOWN):
            _answer(_react_agent(wrapped))
    else:
        assert _answer(_react_agent(wrapped)) == expected


@pytest.mark.parametrize("provider", ["ollama", "gemini"])
def test_the_shipped_providers_are_on_the_safe_side_of_that_annotation(provider):
    """The precondition for the migration ADR-004 now points at.

    Skipped where the provider extra isn't installed (CI installs `[dev,mcp]`
    only), so it guards the dev box, not the pipeline.
    """
    from langchain_core.runnables.fallbacks import _returns_runnable

    if provider == "ollama":
        pytest.importorskip("langchain_ollama")
        from langchain_ollama import ChatOllama

        model: BaseChatModel = ChatOllama(model="qwen2.5:0.5b")
    else:
        pytest.importorskip("langchain_google_genai")
        from langchain_google_genai import ChatGoogleGenerativeAI

        model = ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key="x")

    assert _returns_runnable(model.bind_tools), (
        f"{provider}: bind_tools stopped declaring a Runnable return type — a "
        "RunnableWithFallbacks over it would silently drop the backup"
    )


def test_our_own_constrained_wrapper_declares_it_too():
    """`ConstrainedToolModel` (ADR-011) sits in the primary slot on a tiny box.

    Unlike the providers above this class is ours, so the annotation is ours to
    keep — and this runs everywhere, no extra required.
    """
    from langchain_core.runnables.fallbacks import _returns_runnable

    from src.agent.constrained import ConstrainedToolModel

    model = ConstrainedToolModel(base=FakeModel())

    assert _returns_runnable(model.bind_tools)
