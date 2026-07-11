"""Tests for the page-level PDF indexer (pdf_indexer.py).

PDFs are generated with pymupdf in tmp directories -- no binary
fixtures are committed.

Run:
    cd mcp-server
    uv run --with pytest pytest tests/ -q
"""

import pymupdf
import pytest

from mcp_docs_server.pdf_indexer import DocIndex


def make_pdf(path, pages: list[str]) -> None:
    """Write a PDF with one text block per page ("" = blank page)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def index(tmp_path):
    (tmp_path / "entitlements").mkdir()
    make_pdf(tmp_path / "entitlements" / "ordering_faq.pdf", [
        "How to order entitlements: open the request portal.",
        "Approval workflow: your manager approves within two days.",
        "Delegation rules: a delegate can approve on your behalf.",
        "Contact support if the request is stuck.",
    ])
    make_pdf(tmp_path / "leaver_process.pdf", [
        "Leaver process overview.",
        "",  # blank page -- numbering must survive it
        "All entitlements are revoked on the leaver's last day.",
    ])
    return DocIndex(str(tmp_path))


# -- search: page-level hits --

def test_search_returns_the_specific_page(index):
    results = index.search("delegation rules approve behalf")
    top = results[0]
    assert top["page_path"] == "entitlements/ordering_faq.pdf"
    assert top["page"] == 3
    assert top["total_pages"] == 4
    assert "delegate" in top["snippet"].lower()


def test_search_snippet_comes_from_the_hit_page(index):
    results = index.search("approval workflow manager")
    top = results[0]
    assert top["page"] == 2
    assert "manager" in top["snippet"].lower()
    assert "portal" not in top["snippet"].lower()  # that text is page 1


def test_search_pages_rank_independently(index):
    # both documents mention entitlements; hits carry their own pages
    results = index.search("entitlements", max_results=10)
    hits = {(r["page_path"], r["page"]) for r in results}
    assert ("entitlements/ordering_faq.pdf", 1) in hits
    assert ("leaver_process.pdf", 3) in hits


def test_blank_pages_keep_numbering(index):
    results = index.search("revoked last day")
    assert results[0]["page_path"] == "leaver_process.pdf"
    assert results[0]["page"] == 3  # page 2 is blank, numbering preserved


def test_search_no_match(index):
    assert "message" in index.search("zzzz qqqq")[0]


# -- read: full document (backward compatible) --

def test_read_full_document_by_default(index):
    result = index.read("entitlements/ordering_faq.pdf")
    assert result["total_pages"] == 4
    assert result["pages_returned"] == "all"
    assert "[Page 1]" in result["content"]
    assert "[Page 4]" in result["content"]


def test_read_fuzzy_path_match(index):
    # stem match without directory, as before
    result = index.read("ordering_faq.pdf")
    assert result["page_path"] == "entitlements/ordering_faq.pdf"


def test_read_unknown_path(index):
    result = index.read("nope.pdf")
    assert result["error"].startswith("Page not found")
    assert "leaver_process.pdf" in result["available_pages"]


# -- read: page selection --

def test_read_single_page(index):
    result = index.read("entitlements/ordering_faq.pdf", pages="3")
    assert result["pages_returned"] == "3"
    assert "[Page 3]" in result["content"]
    assert "[Page 1]" not in result["content"]
    assert "delegate" in result["content"].lower()


def test_read_page_range(index):
    result = index.read("entitlements/ordering_faq.pdf", pages="2-3")
    assert result["pages_returned"] == "2-3"
    assert "[Page 2]" in result["content"] and "[Page 3]" in result["content"]
    assert "[Page 4]" not in result["content"]


def test_read_range_skips_blank_pages_in_content(index):
    result = index.read("leaver_process.pdf", pages="1-3")
    assert "[Page 1]" in result["content"]
    assert "[Page 2]" not in result["content"]  # blank page has no block
    assert "[Page 3]" in result["content"]


@pytest.mark.parametrize("bad", ["0", "5", "9-12", "3-2"])
def test_read_out_of_range_errors(index, bad):
    result = index.read("entitlements/ordering_faq.pdf", pages=bad)
    assert "out of range" in result["error"]
    assert result["total_pages"] == 4


def test_read_garbage_selection_errors(index):
    result = index.read("entitlements/ordering_faq.pdf", pages="two")
    assert "Invalid pages selection" in result["error"]


# -- topic tree --

def test_topic_tree_unchanged(index):
    tree = index.get_topic_tree()
    assert tree["total_documents"] == 2
    assert "entitlements/ordering_faq.pdf" in tree["topics"]["entitlements"]
    assert "leaver_process.pdf" in tree["topics"]["general"]
