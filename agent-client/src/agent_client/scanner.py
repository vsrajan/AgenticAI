"""Batch scan engine -- runs each incident through the knowledgebase agent.

Reuses the existing LangGraph graph from agent.py without modification.
For each incident, compiles the graph fresh (no checkpointer) and invokes
the knowledgebase_agent directly to search documentation for coverage.

Typical call sequence:

    scan_cli.main()
      +-> CsvIncidentSource.fetch_open_incidents()
      |     reads incidents.csv -> list[Incident]
      |
      +-> scanner.run_scan(source, output_path)
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
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_client.llm import get_llm
from agent_client.agent import build_graph, _get_mcp_server_config

logger = logging.getLogger("agent_client.scanner")


# -- Data classes --

@dataclass
class Incident:
    """A single incident record from the source CSV."""
    id: str
    short_description: str
    description: str
    priority: str
    state: str
    category: str
    subcategory: str
    assignment_group: str
    assigned_to: str
    opened_date: str
    resolved_date: str
    resolution_notes: str


@dataclass
class ScanResult:
    """Result of scanning one incident against the knowledgebase."""
    incident_id: str
    short_description: str
    category: str
    subcategory: str
    question: str
    answer: str
    has_coverage: bool
    matched_topics: list[str] = field(default_factory=list)


# -- Incident source --

class CsvIncidentSource:
    """Reads incidents from a CSV file."""

    # maps CSV column headers to Incident field names
    _FIELD_MAP = {
        "IncidentID": "id",
        "ShortDescription": "short_description",
        "Description": "description",
        "Priority": "priority",
        "State": "state",
        "Category": "category",
        "Subcategory": "subcategory",
        "AssignmentGroup": "assignment_group",
        "AssignedTo": "assigned_to",
        "OpenedDate": "opened_date",
        "ResolvedDate": "resolved_date",
        "ResolutionNotes": "resolution_notes",
    }

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def fetch_open_incidents(self) -> list[Incident]:
        """Read CSV and return only open/in-progress incidents."""
        incidents: list[Incident] = []
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                mapped = {
                    field_name: row.get(csv_col, "").strip()
                    for csv_col, field_name in self._FIELD_MAP.items()
                }
                incident = Incident(**mapped)
                if incident.state.lower() in ("open", "in progress", "new"):
                    incidents.append(incident)
        logger.info("Loaded %d open incidents from %s", len(incidents), self.csv_path)
        return incidents


# -- Scan engine --

def _build_question(incident: Incident) -> str:
    """Turn an incident into a knowledgebase search question."""
    return (
        f"I have an access governance incident that needs documentation coverage analysis.\n\n"
        f"Incident: {incident.short_description}\n"
        f"Category: {incident.category} / {incident.subcategory}\n"
        f"Description: {incident.description}\n\n"
        f"Search the knowledgebase for any documentation that covers the scenario "
        f"described in this incident. Tell me:\n"
        f"1. Which documents and pages are relevant (with citations).\n"
        f"2. Whether the existing documentation adequately covers how to resolve "
        f"or prevent this type of incident.\n"
        f"3. If there is a gap -- what specific documentation is missing."
    )


def _parse_coverage(answer: str) -> tuple[bool, list[str]]:
    """Heuristic: check if the agent found relevant documentation.

    Returns (has_coverage, list_of_matched_topic_names).
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
    import re
    for match in re.finditer(r"\(Source:\s*([^,)]+)", answer):
        topic = match.group(1).strip()
        if topic and topic not in topics:
            topics.append(topic)

    has_coverage = bool(topics) and not has_gap
    return has_coverage, topics


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
    async with MultiServerMCPClient(mcp_config) as client:
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
                final_state = await agent.ainvoke({
                    "messages": [HumanMessage(content=question)],
                    "active_agent": "knowledgebase_agent",
                })

                # extract the last AI message as the answer
                answer = ""
                for msg in reversed(final_state["messages"]):
                    if isinstance(msg, AIMessage) and msg.content:
                        answer = msg.content
                        break

                has_coverage, topics = _parse_coverage(answer)

                results.append(ScanResult(
                    incident_id=incident.id,
                    short_description=incident.short_description,
                    category=incident.category,
                    subcategory=incident.subcategory,
                    question=question,
                    answer=answer,
                    has_coverage=has_coverage,
                    matched_topics=topics,
                ))
                logger.info(
                    "  -> coverage=%s, topics=%d",
                    has_coverage, len(topics),
                )

            except Exception:
                logger.exception("  -> ERROR scanning %s", incident.id)
                results.append(ScanResult(
                    incident_id=incident.id,
                    short_description=incident.short_description,
                    category=incident.category,
                    subcategory=incident.subcategory,
                    question=question,
                    answer="ERROR: scan failed -- see logs",
                    has_coverage=False,
                ))

    _write_results_csv(results, output_path)
    return results


def _write_results_csv(results: list[ScanResult], path: Path) -> None:
    """Write scan results to a CSV file."""
    fieldnames = [
        "IncidentID",
        "ShortDescription",
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
                "Category": r.category,
                "Subcategory": r.subcategory,
                "HasCoverage": r.has_coverage,
                "MatchedTopics": "; ".join(r.matched_topics),
                "Answer": r.answer,
            })
    logger.info("Results written to %s (%d rows)", path, len(results))
