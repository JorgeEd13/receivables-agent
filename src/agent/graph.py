"""The ReAct agent: a LangGraph loop over the guarded SQL tool.

Wiring note (the dual-provider fallback): LangGraph's ``create_react_agent``
auto-binds tools to a single chat model and won't accept a fallback runnable
directly. So we bind the tools to *each* provider, combine them with
``with_fallbacks``, and hand the result to the agent as a **dynamic model
callable** — a plain function ``(state, runtime) -> model``. LangGraph treats a
callable as a pre-built model and skips its own binding, which lets the active
fallback flow through every model call in the loop.
"""

from __future__ import annotations

import chromadb
import duckdb
from langgraph.prebuilt import create_react_agent

from src.agent.cached_agent import CachedAgent
from src.agent.plan_cache import get_plan_cache
from src.agent.providers import build_chat_model, has_credentials
from src.agent.schema_hints import SCHEMA_HINTS
from src.agent.tools import make_query_ledger_tool, make_search_policy_tool
from src.core.config import Settings, get_settings
from src.agent.ledger import connect_readonly
from src.rag.embeddings import default_embedding_function
from src.rag.index import ensure_policy_index

SYSTEM_PROMPT = f"""\
You are a careful accounts-receivable analyst. Answer questions about overdue
invoices, aging, DSO, collections policy and which customers to prioritize.

You have two tools, and the best answers use them together:
- `query_ledger` — guarded read-only DuckDB SELECT for any number, name or fact
  (who owes what, aging, DSO). Never guess data or use prior knowledge of it.
- `search_policy` — retrieves the company's written collections policy (rules,
  thresholds, cadence, processes) with the section heading to cite.

Rules:
- Use BOTH tools when a question mixes facts and rules: get the number from
  `query_ledger` AND the governing rule from `search_policy`, then combine them.
  For example "which overdue accounts should go on credit hold?" — look up the
  credit-hold rule with `search_policy`, then find the matching accounts with
  `query_ledger`. Don't state a policy threshold from memory; retrieve it.
- Write standard DuckDB SQL. Prefer the analytics views. Aggregate in SQL rather
  than pulling raw rows; the tool caps the number of rows returned.
- If a query is rejected or errors, read the reason and fix the SQL — do not try
  to bypass the guardrail.
- Ground every claim in tool results, cite the policy section when you rely on
  it, and answer concisely. State your assumptions when a question is ambiguous
  (e.g. what "overdue" threshold).

{SCHEMA_HINTS}"""


def build_dynamic_model(settings: Settings, tools: list):
    """Return a `(state, runtime) -> model` callable with active fallback.

    Tools are bound to each provider; the primary is wrapped with the fallback
    (when the fallback has credentials and differs from the primary).
    """
    primary = build_chat_model(settings.primary_provider, settings).bind_tools(tools)

    fallbacks = []
    fb = settings.fallback_provider
    if fb and fb != settings.primary_provider and has_credentials(fb, settings):
        fallbacks.append(build_chat_model(fb, settings).bind_tools(tools))

    model = primary.with_fallbacks(fallbacks) if fallbacks else primary

    def _dynamic(state, runtime):  # noqa: ARG001 - signature required by langgraph
        return model

    return _dynamic


def build_agent(
    settings: Settings | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
):
    """Build the compiled ReAct agent.

    `con` defaults to a read-only connection to ``settings.ledger_path``; pass
    one in (e.g. in tests) to reuse a connection.
    """
    settings = settings or get_settings()
    con = con or connect_readonly(settings.ledger_path)
    policy = ensure_policy_index(settings)

    tools = [
        make_query_ledger_tool(con, settings.max_rows),
        make_search_policy_tool(policy, settings.search_k),
    ]
    model = build_dynamic_model(settings, tools)
    agent = create_react_agent(model, tools=tools, prompt=SYSTEM_PROMPT)

    if not settings.plan_cache_enabled:
        return agent

    # Wrap with the semantic plan-cache (ADR-009): reuse the same ChromaDB
    # client + local MiniLM embeddings as the policy index — no new dependency —
    # and hand the cache the same read-only connection + policy collection so a
    # hit is replayed *live* (never a frozen answer).
    client = chromadb.PersistentClient(path=settings.chroma_path)
    cache = get_plan_cache(
        client,
        settings.plan_cache_collection,
        default_embedding_function(),
        similarity_threshold=settings.plan_cache_threshold,
    )
    return CachedAgent(
        agent,
        cache,
        con,
        policy,
        max_rows=settings.max_rows,
        search_k=settings.search_k,
    )
