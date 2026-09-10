"""Shared LangGraph loop for the four domain agents.

Facts and reasoning are kept apart: twin numbers are collected deterministically
into evidence.twin_facts with their vintages, and the model only writes the
analysis block. That separation is what makes the report auditable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from ..rag.retrieve import retrieve
from .llm import Backend, complete_json, resolve_backend

RAG_TOP_K = 3


class AgentState(TypedDict, total=False):
    scenario: str
    admin_id: str
    domain_data: dict
    citations: list[dict]
    twin_facts: list[dict]
    analysis: dict
    error: str


@dataclass
class AgentSpec:
    """Everything that distinguishes one domain agent from another."""

    name: str
    role: str
    domain_inputs: str
    core_question: str
    output_schema: dict[str, str]
    gather: Callable[[str], dict]
    facts: Callable[[dict], list[dict]]
    rag_query: Callable[[str], str]
    guidance: str = ""


def flatten_fact(label: str, tag: Any, **extra) -> dict | None:
    """Turn one tagged value from the twin into a citable fact.

    Drops anything that is not a tagged value, so a fact in a report always has
    a vintage attached to it.
    """
    if not isinstance(tag, dict) or "value" not in tag:
        return None
    return {
        "label": label,
        "value": tag.get("value"),
        "data_year": tag.get("data_year"),
        "revision_status": tag.get("revision_status"),
        "boundary_vintage": tag.get("boundary_vintage"),
        "match_confidence": tag.get("match_confidence"),
        "source": tag.get("source"),
        "vintage": tag.get("vintage"),
        **extra,
    }


SYSTEM_TEMPLATE = """You are the {role} for an Ernakulam district urban planning \
system. You answer exactly one question: {core_question}

You are given DOMAIN DATA drawn from a digital twin (PostGIS + Neo4j) and \
DOCUMENT CONTEXT retrieved from government publications.

Hard rules:
- Use only the numbers given. Never invent a figure. If something you need is \
absent, say so in `risks` or `assumptions` rather than estimating silently.
- Every value in the domain data carries vintage metadata (data_year, \
revision_status, boundary_vintage). When you cite a number, name its data_year. \
Data marked match_confidence "unmatched-estimate" is not a measurement of this \
admin unit — treat it as indicative only and say so.
- Reply with a single JSON object and nothing else.

The JSON object must have exactly these keys:
{schema}"""

USER_TEMPLATE = """SCENARIO
{scenario}

TARGET ADMIN UNIT
{admin_id}

DOMAIN DATA (from the digital twin)
{domain_data}

DOCUMENT CONTEXT (retrieved excerpts)
{citations}

{guidance}
Reply with the JSON object only."""


def _format_citations(citations: list[dict]) -> str:
    if not citations:
        return "(no relevant documents retrieved)"
    return "\n\n".join(
        f"[{index}] {c['source_file']} (chunk {c['chunk_index']}, score {c['score']}):\n{c['text']}"
        for index, c in enumerate(citations, 1)
    )


def build_agent(spec: AgentSpec, backend: Backend | None = None):
    """Compile the four-node LangGraph for one domain agent."""
    from langgraph.graph import END, START, StateGraph

    def gather_domain_data(state: AgentState) -> AgentState:
        return {"domain_data": spec.gather(state["admin_id"])}

    def gather_context(state: AgentState) -> AgentState:
        query = spec.rag_query(state["scenario"])
        try:
            citations = retrieve(query, admin_id=state["admin_id"], top_k=RAG_TOP_K)
        except Exception as error:
            # An empty or unreachable index degrades the analysis; it must not
            # take the agent down, and the gap has to be visible downstream.
            return {"citations": [], "error": f"retrieval unavailable: {error}"}
        return {"citations": citations}

    def reason(state: AgentState) -> AgentState:
        facts = spec.facts(state["domain_data"])
        system = SYSTEM_TEMPLATE.format(
            role=spec.role,
            core_question=spec.core_question,
            schema=json.dumps(spec.output_schema, indent=2),
        )
        user = USER_TEMPLATE.format(
            scenario=state["scenario"],
            admin_id=state["admin_id"],
            domain_data=json.dumps(facts, indent=2, default=str),
            citations=_format_citations(state.get("citations", [])),
            guidance=spec.guidance,
        )
        analysis = complete_json(system, user, backend)
        return {"analysis": analysis, "twin_facts": facts}

    graph = StateGraph(AgentState)
    graph.add_node("gather_domain_data", gather_domain_data)
    graph.add_node("gather_context", gather_context)
    graph.add_node("reason", reason)
    graph.add_edge(START, "gather_domain_data")
    graph.add_edge("gather_domain_data", "gather_context")
    graph.add_edge("gather_context", "reason")
    graph.add_edge("reason", END)
    return graph.compile()


def run_agent(spec: AgentSpec, scenario: str, admin_id: str, backend: Backend | None = None) -> dict:
    """Run one domain agent and return its structured output envelope."""
    backend = backend or resolve_backend()
    compiled = build_agent(spec, backend)
    state = compiled.invoke({"scenario": scenario, "admin_id": admin_id})

    analysis = state.get("analysis", {}) or {}
    # Normalise: guarantee every schema key exists, so downstream code and the
    # Supervisor can compare agents without defensive .get() everywhere.
    for key in spec.output_schema:
        analysis.setdefault(key, None)

    return {
        "agent": spec.name,
        "admin_id": admin_id,
        "scenario": scenario,
        "generated_at": datetime.now().astimezone().isoformat(),
        "backend": {"kind": backend.kind, "model": backend.model, "note": backend.note or None},
        "analysis": analysis,
        "evidence": {
            "twin_facts": state.get("twin_facts", []),
            "citations": [
                {"source_file": c["source_file"], "chunk_index": c["chunk_index"],
                 "score": c["score"], "text": c["text"]}
                for c in state.get("citations", [])
            ],
        },
        "warnings": [w for w in [state.get("error")] if w],
    }
