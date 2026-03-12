"""Batch scan engine -- runs each incident through the knowledgebase agent.

Reuses the existing LangGraph graph from agent.py without modification.
For each incident, compiles the graph fresh (no checkpointer) and invokes
the knowledgebase_agent directly to search documentation for coverage.

Typical call sequence:

    scan_cli.main()
      +-> source.fetch_open_incidents()
      |     reads incidents -> list[Incident]
      |
      +-> scanner.run_scan(incidents, output_path)
            +-> get_llm(), _get_mcp_server_config(), build_graph()
            |
            +-> for each incident:
            |     +-> graph.compile()  (fresh, no checkpointer)
            |     +-> ainvoke(messages=[question], active_agent="knowledgebase_agent")
            |     +-> knowledgebase_agent <-> knowledgebase_tools -> answer
            |     +-> collect ScanResult
            |
            +-> _write_results_csv(results)
"""

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_client.llm import get_llm
from agent_client.agent import build_graph, _get_mcp_server_config
from agent_client.incident_sources import Incident

logger = logging.getLogger("agent_client.scanner")


# -- Data classes --

@dataclass
class ScanResult:
    """Result of scanning one incident against the knowledgebase."""
    incident_id: str
    short_description: str
    description: str
    category: str
    subcategory: str
    question: str
    answer: str
    has_coverage: str  # "full", "partial", or "none"
    matched_topics: list[str] = field(default_factory=list)


# -- Scan engine --

def _build_question(incident: Incident) -> str:
    """Turn an incident into a knowledgebase search question."""
    return f"""\
I have an access governance incident that needs documentation coverage analysis.

Incident: {incident.short_description}
Category: {incident.category} / {incident.subcategory}
Description: {incident.description}

Search the knowledgebase for any documentation that covers the scenario \
described in this incident. Tell me:
1. Which documents and pages are relevant (with citations).
2. Whether the existing documentation adequately covers how to resolve \
or prevent this type of incident.
3. If there is a gap -- what specific documentation is missing."""


def _parse_coverage(answer: str) -> tuple[str, list[str]]:
    """Heuristic: check if the agent found relevant documentation.

    Returns (coverage, list_of_matched_topic_names) where coverage is
    "full", "partial", or "none".
    """
    lower = answer.lower()

    # negative signals -- agent explicitly says no coverage
    no_coverage_phrases = [
        "no documentation",
        "no relevant documentation",
        "not covered",
        "no existing documentation",
        "documentation gap",
        "gap in documentation",
        "no results",
        "could not find",
        "no matching",
    ]
    has_gap = any(phrase in lower for phrase in no_coverage_phrases)

    # positive signal -- citations like (Source: topic/file.pdf, Page N)
    topics: list[str] = []
    for match in re.finditer(r"\(Source:\s*([^,)]+)", answer):
        topic = match.group(1).strip()
        if topic and topic not in topics:
            topics.append(topic)

    if topics and not has_gap:
        coverage = "full"
    elif topics and has_gap:
        coverage = "partial"
    else:
        coverage = "none"
    return coverage, topics


async def run_scan(
    incidents: list[Incident],
    output_path: Path,
) -> list[ScanResult]:
    """Run the knowledgebase agent against each incident and write results.

    For each incident:
    - Compiles a fresh graph (no checkpointer, no conversation history)
    - Invokes with active_agent="knowledgebase_agent" so the router is skipped
    - Collects the answer and parses coverage heuristics
    """
    llm = get_llm()
    mcp_config = _get_mcp_server_config()

    logger.info("Connecting to MCP server ...")
    client = MultiServerMCPClient(mcp_config)
    tools = await client.get_tools()
    logger.info("Loaded %d MCP tools", len(tools))

    graph = build_graph(llm, tools)
    results: list[ScanResult] = []

    for i, incident in enumerate(incidents, 1):
        logger.info(
            "[%d/%d] Scanning %s: %s",
            i, len(incidents), incident.id, incident.short_description,
        )
        question = _build_question(incident)

        # compile fresh each time -- no checkpointer, no shared state
        agent = graph.compile()

        try:
            final_state = await agent.ainvoke(
                {
                    "messages": [HumanMessage(content=question)],
                    "active_agent": "knowledgebase_agent",
                },
                {"recursion_limit": 50},
            )

            # extract the last AI message as the answer
            answer = ""
            for msg in reversed(final_state["messages"]):
                if isinstance(msg, AIMessage) and msg.content:
                    answer = msg.content
                    break

            coverage, topics = _parse_coverage(answer)
            logger.debug("incident.id=%r for %s", incident.id, incident.short_description)

            results.append(ScanResult(
                incident_id=incident.id,
                short_description=incident.short_description,
                description=incident.description,
                category=incident.category,
                subcategory=incident.subcategory,
                question=question,
                answer=answer,
                has_coverage=coverage,
                matched_topics=topics,
            ))
            logger.info(
                "  -> coverage=%s, topics=%d",
                coverage, len(topics),
            )

        except Exception:
            logger.exception("  -> ERROR scanning %s", incident.id)
            results.append(ScanResult(
                incident_id=incident.id,
                short_description=incident.short_description,
                description=incident.description,
                category=incident.category,
                subcategory=incident.subcategory,
                question=question,
                answer="ERROR: scan failed -- see logs",
                has_coverage="none",
            ))

    _write_results_csv(results, output_path)
    return results


def _write_results_csv(results: list[ScanResult], path: Path) -> None:
    """Write scan results to a CSV file."""
    fieldnames = [
        "IncidentID",
        "ShortDescription",
        "Description",
        "Category",
        "Subcategory",
        "HasCoverage",
        "MatchedTopics",
        "Answer",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "IncidentID": r.incident_id,
                "ShortDescription": r.short_description,
                "Description": r.description,
                "Category": r.category,
                "Subcategory": r.subcategory,
                "HasCoverage": r.has_coverage,
                "MatchedTopics": "; ".join(r.matched_topics),
                "Answer": r.answer,
            })
    logger.info("Results written to %s (%d rows)", path, len(results))
