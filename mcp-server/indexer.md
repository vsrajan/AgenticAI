# How `indexer.py` Works

This document walks through every part of `indexer.py` — the module that reads PDF files, organizes them into a browsable structure, and makes them searchable. It is written for someone with no prior knowledge of BM25 or text-retrieval concepts.

---

## Table of Contents

1. [The Big Picture](#the-big-picture)
2. [Step 1 — Extracting Text from PDFs](#step-1--extracting-text-from-pdfs)
3. [Step 2 — Tokenization (Turning Text into Word Lists)](#step-2--tokenization-turning-text-into-word-lists)
4. [Step 3 — Building the In-Memory Data Structures](#step-3--building-the-in-memory-data-structures)
   - [The Documents Dictionary](#the-documents-dictionary)
   - [The Topic Tree](#the-topic-tree)
   - [The BM25 Search Index](#the-bm25-search-index)
5. [What Is BM25Okapi?](#what-is-bm25okapi)
   - [The Intuition](#the-intuition)
   - [TF-IDF in 60 Seconds](#tf-idf-in-60-seconds)
   - [How BM25 Improves on TF-IDF](#how-bm25-improves-on-tf-idf)
   - [What "Okapi" Means](#what-okapi-means)
6. [How the Three MCP Tools Use the Index](#how-the-three-mcp-tools-use-the-index)
   - [`list_topics()` — Browse the Topic Tree](#list_topics--browse-the-topic-tree)
   - [`search_docs(query)` — BM25 Search](#search_docsquery--bm25-search)
   - [`read_page(page_path)` — Full-Text Retrieval](#read_pagepage_path--full-text-retrieval)
7. [The Snippet Extractor](#the-snippet-extractor)
8. [End-to-End Walkthrough](#end-to-end-walkthrough)
9. [Key Data Structures at a Glance](#key-data-structures-at-a-glance)

---

## The Big Picture

When the MCP server starts, `indexer.py` does the following **once**, at startup:

```
docs/ directory on disk
        │
        ▼
  ┌─────────────┐     ┌──────────────────┐     ┌───────────────┐
  │  Read every  │────►│  Extract text    │────►│  Store text   │
  │  .pdf file   │     │  from each PDF   │     │  in a dict    │
  └─────────────┘     └──────────────────┘     └───────┬───────┘
                                                       │
                              ┌─────────────────────────┤
                              │                         │
                              ▼                         ▼
                     ┌─────────────────┐     ┌───────────────────┐
                     │  Build topic    │     │  Tokenize text &  │
                     │  tree from      │     │  build BM25 index │
                     │  folder names   │     │  for search       │
                     └─────────────────┘     └───────────────────┘
```

After startup, everything lives in memory. There is no database, no disk-based index. The three MCP tools (`list_topics`, `search_docs`, `read_page`) simply query these in-memory data structures and return results.

---

## Step 1 — Extracting Text from PDFs

```python
def _extract_text(pdf_path: str) -> tuple[str, int]:
    doc = pymupdf.open(pdf_path)
    pages: list[str] = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            # page.number is 0-based; display as 1-based
            pages.append(f"[Page {page.number + 1}]\n{text.strip()}")
    total_pages = len(doc)
    doc.close()
    return "\n\n".join(pages), total_pages
```

PDFs are not plain text files — they are a complex binary format that describes where to draw shapes, images, and individual characters on a page. You cannot just "read" a PDF like a `.txt` file.

**PyMuPDF** (imported as `pymupdf`) is a library that understands the PDF format and can extract the text content from each page. Here is what happens:

1. `pymupdf.open(pdf_path)` opens the PDF and returns a document object.
2. We loop over every page in the document.
3. `page.get_text("text")` extracts the text content of that page as a plain string. The `"text"` argument means "give me just the text, no formatting or layout information."
4. We skip empty pages (`if text.strip()`).
5. Each page's text is prefixed with a **`[Page N]` marker** (1-based) so that downstream LLM agents can cite the exact PDF page when answering questions.
6. We join all pages with double newlines to produce one big string per PDF.
7. We also return the **total page count** of the PDF (including any blank pages), which is included in search and read responses as metadata.

**Example:** A 3-page PDF about entitlements produces a tuple like:

```python
(
    "[Page 1]\nPage 1 content here...\n\n[Page 2]\nPage 2 content here...\n\n[Page 3]\nPage 3 content here...",
    3
)
```

The `[Page N]` markers remain in the stored text and flow through to `search_docs` snippets and `read_page` content, enabling the agent to cite sources precisely (e.g. *"Source: ordering_faq.pdf, Page 3"*).

---

## Step 2 — Tokenization (Turning Text into Word Lists)

```python
def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())
```

Before we can search text, we need to break it into individual words (called **tokens**). This function:

1. **Lowercases** everything — so "Entitlement", "entitlement", and "ENTITLEMENT" are all treated as the same word.
2. **Extracts words** using the regex `\w+`, which matches sequences of letters, digits, and underscores. This naturally strips out punctuation, commas, periods, etc.

**Example:**

```
Input:  "How to order Entitlements (FAQ)"
Output: ["how", "to", "order", "entitlements", "faq"]
```

This is deliberately simple. Production search engines use more sophisticated tokenizers (stemming, stop-word removal, etc.), but this works well enough for our documentation use case.

---

## Step 3 — Building the In-Memory Data Structures

When `DocIndex.__init__` is called, it immediately calls `_build_index()`, which scans the `docs/` folder and populates three data structures.

### The Documents Dictionary

```python
self._documents: dict[str, str] = {}    # path -> extracted text (with [Page N] markers)
self._page_counts: dict[str, int] = {}  # path -> total PDF pages
```

`_documents` is a dictionary mapping each PDF's **relative path** to its **full extracted text** (which includes `[Page N]` markers). `_page_counts` maps each path to the total number of pages in the PDF, used as metadata in tool responses.

**Example contents:**

```python
# _documents
{
    "entitlements/ordering_faq.pdf": "[Page 1]\nHow to order entitlements...\n\n[Page 2]\n...",
    "entitlements/bulk_requests.pdf": "[Page 1]\nBulk entitlement requests...",
    "delegations/setup_guide.pdf":   "[Page 1]\nSetting up delegations...\n\n[Page 2]\n...",
    "jml/leaver_process.pdf":        "[Page 1]\nWhen an employee leaves...",
    "general_overview.pdf":          "[Page 1]\nThis application provides...",
}

# _page_counts
{
    "entitlements/ordering_faq.pdf": 5,
    "entitlements/bulk_requests.pdf": 2,
    "delegations/setup_guide.pdf": 8,
    "jml/leaver_process.pdf": 3,
    "general_overview.pdf": 1,
}
```

`_documents` is the **primary store** of all document content (with embedded page markers). Every other data structure references back to the keys in this dictionary. `_page_counts` provides the total page count for each PDF, included in API responses so agents know how large each document is.

### The Topic Tree

```python
self._topic_tree: dict[str, list[str]] = {}  # topic -> [filenames]
```

Documents are grouped into **topics** based on the folder they live in:

```python
parts = Path(rel_path).parts
topic = parts[0] if len(parts) > 1 else "general"
self._topic_tree.setdefault(topic, []).append(rel_path)
```

The logic:

- If a file is inside a subdirectory (e.g., `entitlements/ordering_faq.pdf`), the **first directory name** becomes the topic (`"entitlements"`).
- If a file is at the root of `docs/` (e.g., `general_overview.pdf`), it goes under the topic `"general"`.

**Example contents:**

```python
{
    "entitlements": [
        "entitlements/ordering_faq.pdf",
        "entitlements/bulk_requests.pdf",
    ],
    "delegations": [
        "delegations/setup_guide.pdf",
    ],
    "jml": [
        "jml/leaver_process.pdf",
    ],
    "general": [
        "general_overview.pdf",
    ],
}
```

This gives the agent a way to **browse** available documentation without searching — it can call `list_topics()` to see what subjects are covered and which documents exist for each.

### The BM25 Search Index

```python
self._doc_keys = list(self._documents.keys())
corpus = [_tokenize(self._documents[k]) for k in self._doc_keys]
self._bm25 = BM25Okapi(corpus)
```

This is the search engine. Three things happen:

1. **`_doc_keys`** — We fix the order of documents in a list. This is important because BM25 returns scores by position (index 0, index 1, etc.), so we need to know which document corresponds to which position.

2. **`corpus`** — We tokenize every document's text into a list of word lists. If we have 5 documents, `corpus` is a list of 5 word lists.

3. **`BM25Okapi(corpus)`** — We hand the corpus to the BM25Okapi algorithm. It analyzes word frequencies across all documents and builds its internal index. After this, we can pass it a tokenized query and it will score every document for relevance.

**Example of what `corpus` looks like:**

```python
[
    ["how", "to", "order", "entitlements", "you", "can", ...],   # doc 0
    ["bulk", "entitlement", "requests", "are", "handled", ...],  # doc 1
    ["setting", "up", "delegations", "first", "navigate", ...],  # doc 2
    ...
]
```

---

## What Is BM25Okapi?

### The Intuition

Imagine you have 100 documents and someone searches for **"delegation setup"**. You want to rank the documents from most relevant to least relevant. But how do you decide that Document A is more relevant than Document B?

The simplest approach would be: count how many times "delegation" and "setup" appear in each document, and rank by count. But this has problems:

- A 200-page document will naturally contain more word occurrences than a 2-page document, even if the 2-page document is entirely about delegations.
- The word "the" appears in every document but tells you nothing about relevance. Rare words like "delegation" should matter more.

BM25 solves both of these problems.

### TF-IDF in 60 Seconds

BM25 is built on an older concept called **TF-IDF**. Understanding TF-IDF first makes BM25 easy to grasp:

- **TF (Term Frequency):** How often does the search word appear in *this* document? More occurrences = probably more relevant.
- **IDF (Inverse Document Frequency):** How rare is this word across *all* documents? If "delegation" appears in only 3 out of 100 documents, it's a strong signal. If "the" appears in 100 out of 100 documents, it's worthless for ranking.

TF-IDF multiplies these two numbers together:

```
score = TF × IDF
```

A word that appears **often in this document** (high TF) and **rarely in other documents** (high IDF) gets the highest score. This is why searching "delegation setup" would rank a delegation-focused document highly — "delegation" has high IDF (it's a rare, specific word) and high TF in that document.

### How BM25 Improves on TF-IDF

Plain TF-IDF has a flaw: if "delegation" appears 100 times in a document vs. 50 times, TF-IDF gives the first document double the score. But realistically, after a certain point, more occurrences don't make a document *that* much more relevant. BM25 fixes this with **saturation**:

```
           TF × (k₁ + 1)
BM25_TF = ─────────────────────────────────────
           TF + k₁ × (1 - b + b × docLen/avgDocLen)
```

Don't worry about memorizing this formula. The key ideas are:

1. **Saturation (the `k₁` parameter):** As TF grows, the score increases but eventually flattens out. Going from 1 occurrence to 5 matters a lot. Going from 50 to 100 barely matters. This prevents long, repetitive documents from dominating.

2. **Document length normalization (the `b` parameter):** Longer documents are penalized slightly. The term `docLen/avgDocLen` compares each document's length to the average. This way, a short, focused document about delegations can rank higher than a long, general document that happens to mention delegations a few times.

The final BM25 score for a query is the **sum of BM25 scores for each query word**:

```
score("delegation setup", document) = BM25("delegation", document) + BM25("setup", document)
```

### What "Okapi" Means

"Okapi" is the name of the information retrieval system at City University London where BM25 was first implemented in the 1990s. The `BM25Okapi` class in the `rank-bm25` Python library is a direct implementation of this classic algorithm with sensible default parameters (`k₁=1.5`, `b=0.75`).

---

## How the Three MCP Tools Use the Index

### `list_topics()` — Browse the Topic Tree

```python
def get_topic_tree(self) -> dict:
    return {
        "topics": {
            topic: sorted(pages) for topic, pages in sorted(self._topic_tree.items())
        },
        "total_documents": len(self._documents),
    }
```

This simply returns the `_topic_tree` dictionary in a sorted, readable format. No search is involved — it's a directory listing.

**Example return value:**

```json
{
    "topics": {
        "delegations": ["delegations/setup_guide.pdf"],
        "entitlements": ["entitlements/bulk_requests.pdf", "entitlements/ordering_faq.pdf"],
        "general": ["general_overview.pdf"],
        "jml": ["jml/leaver_process.pdf"]
    },
    "total_documents": 4
}
```

The agent uses this to understand **what documentation exists** before deciding what to search for or read.

### `search_docs(query)` — BM25 Search

```python
def search(self, query: str, max_results: int = 5) -> list[dict]:
    tokens = _tokenize(query)                    # 1. Tokenize the query
    scores = self._bm25.get_scores(tokens)       # 2. Score every document
    scored = sorted(zip(scores, self._doc_keys),  # 3. Sort by score
                    reverse=True)
    # 4. Return top results with snippets
```

Step by step:

1. **Tokenize the query** — `"how to set up delegations"` becomes `["how", "to", "set", "up", "delegations"]`.

2. **Score every document** — `get_scores()` returns an array of floats, one per document, in the same order as `_doc_keys`. A higher score means more relevant.

   ```
   Example: [0.0, 0.0, 8.42, 0.31, 0.0]
   ```

   Here, document at index 2 scored highest (probably the delegations setup guide).

3. **Sort by score** — We pair each score with its document key, then sort descending so the best match comes first.

4. **Build results** — For each of the top results (up to `max_results`), we create a dictionary with the page path, relevance score, and a snippet (see [The Snippet Extractor](#the-snippet-extractor)). Documents with score <= 0 are excluded (they had no matching terms at all).

**Example return value:**

```json
[
    {
        "page_path": "delegations/setup_guide.pdf",
        "total_pages": 8,
        "score": 8.42,
        "snippet": "...[Page 2]\nTo set up delegations, navigate to the admin panel and select..."
    },
    {
        "page_path": "general_overview.pdf",
        "total_pages": 1,
        "score": 0.31,
        "snippet": "...[Page 1]\nthe application supports entitlements, delegations, and JML..."
    }
]
```

Each result includes `total_pages` (the total number of pages in the source PDF) and snippets may contain `[Page N]` markers so the agent knows exactly which page the matching text came from.

### `read_page(page_path)` — Full-Text Retrieval

```python
def read(self, page_path: str) -> dict:
    if page_path in self._documents:
        return {
            "page_path": page_path,
            "total_pages": self._page_counts[page_path],
            "content": self._documents[page_path],
        }
```

This is a simple dictionary lookup — given a path like `"delegations/setup_guide.pdf"`, it returns the complete extracted text of that document (with `[Page N]` markers throughout) and the total page count.

It also includes a **fuzzy match fallback**: if the exact path isn't found, it checks whether any stored key ends with the given path or has the same filename stem. This is forgiving if the agent passes just `"setup_guide"` instead of `"delegations/setup_guide.pdf"`.

---

## The Snippet Extractor

When `search_docs` returns results, each result includes a short **snippet** — a ~300-character excerpt from the document that shows the most relevant part. This helps the agent (and ultimately the user) judge whether the document is worth reading in full.

```python
def _make_snippet(text: str, query_tokens: list[str], max_length: int = 300) -> str:
```

The algorithm uses a **sliding window** approach:

1. Define a window size of 300 characters.
2. Slide the window across the document text in steps of 75 characters (300 / 4).
3. For each window position, count how many of the query tokens appear in that chunk.
4. The window position with the **highest density** of query terms wins.
5. Extract the text at that position and add `"..."` at the start/end if the snippet doesn't cover the full document.

**Visual example** for query `"delegation setup"` on a 1000-character document:

```
Document text:
[--------|--------|--------|--------|--------|--------|--------|--------]
 0       75      150      225      300      375      450      525

Window positions checked:
[========300 chars========]                                              → 0 matches
         [========300 chars========]                                     → 1 match ("delegation")
                  [========300 chars========]                            → 2 matches ("delegation" + "setup")  ← WINNER
                           [========300 chars========]                   → 1 match ("setup")
                                    ...
```

The winning 300-character chunk becomes the snippet.

---

## End-to-End Walkthrough

Let's trace through a complete example from server startup to an agent query.

**1. Server starts.** The `docs/` directory looks like this:

```
docs/
├── entitlements/
│   └── ordering_faq.pdf      (contains "How to order entitlements...")
├── delegations/
│   └── setup_guide.pdf        (contains "Setting up delegations...")
└── overview.pdf               (contains "This application provides...")
```

**2. `DocIndex("docs/")` is created.** `_build_index()` runs:

- Finds 3 PDF files via `rglob("*.pdf")`.
- Calls `_extract_text()` on each, producing 3 `(text, total_pages)` tuples. The text includes `[Page N]` markers.
- Stores the text in `_documents` and the page counts in `_page_counts`:
  ```python
  # _documents
  {
      "delegations/setup_guide.pdf": "[Page 1]\nSetting up delegations...",
      "entitlements/ordering_faq.pdf": "[Page 1]\nHow to order entitlements...",
      "overview.pdf": "[Page 1]\nThis application provides...",
  }
  # _page_counts
  {
      "delegations/setup_guide.pdf": 4,
      "entitlements/ordering_faq.pdf": 3,
      "overview.pdf": 1,
  }
  ```
- Builds `_topic_tree`:
  ```python
  {
      "delegations": ["delegations/setup_guide.pdf"],
      "entitlements": ["entitlements/ordering_faq.pdf"],
      "general": ["overview.pdf"],
  }
  ```
- Tokenizes all 3 documents and creates the `BM25Okapi` index.

**3. Agent calls `list_topics()`.** Gets back the topic tree. Sees there are topics for delegations, entitlements, and general.

**4. Agent calls `search_docs("how do I set up a delegation?")`.**

- Query is tokenized: `["how", "do", "i", "set", "up", "a", "delegation"]`.
- BM25 scores all 3 documents. The delegations guide scores highest because it contains "delegation" (rare word = high IDF) and "set up" frequently.
- Returns the top result with a snippet.

**5. Agent calls `read_page("delegations/setup_guide.pdf")`.** Gets the full text of the delegations guide. Uses this to answer the user's question.

---

## Key Data Structures at a Glance

| Attribute | Type | What It Holds | Used By |
|---|---|---|---|
| `_documents` | `dict[str, str]` | `"relative/path.pdf"` → full extracted text with `[Page N]` markers | `read_page`, `search_docs` (for snippets) |
| `_page_counts` | `dict[str, int]` | `"relative/path.pdf"` → total number of PDF pages | `read_page`, `search_docs` (returned as metadata) |
| `_topic_tree` | `dict[str, list[str]]` | `"topic_name"` → list of document paths | `list_topics` |
| `_bm25` | `BM25Okapi` | Internal term-frequency index over all documents | `search_docs` |
| `_doc_keys` | `list[str]` | Ordered list of document paths (matches BM25 corpus order) | `search_docs` (to map BM25 scores back to document paths) |

Everything is built once at startup and then queried in-memory for the lifetime of the server process. Adding new PDFs requires restarting the server so the index is rebuilt.
