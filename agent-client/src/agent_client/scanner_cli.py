"""Command-line entry point for the incident scanner.

Reads incidents from a CSV file, runs each through the knowledgebase agent
to check documentation coverage, and writes results to an output CSV.

Usage:
    scan-cli incidents.csv                    # output: scan_results.csv
    scan-cli incidents.csv -o my_results.csv  # custom output path
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# load .env from project root (agent-client/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# -- Logging --
LOG_LEVEL = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("agent_client.scanner_cli")


def main():
    """Entry point invoked by the scan-cli console script."""
    parser = argparse.ArgumentParser(
        description="Scan incidents against the knowledgebase for documentation coverage.",
    )
    parser.add_argument(
        "incidents_csv",
        type=Path,
        help="Path to the incidents CSV file.",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("scan_results.csv"),
        help="Output CSV path (default: scan_results.csv).",
    )
    args = parser.parse_args()

    if not args.incidents_csv.exists():
        print(f"Error: file not found: {args.incidents_csv}", file=sys.stderr)
        sys.exit(1)

    from agent_client.scanner import CsvIncidentSource, run_scan

    source = CsvIncidentSource(args.incidents_csv)
    incidents = source.fetch_open_incidents()

    if not incidents:
        print("No open incidents found in the input file.")
        sys.exit(0)

    print(f"Found {len(incidents)} open incident(s). Starting scan ...\n")

    results = asyncio.run(run_scan(incidents, args.output))

    # summary
    covered = sum(1 for r in results if r.has_coverage)
    gaps = len(results) - covered
    print(f"\nScan complete: {covered} covered, {gaps} gap(s) out of {len(results)} incident(s).")
    print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
