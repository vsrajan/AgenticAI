#!/usr/bin/env python3
"""Interactive tester for the MCP docs indexer and BM25 search.

Creates sample PDF files in a temporary directory, builds the index,
and lets you exercise all three MCP tools (list_topics, search_docs,
read_page) from the command line.

Usage:
    uv run python tester.py              # run all automated tests
    uv run python tester.py --interactive # drop into an interactive search loop
"""

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

import pymupdf

from mcp_docs_server.pdf_indexer import DocIndex

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tester")

# ---------------------------------------------------------------------------
# Sample documents — each tuple is (relative_path, list_of_page_texts)
# ---------------------------------------------------------------------------
SAMPLE_DOCS: list[tuple[str, list[str]]] = [
    (
        "entitlements/ordering_faq.pdf",
        [
            "Ordering Entitlements FAQ\n\n"
            "Q: How do I order a new entitlement?\n"
            "A: Navigate to the Access Governance portal, select 'Request Access', "
            "choose the target application, and submit your request for approval.",
            "Q: Who approves entitlement requests?\n"
            "A: Your line manager approves first, followed by the application owner. "
            "Emergency requests bypass the manager step.\n\n"
            "Q: How long does approval take?\n"
            "A: Standard requests are processed within 48 hours.",
        ],
    ),
    (
        "entitlements/bulk_requests.pdf",
        [
            "Bulk Entitlement Requests\n\n"
            "For teams requiring access for multiple users, use the bulk request "
            "feature. Upload a CSV file with columns: employee_id, application, "
            "role. The system validates each row and creates individual requests.",
        ],
    ),
    (
        "delegations/setup_guide.pdf",
        [
            "Setting Up Delegations\n\n"
            "Delegations allow a manager to temporarily assign their approval "
            "authority to another user. This is useful during vacations or "
            "extended absences.",
            "To set up a delegation:\n"
            "1. Go to Settings > Delegations\n"
            "2. Click 'New Delegation'\n"
            "3. Select the delegate (the person who will approve on your behalf)\n"
            "4. Set start and end dates\n"
            "5. Click 'Activate'",
            "Important notes:\n"
            "- Delegations expire automatically on the end date\n"
            "- You can revoke a delegation at any time\n"
            "- The delegate receives an email notification\n"
            "- Audit logs capture all delegated approvals",
        ],
    ),
    (
        "jml/leaver_process.pdf",
        [
            "Leaver Process (Joiner-Mover-Leaver)\n\n"
            "When an employee leaves the organization, all their entitlements "
            "must be revoked. The automated leaver process handles this:\n\n"
            "1. HR marks the employee as 'leaving' in the HR system\n"
            "2. Access Governance receives the event via API\n"
            "3. All active entitlements are queued for revocation\n"
            "4. Application owners are notified\n"
            "5. Entitlements are revoked on the employee's last day",
        ],
    ),
    (
        "overview.pdf",
        [
            "Access Governance Application Overview\n\n"
            "This application provides centralized management of user access "
            "across the enterprise. Key features include:\n"
            "- Entitlement ordering and approval workflows\n"
            "- Delegated approvals for managers\n"
            "- Automated joiner-mover-leaver (JML) processes\n"
            "- Access certification campaigns\n"
            "- Segregation of duties (SoD) policy enforcement",
        ],
    ),
]


def _create_sample_pdfs(base_dir: Path) -> None:
    """Create sample PDF files from SAMPLE_DOCS."""
    for rel_path, pages in SAMPLE_DOCS:
        pdf_path = base_dir / rel_path
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        doc = pymupdf.open()
        for page_text in pages:
            page = doc.new_page()
            page.insert_text((72, 72), page_text, fontsize=11)
        doc.save(str(pdf_path))
        doc.close()
        logger.info("Created %s (%d pages)", rel_path, len(pages))


def _create_corrupt_pdf(base_dir: Path) -> None:
    """Create a corrupt PDF to test error handling."""
    corrupt_path = base_dir / "corrupt" / "bad_file.pdf"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("this is not a valid PDF")
    logger.info("Created corrupt/bad_file.pdf (intentionally malformed)")


def _pp(obj: object) -> str:
    """Pretty-print a dict/list as JSON."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


def run_automated_tests(index: DocIndex) -> bool:
    """Run a suite of automated checks. Returns True if all pass."""
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            logger.info("PASS: %s", name)
        else:
            failed += 1
            logger.error("FAIL: %s — %s", name, detail)

    # ------------------------------------------------------------------
    # 1. Topic tree
    # ------------------------------------------------------------------
    tree = index.get_topic_tree()
    check(
        "Topic tree has topics",
        "topics" in tree,
        f"got {list(tree.keys())}",
    )
    check(
        "Correct number of documents indexed",
        tree.get("total_documents") == len(SAMPLE_DOCS),
        f"expected {len(SAMPLE_DOCS)}, got {tree.get('total_documents')}",
    )
    topics = tree.get("topics", {})
    check(
        "Expected topics present",
        set(topics.keys()) == {"entitlements", "delegations", "jml", "general"},
        f"got {set(topics.keys())}",
    )

    # ------------------------------------------------------------------
    # 2. Search
    # ------------------------------------------------------------------
    results = index.search("delegation setup")
    check(
        "Search 'delegation setup' returns results",
        len(results) > 0 and "page_path" in results[0],
        f"got {len(results)} results",
    )
    check(
        "Top result is the delegation guide",
        results[0]["page_path"] == "delegations/setup_guide.pdf",
        f"got {results[0].get('page_path')}",
    )
    check(
        "Search results include total_pages",
        "total_pages" in results[0],
        f"keys: {list(results[0].keys())}",
    )

    results2 = index.search("entitlement order approval")
    check(
        "Search 'entitlement order approval' top result is ordering FAQ",
        results2[0]["page_path"] == "entitlements/ordering_faq.pdf",
        f"got {results2[0].get('page_path')}",
    )

    results3 = index.search("leaver revocation")
    check(
        "Search 'leaver revocation' finds JML doc",
        any(r["page_path"] == "jml/leaver_process.pdf" for r in results3),
        f"got {[r['page_path'] for r in results3]}",
    )

    empty = index.search("xyzzy_nonexistent_term")
    check(
        "Search for non-existent term returns no-match message",
        len(empty) == 1 and "message" in empty[0],
        f"got {empty}",
    )

    # ------------------------------------------------------------------
    # 3. Read page
    # ------------------------------------------------------------------
    page = index.read("delegations/setup_guide.pdf")
    check(
        "read exact path returns content",
        "content" in page,
        f"keys: {list(page.keys())}",
    )
    check(
        "Content has [Page N] markers",
        "[Page 1]" in page.get("content", ""),
        "missing [Page 1] marker",
    )
    check(
        "Read includes total_pages",
        page.get("total_pages") == 3,
        f"got total_pages={page.get('total_pages')}",
    )

    # Fuzzy match — filename only
    fuzzy = index.read("setup_guide")
    check(
        "Fuzzy match by stem works",
        fuzzy.get("page_path") == "delegations/setup_guide.pdf",
        f"got {fuzzy.get('page_path')}",
    )

    # Not found
    missing = index.read("nonexistent.pdf")
    check(
        "Read non-existent returns error",
        "error" in missing,
        f"keys: {list(missing.keys())}",
    )

    # ------------------------------------------------------------------
    # 4. Corrupt PDF handling
    # ------------------------------------------------------------------
    check(
        "Corrupt PDF was skipped (not in index)",
        "corrupt/bad_file.pdf" not in index._documents,
        "corrupt file was indexed unexpectedly",
    )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = passed + failed
    logger.info("=" * 60)
    logger.info("Results: %d/%d passed", passed, total)
    if failed:
        logger.error("%d test(s) FAILED", failed)
    else:
        logger.info("All tests passed!")
    return failed == 0


def run_interactive(index: DocIndex) -> None:
    """Drop into an interactive search loop."""
    print("\n--- Interactive Mode ---")
    print("Commands:")
    print("  topics          — list all topics")
    print("  search <query>  — search documents")
    print("  read <path>     — read a document")
    print("  quit            — exit\n")

    while True:
        try:
            line = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        elif line == "quit":
            break
        elif line == "topics":
            print(_pp(index.get_topic_tree()))
        elif line.startswith("search "):
            query = line[len("search "):]
            results = index.search(query)
            print(_pp(results))
        elif line.startswith("read "):
            path = line[len("read "):]
            result = index.read(path)
            # Truncate content for display
            if "content" in result:
                content = result["content"]
                if len(content) > 500:
                    result["content"] = content[:500] + f"\n... ({len(content)} chars total)"
            print(_pp(result))
        else:
            print(f"Unknown command: {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the MCP docs indexer")
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Drop into interactive search mode after tests",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="mcp_docs_test_") as tmp:
        docs_dir = Path(tmp)
        logger.info("Using temporary docs directory: %s", docs_dir)

        _create_sample_pdfs(docs_dir)
        _create_corrupt_pdf(docs_dir)

        logger.info("Building index...")
        index = DocIndex(str(docs_dir))

        all_passed = run_automated_tests(index)

        if args.interactive:
            run_interactive(index)

        sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
