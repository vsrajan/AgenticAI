"""PDF document indexer with page-level BM25 search.

Indexing and retrieval work at PAGE granularity (P1.1):

- the BM25 corpus holds one entry per non-empty PDF page, so search
  hits point at the specific page that matched, and ranking precision
  grows with the corpus instead of degrading (pages compete with
  pages, not 60-page handbooks against 2-page FAQs)
- read() accepts an optional page selection ("3" or "2-5") and returns
  only those pages -- tool results shrink by roughly document-size /
  pages-needed, which is what cuts prompt tokens per knowledgebase turn
- called without a selection, read() returns the whole document, so
  existing callers keep working unchanged
"""

import logging
import re
from pathlib import Path

import pymupdf
from rank_bm25 import BM25Okapi

logger = logging.getLogger("mcp_docs_server.pdf_indexer")


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer with lowercasing."""
    return re.findall(r"\w+", text.lower())


def _extract_pages(pdf_path: str) -> list[str]:
    """Extract text per page using PyMuPDF.

    Index i holds page i+1's text. Blank pages are kept as "" so page
    NUMBERING is preserved -- page 3 stays page 3 even when page 2 is
    blank -- they just do not enter the search index.
    """
    doc = pymupdf.open(pdf_path)
    pages = [page.get_text("text").strip() for page in doc]
    doc.close()
    return pages


class DocIndex:
    """Indexes PDF documents in a directory; page-level search/retrieval."""

    def __init__(self, docs_dir: str) -> None:
        self.docs_dir = Path(docs_dir)
        self._pages: dict[str, list[str]] = {}  # path -> per-page text
        self._topic_tree: dict[str, list[str]] = {}  # topic -> [filenames]
        self._bm25: BM25Okapi | None = None
        self._index_keys: list[tuple[str, int]] = []  # (path, 1-based page)
        self._build_index()

    # kept for compatibility with the startup log in server.py
    @property
    def _documents(self) -> dict[str, list[str]]:
        return self._pages

    def _build_index(self) -> None:
        """Scan the docs directory, extract pages, build the BM25 index."""
        if not self.docs_dir.exists():
            logger.warning("Docs directory does not exist: %s", self.docs_dir)
            return

        logger.info("Scanning for PDFs in %s", self.docs_dir)

        for pdf_path in sorted(self.docs_dir.rglob("*.pdf")):
            rel_path = str(pdf_path.relative_to(self.docs_dir))
            try:
                pages = _extract_pages(str(pdf_path))
            except Exception:
                logger.exception("Failed to read %s, skipping", rel_path)
                continue
            if not any(pages):
                logger.warning("No text extracted from %s, skipping", rel_path)
                continue

            self._pages[rel_path] = pages
            logger.debug("Indexed %s (%d pages)", rel_path, len(pages))

            # topic tree from directory structure; root files -> "general"
            parts = Path(rel_path).parts
            topic = parts[0] if len(parts) > 1 else "general"
            self._topic_tree.setdefault(topic, []).append(rel_path)

        # one BM25 entry per non-empty page
        corpus: list[list[str]] = []
        keys: list[tuple[str, int]] = []
        for path, pages in self._pages.items():
            for page_no, text in enumerate(pages, start=1):
                if text:
                    keys.append((path, page_no))
                    corpus.append(_tokenize(text))
        self._index_keys = keys
        if corpus:
            self._bm25 = BM25Okapi(corpus)
            logger.info(
                "BM25 index built: %d documents, %d pages, %d topics",
                len(self._pages), len(corpus), len(self._topic_tree),
            )
        else:
            logger.warning("No documents found to index")

    def get_topic_tree(self) -> dict:
        """Return the topic tree: topics mapped to their page paths."""
        if not self._topic_tree:
            return {"message": "No documents indexed. Add PDF files to the docs/ directory."}
        return {
            "topics": {
                topic: sorted(pages) for topic, pages in sorted(self._topic_tree.items())
            },
            "total_documents": len(self._pages),
        }

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Page-level BM25 search.

        Each result names the specific PAGE that matched, with a
        snippet taken from that page -- pass page_path and the page
        number to read(). Multiple pages of one document can appear as
        separate results.
        """
        if not self._bm25 or not self._index_keys:
            return [{"message": "No documents indexed."}]

        tokens = _tokenize(query)
        if not tokens:
            return [{"message": "Empty query."}]

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(zip(scores, self._index_keys), reverse=True)

        results = []
        for score, (path, page_no) in ranked[:max_results]:
            if score <= 0:
                break
            page_text = self._pages[path][page_no - 1]
            results.append({
                "page_path": path,
                "page": page_no,
                "total_pages": len(self._pages[path]),
                "score": round(float(score), 3),
                "snippet": _make_snippet(page_text, tokens),
            })

        if not results:
            return [{"message": "No matching documents found.", "query": query}]
        return results

    def _resolve_path(self, page_path: str) -> str | None:
        """Exact match, then fuzzy (suffix / stem) like before."""
        if page_path in self._pages:
            return page_path
        for key in self._pages:
            if key.endswith(page_path) or Path(key).stem == Path(page_path).stem:
                return key
        return None

    def read(self, page_path: str, pages: str = "") -> dict:
        """Read a document -- whole, or just a page selection.

        pages selects what to return: "3" for one page, "2-5" for a
        range, empty for the full document (backward compatible).
        Content keeps the [Page N] markers either way, so citations
        work identically.
        """
        path = self._resolve_path(page_path)
        if path is None:
            return {
                "error": f"Page not found: {page_path}",
                "available_pages": list(self._pages.keys()),
            }

        doc_pages = self._pages[path]
        total = len(doc_pages)

        if not pages.strip():
            start, end = 1, total
            returned = "all"
        else:
            match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", pages)
            if not match:
                return {
                    "error": (f"Invalid pages selection {pages!r}: use a single "
                              f"page like '3' or a range like '2-5'."),
                    "total_pages": total,
                }
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if start < 1 or end > total or start > end:
                return {
                    "error": (f"Pages {pages!r} out of range: {path} has "
                              f"pages 1-{total}."),
                    "total_pages": total,
                }
            returned = f"{start}-{end}" if end != start else str(start)

        content = "\n\n".join(
            f"[Page {page_no}]\n{doc_pages[page_no - 1]}"
            for page_no in range(start, end + 1)
            if doc_pages[page_no - 1]
        )
        return {
            "page_path": path,
            "total_pages": total,
            "pages_returned": returned,
            "content": content,
        }


def _make_snippet(text: str, query_tokens: list[str], max_length: int = 300) -> str:
    """Extract a relevant snippet from the text around matching terms."""
    text_lower = text.lower()
    best_pos = 0
    best_density = 0

    window = max_length
    for i in range(0, max(1, len(text_lower) - window), max(1, window // 4)):
        chunk = text_lower[i: i + window]
        density = sum(1 for t in query_tokens if t in chunk)
        if density > best_density:
            best_density = density
            best_pos = i

    snippet = text[best_pos: best_pos + max_length].strip()
    if best_pos > 0:
        snippet = "..." + snippet
    if best_pos + max_length < len(text):
        snippet = snippet + "..."
    return snippet
