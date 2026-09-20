"""Deterministic document-search tool over a tiny in-memory corpus."""

from __future__ import annotations

from ..classification import MissingContext
from ..schemas import DocHit, DocSearchInput, DocSearchOutput
from .base import Tool

# A fixed corpus so results are reproducible offline. Real deployments would
# back this with a vector store or search index.
_CORPUS: list[dict[str, str]] = [
    {
        "doc_id": "runbook-01",
        "title": "Retry & backoff runbook",
        "text": "transient failures should use exponential backoff with a bounded "
        "cap and jitter before falling back to a cached response.",
    },
    {
        "doc_id": "runbook-02",
        "title": "Timeout handling",
        "text": "a hanging upstream must be bounded by a deadline; on timeout the "
        "harness classifies the failure and degrades to a secondary path.",
    },
    {
        "doc_id": "policy-07",
        "title": "Unsafe action policy",
        "text": "destructive actions such as delete are blocked by the safety gate "
        "unless explicitly allow-listed for the resource.",
    },
    {
        "doc_id": "note-42",
        "title": "Unit conversions",
        "text": "miles to kilometers, celsius to fahrenheit and pounds to kilograms "
        "are supported by the calculator tool.",
    },
]


class DocSearchTool(Tool[DocSearchInput, DocSearchOutput]):
    name = "doc_search"
    input_model = DocSearchInput
    output_model = DocSearchOutput
    latency_ms = 8.0
    # A hosted search index bills per query. Priced so the run ledger in the
    # trace is not all zeroes.
    cost_usd = 0.0002

    def __init__(self, corpus: list[dict[str, str]] | None = None) -> None:
        # Passing an empty corpus lets us exercise the missing-context path.
        self._corpus = _CORPUS if corpus is None else corpus

    def run(self, request: DocSearchInput, attempt: int = 0) -> DocSearchOutput:
        if not self._corpus:
            raise MissingContext(
                "doc_search invoked with no corpus loaded",
                detail="the search index is empty; cannot answer the query",
            )
        terms = {t for t in request.query.lower().split() if len(t) > 2}
        scored: list[tuple[float, dict[str, str]]] = []
        for doc in self._corpus:
            haystack = (doc["title"] + " " + doc["text"]).lower()
            hits = sum(1 for t in terms if t in haystack)
            if hits:
                score = min(1.0, hits / max(len(terms), 1))
                scored.append((score, doc))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["doc_id"]))
        results = [
            DocHit(
                doc_id=doc["doc_id"],
                title=doc["title"],
                snippet=doc["text"][:120],
                score=round(score, 4),
            )
            for score, doc in scored[: request.top_k]
        ]
        return DocSearchOutput(results=results)
