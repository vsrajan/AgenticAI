"""PDF document indexer with BM25 search support."""

import os
import re
from pathlib import Path

import pymupdf
from rank_bm25 import BM25Okapi


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer with lowercasing."""
    return re.findall(r"\w+", text.lower())


def _extract_text(pdf_path: str) -> str:
    """Extract all text from a PDF file using PyMuPDF."""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            pages.append(text.strip())
    doc.close()
    return "\n\n".join(pages)


class DocIndex:
    """Indexes PDF documents in a directory and provides search/retrieval."""

    def __init__(self, docs_dir: str) -> None:
        self.docs_dir = Path(docs_dir)
        self._documents: dict[str, str] = {}  # path -> extracted text
        self._topic_tree: dict[str, list[str]] = {}  # topic -> [filenames]
        self._bm25: BM25Okapi | None = None
        self._doc_keys: list[str] = []  # ordered keys matching BM25 corpus
        self._build_index()

    def _build_index(self) -> None:
        """Scan the docs directory, extract text, and build the BM25 index."""
        if not self.docs_dir.exists():
            return

        for pdf_path in sorted(self.docs_dir.rglob("*.pdf")):
            rel_path = str(pdf_path.relative_to(self.docs_dir))
            text = _extract_text(str(pdf_path))
            if not text:
                continue

            self._documents[rel_path] = text

            # Build topic tree from directory structure.
            # Files in subdirectories are grouped by directory name.
            # Files at the root level go under "general".
            parts = Path(rel_path).parts
            topic = parts[0] if len(parts) > 1 else "general"
            self._topic_tree.setdefault(topic, []).append(rel_path)

        # Build BM25 index
        self._doc_keys = list(self._documents.keys())
        if self._doc_keys:
            corpus = [_tokenize(self._documents[k]) for k in self._doc_keys]
            self._bm25 = BM25Okapi(corpus)

    def get_topic_tree(self) -> dict:
        """Return the topic tree: topics mapped to their page paths."""
        if not self._topic_tree:
            return {"message": "No documents indexed. Add PDF files to the docs/ directory."}
        return {
            "topics": {
                topic: sorted(pages) for topic, pages in sorted(self._topic_tree.items())
            },
            "total_documents": len(self._documents),
        }

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search documents using BM25 ranking. Returns snippets with paths."""
        if not self._bm25 or not self._doc_keys:
            return [{"message": "No documents indexed."}]

        tokens = _tokenize(query)
        if not tokens:
            return [{"message": "Empty query."}]

        scores = self._bm25.get_scores(tokens)

        # Pair scores with doc keys and sort descending
        scored = sorted(zip(scores, self._doc_keys), reverse=True)

        results = []
        for score, key in scored[:max_results]:
            if score <= 0:
                break
            text = self._documents[key]
            snippet = _make_snippet(text, tokens)
            results.append({
                "page_path": key,
                "score": round(float(score), 3),
                "snippet": snippet,
            })

        if not results:
            return [{"message": "No matching documents found.", "query": query}]
        return results

    def read(self, page_path: str) -> dict:
        """Read the full text content of a document by its path."""
        if page_path in self._documents:
            return {
                "page_path": page_path,
                "content": self._documents[page_path],
            }

        # Try fuzzy match — user might omit directory or extension
        for key in self._documents:
            if key.endswith(page_path) or Path(key).stem == Path(page_path).stem:
                return {
                    "page_path": key,
                    "content": self._documents[key],
                }

        available = list(self._documents.keys())
        return {
            "error": f"Page not found: {page_path}",
            "available_pages": available,
        }


def _make_snippet(text: str, query_tokens: list[str], max_length: int = 300) -> str:
    """Extract a relevant snippet from the document around matching terms."""
    text_lower = text.lower()
    best_pos = 0
    best_density = 0

    # Slide a window and find the region with the most query term hits
    window = max_length
    for i in range(0, len(text_lower) - window, window // 4):
        chunk = text_lower[i : i + window]
        density = sum(1 for t in query_tokens if t in chunk)
        if density > best_density:
            best_density = density
            best_pos = i

    start = max(0, best_pos)
    snippet = text[start : start + max_length].strip()

    # Clean up snippet boundaries
    if start > 0:
        snippet = "..." + snippet
    if start + max_length < len(text):
        snippet = snippet + "..."

    return snippet
