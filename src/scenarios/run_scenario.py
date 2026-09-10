"""Stage 8-9 — run a scenario end to end and write a cited Markdown report.

Every figure in the report's Evidence tables came from the digital twin with its
vintage printed beside it. The traceability audit then flags any number the
model wrote into its own prose that matches nothing it was given.

    python src/scenarios/run_scenario.py --admin-id ID --scenario "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agents.supervisor_agent import run as run_supervisor  # noqa: E402
from src.digital_twin.twin import get_context  # noqa: E402

REPORTS_DIR = PROJECT_ROOT / "data" / "derived" / "scenario_reports"


def _fmt(value) -> str:
    if value is None:
        return "_null_"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value) if value else "_none_"
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


def _vintage(fact: dict) -> str:
    """The vintage tag as it appears in the report — never blank."""
    parts = []
    if fact.get("data_year") is not None:
        parts.append(f"year={fact['data_year']}")
    if fact.get("revision_status"):
        parts.append(str(fact["revision_status"]))
    if fact.get("boundary_vintage"):
        parts.append(str(fact["boundary_vintage"]))
    if fact.get("match_confidence"):
        parts.append(f"confidence={fact['match_confidence']}")
    if fact.get("vintage") == "unpublished_by_source":
        parts.append("vintage unpublished by source")
    return ", ".join(parts) or "_no vintage published_"


def _facts_table(facts: list[dict]) -> str:
    if not facts:
        return "_No digital-twin facts were gathered._\n"
    lines = ["| Fact | Value | Vintage | Source |", "| --- | --- | --- | --- |"]
    for fact in facts:
        lines.append(
            f"| `{fact.get('label')}` | {_fmt(fact.get('value'))} | {_vintage(fact)} | "
            f"{fact.get('source') or '_unstated_'} |"
        )
    return "\n".join(lines) + "\n"


def _analysis_block(analysis: dict) -> str:
    lines = []
    for key, value in analysis.items():
        lines.append(f"- **{key}**: {_fmt(value)}")
    return "\n".join(lines) + "\n"


# Standalone numbers only: the lookarounds keep identifier fragments out, so
# "DEMO-BS-02" and "pre-2025-delimitation" are not read as claimed figures.
NUMBER_PATTERN = __import__("re").compile(r"(?<![\w.-])-?\d[\d,]*\.?\d*(?![\w-])")


def _known_numbers(agent_result: dict) -> set[str]:
    """Every number the agent was actually given, from twin facts and citations."""
    known: set[str] = set()
    for fact in agent_result.get("evidence", {}).get("twin_facts", []):
        for token in NUMBER_PATTERN.findall(str(fact.get("value"))):
            known.add(token.replace(",", "").rstrip("."))
        for extra in ("year", "data_year"):
            if fact.get(extra) is not None:
                known.add(str(fact[extra]))
    for citation in agent_result.get("evidence", {}).get("citations", []):
        for token in NUMBER_PATTERN.findall(citation.get("text") or ""):
            known.add(token.replace(",", "").rstrip("."))
    return known


def audit_unsourced_numbers(agent_result: dict) -> list[dict]:
    """Numbers in the model's prose that appear in none of its sources.

    Step 9's proof point is that every factual claim traces to the twin or to a
    cited chunk. The Evidence tables guarantee that for the twin's figures, but
    the model can still write a number of its own into the analysis. Rather than
    trust the prompt's "never invent a figure" instruction to hold, this measures
    it: any numeric token in the analysis that is absent from the agent's own
    inputs is listed in the report as unverified.
    """
    known = _known_numbers(agent_result)
    findings: list[dict] = []
    for key, value in (agent_result.get("analysis") or {}).items():
        for token in NUMBER_PATTERN.findall(str(value)):
            normalised = token.replace(",", "").rstrip(".")
            if len(normalised.lstrip("-").replace(".", "")) < 2:
                continue  # single digits are usually list numbering, not claims
            if normalised in known:
                continue
            # A rounded restatement of a known value is not an invention.
            try:
                if any(abs(float(normalised) - float(k)) < 0.01 for k in known
                       if k.replace(".", "").replace("-", "").isdigit()):
                    continue
            except ValueError:
                pass
            findings.append({"field": key, "number": token,
                             "context": str(value)[:160]})
    return findings


def build_report(result: dict, context: dict) -> str:
    scenario = result["scenario"]
    admin_ids = result["admin_ids"]
    backend = result["backend"]
    supervisor = result["supervisor"]

    out = [
        "# Ernakulam Digital Twin — Scenario Report",
        "",
        f"- **Generated**: {result['generated_at']}",
        f"- **Target admin unit(s)**: {', '.join(admin_ids)}",
        f"- **Dataset edition**: {context.get('dataset_edition', '_unknown_')}",
        f"- **Reasoning backend**: {backend['kind']} / `{backend['model']}`",
    ]
    if backend.get("note"):
        out.append(f"- **Backend note**: {backend['note']}")
    if result.get("warnings"):
        out.append(f"- **Warnings**: {'; '.join(result['warnings'])}")

    out += [
        "",
        "> Every figure in the Evidence tables below was returned by the digital twin",
        "> and is printed with the vintage it was published under. Every excerpt is",
        "> printed with its source filename. The language model contributed the",
        "> judgement in the Analysis and Supervisor sections, not the numbers.",
        "",
        "## 1. Scenario",
        "",
        scenario,
        "",
        "## 2. Target context",
        "",
    ]

    if context.get("found"):
        boundary = context["boundary"]
        rows = [
            {"label": "name", **boundary["name"]},
            {"label": "level", **boundary["level"]},
            {"label": "area_m2", **boundary["area_m2"]},
        ]
        if context.get("population"):
            rows.append({"label": "population", **context["population"]["population"]})
            rows.append({"label": "literacy_rate", **context["population"]["literacy_rate"]})
        out.append(_facts_table(rows))
    else:
        out.append(f"_{context.get('note', 'Admin unit not found.')}_\n")

    out += ["## 3. Domain agent analyses", ""]
    for name, agent_result in sorted(result["agent_outputs"].items()):
        out.append(f"### 3.{sorted(result['agent_outputs']).index(name) + 1} {name.title()} Agent")
        out.append("")
        if agent_result.get("error"):
            out += [f"**Agent failed**: {agent_result['error']}", ""]
            continue
        out += ["#### Analysis (model judgement)", "", _analysis_block(agent_result["analysis"]), ""]
        out += ["#### Evidence — digital twin facts", "",
                _facts_table(agent_result["evidence"]["twin_facts"]), ""]
        unsourced = audit_unsourced_numbers(agent_result)
        out.append("#### Traceability audit")
        out.append("")
        if unsourced:
            out.append(f"**{len(unsourced)} figure(s) in this agent's prose appear in none of its "
                       "sources** — treat as model-generated, not evidence:")
            out.append("")
            for finding in unsourced:
                out.append(f"- `{finding['field']}`: **{finding['number']}** — {finding['context']}")
        else:
            out.append("_Every figure in this agent's analysis traces to a twin fact or a cited chunk._")
        out.append("")

        citations = agent_result["evidence"]["citations"]
        out.append("#### Evidence — cited documents")
        out.append("")
        if citations:
            for citation in citations:
                out.append(f"- **{citation['source_file']}** (chunk {citation['chunk_index']}, "
                           f"score {citation['score']}):")
                out.append(f"  > {citation['text']}")
        else:
            out.append("_No documents retrieved._")
        out.append("")

    out += ["## 4. Supervisor — final recommendation", ""]
    out += [f"**{_fmt(supervisor.get('final_recommendation'))}**", ""]
    out += [f"- **Rationale**: {_fmt(supervisor.get('rationale'))}",
            f"- **Confidence**: {_fmt(supervisor.get('confidence'))}",
            f"- **Dissenting agents**: {_fmt(supervisor.get('dissenting_agents'))}", ""]

    out += ["### 4.1 Trade-offs stated", ""]
    trade_offs = supervisor.get("trade_offs") or []
    if trade_offs:
        for item in trade_offs:
            out.append(f"- {_fmt(item)}")
    else:
        out.append("_The Supervisor stated no trade-offs._")
    out.append("")

    out += ["### 4.2 Conflicts detected deterministically", "",
            "_Computed from the agents' own JSON before the Supervisor saw them, so a "
            "disagreement cannot be lost by the model._", ""]
    detected = supervisor.get("detected_conflicts") or []
    if detected:
        for conflict in detected:
            out.append(f"- **{' vs '.join(conflict['between'])}** ({conflict['kind']}): {conflict['tension']}")
    else:
        out.append("_No conflicts detected._")
    out.append("")

    out += ["### 4.3 Conditions", ""]
    conditions = supervisor.get("conditions") or []
    out += ([f"- {_fmt(c)}" for c in conditions] if conditions else ["_None stated._"])
    out.append("")

    out += ["### 4.4 Alternatives considered", ""]
    alternatives = supervisor.get("alternative_options") or []
    out += ([f"- {_fmt(a)}" for a in alternatives] if alternatives else ["_None stated._"])
    out.append("")

    out += ["## 5. Sources cited", "", "| Source file | Chunk | Used by | Vintage |",
            "| --- | --- | --- | --- |"]
    usage: dict[tuple, set] = {}
    for name, agent_result in result["agent_outputs"].items():
        for citation in agent_result.get("evidence", {}).get("citations", []):
            usage.setdefault((citation["source_file"], citation["chunk_index"]), set()).add(name)
    for citation in result.get("budget_context", []):
        usage.setdefault((citation["source_file"], citation["chunk_index"]), set()).add("supervisor")
    for (source_file, chunk_index), agents in sorted(usage.items()):
        out.append(f"| `{source_file}` | {chunk_index} | {', '.join(sorted(agents))} | "
                   f"document excerpt, vintage as stated in text |")
    out.append("")

    out += ["## 6. Traceability summary", ""]
    total_unsourced = 0
    out += ["| Agent | Unverified figures in prose |", "| --- | --- |"]
    for name, agent_result in sorted(result["agent_outputs"].items()):
        if agent_result.get("error"):
            continue
        findings = audit_unsourced_numbers(agent_result)
        total_unsourced += len(findings)
        out.append(f"| {name} | {len(findings)} |")
    out.append("")
    out.append(
        "_All figures in the Evidence tables trace to the digital twin (vintage shown) "
        "or to a cited document chunk. The count above is figures the model wrote into "
        "its own prose that match nothing it was given — a model-quality signal, not "
        "twin data._" if total_unsourced else
        "_Every figure in this report traces to the digital twin or a cited document chunk._")
    out.append("")

    out += ["## 7. Data lineage", "",
            "- Raw inputs: `data/raw/` (see `data/raw/ingestion_manifest.csv` for sha256 per file)",
            "- Stage 2 findings: `data/processed/hard_check_failures.json`, "
            "`data/processed/soft_check_flags.json`",
            "- Store agreement: `data/processed/entity_count_agreement.json`",
            "- Records withheld by hard checks never reach this report.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_scenario", description="Run a scenario end to end and write a Markdown report.")
    parser.add_argument("--admin-id", required=True, action="append", dest="admin_ids")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)

    print(f"Running scenario for {', '.join(args.admin_ids)}")
    context = get_context(args.admin_ids[0])
    if not context.get("found"):
        print(f"  warning: {context.get('note')}", file=sys.stderr)

    result = run_supervisor(args.scenario, args.admin_ids)
    report = build_report(result, context)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = args.output_dir / f"{stamp}.md"
    path.write_text(report, encoding="utf-8")
    (args.output_dir / f"{stamp}.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")

    print(f"\nReport: {path.relative_to(PROJECT_ROOT)}")
    print(f"Raw   : {path.with_suffix('.json').relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
