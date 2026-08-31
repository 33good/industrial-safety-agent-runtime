"""Versioned, auditable SOP retrieval for the safety Agent."""
from __future__ import annotations

from collections import Counter
import json
import math
import re
from pathlib import Path


def _tokens(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    latin = re.findall(r"[a-z0-9][a-z0-9_.-]*", normalized)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    grams = [chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))]
    return latin + grams


class SOPRetriever:
    """Small local retriever with explicit versions and citation identifiers.

    The catalog is intentionally deterministic and dependency-free. Retrieval
    supplies evidence to the model; it never grants tool permissions or changes
    the rule baseline by itself.
    """

    def __init__(self, catalog_path: str | Path, top_k: int = 3, min_score: float = 3.0):
        self.catalog_path = Path(catalog_path)
        self.top_k = max(1, int(top_k))
        self.min_score = max(0.0, float(min_score))
        self.catalog_version = ""
        self.documents = self._load_catalog()
        self._document_frequency = self._build_document_frequency()

    def _load_catalog(self) -> list[dict]:
        payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.catalog_version = str(payload.get("catalog_version") or "unversioned")
        raw_documents = payload.get("documents") or []
        if not isinstance(raw_documents, list):
            raise ValueError("SOP catalog documents must be a list")

        latest: dict[tuple[str, str], dict] = {}
        citation_ids: set[str] = set()
        for raw in raw_documents:
            if not isinstance(raw, dict) or not raw.get("active", True):
                continue
            item = dict(raw)
            document_id = str(item.get("document_id") or "").strip()
            section = str(item.get("section") or "").strip()
            version = str(item.get("version") or "").strip()
            content = str(item.get("content") or "").strip()
            if not all((document_id, section, version, content)):
                raise ValueError("every active SOP section needs document_id, section, version and content")
            citation_id = f"{document_id}#{section}@{version}"
            if citation_id in citation_ids:
                raise ValueError(f"duplicate SOP citation: {citation_id}")
            citation_ids.add(citation_id)
            item["citation_id"] = citation_id
            item["event_types"] = [str(v).strip() for v in item.get("event_types", []) if str(v).strip()]
            item["risk_levels"] = [str(v).upper().strip() for v in item.get("risk_levels", [])]
            item["keywords"] = [str(v).lower().strip() for v in item.get("keywords", []) if str(v).strip()]
            item["_tokens"] = Counter(_tokens(" ".join([
                str(item.get("title") or ""), content, *item["event_types"], *item["keywords"]
            ])))
            key = (document_id, section)
            previous = latest.get(key)
            if previous is None or self._version_key(version) > self._version_key(previous["version"]):
                latest[key] = item
        return sorted(latest.values(), key=lambda item: item["citation_id"])

    @staticmethod
    def _version_key(value: str) -> tuple:
        parts = re.findall(r"\d+|[a-z]+", value.lower())
        return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)

    def _build_document_frequency(self) -> Counter:
        frequency: Counter = Counter()
        for document in self.documents:
            frequency.update(document["_tokens"].keys())
        return frequency

    def retrieve_event(self, event) -> dict:
        events = list(getattr(event, "events", []) or [])
        event_types = [str(item.get("type") or "").strip() for item in events]
        levels = [str(item.get("level") or "B").upper() for item in events]
        query = " ".join(
            value for item in events
            for value in (str(item.get("type") or ""), str(item.get("detail") or ""))
            if value
        )
        return self.retrieve(query=query, event_types=event_types, risk_levels=levels)

    def retrieve(self, query: str, event_types: list[str] | None = None,
                 risk_levels: list[str] | None = None) -> dict:
        event_types = [str(value).strip() for value in (event_types or []) if str(value).strip()]
        risk_levels = [str(value).upper().strip() for value in (risk_levels or [])]
        query_lower = str(query or "").lower()
        query_counts = Counter(_tokens(query_lower))
        results = []
        for document in self.documents:
            exact_event_matches = sorted(set(event_types).intersection(document["event_types"]))
            keyword_matches = sorted({word for word in document["keywords"] if word and word in query_lower})
            level_matches = sorted(set(risk_levels).intersection(document["risk_levels"]))
            lexical = self._cosine_tfidf(query_counts, document["_tokens"])
            score = 8.0 * len(exact_event_matches) + 3.0 * len(keyword_matches) + lexical
            if level_matches:
                score += 0.25
            if score < self.min_score:
                continue
            results.append({
                "citation_id": document["citation_id"],
                "document_id": document["document_id"],
                "title": str(document.get("title") or ""),
                "section": document["section"],
                "version": document["version"],
                "source": str(document.get("source") or self.catalog_path.name),
                "effective_date": str(document.get("effective_date") or ""),
                "excerpt": str(document["content"])[:280],
                "score": round(score, 4),
                "matched_event_types": exact_event_matches,
                "matched_keywords": keyword_matches,
                "risk_levels": list(document["risk_levels"]),
            })
        results.sort(key=lambda item: (-item["score"], item["citation_id"]))
        citations = results[:self.top_k]
        return {
            "status": "retrieved" if citations else "no_evidence",
            "catalog_version": self.catalog_version,
            "query": query,
            "citations": citations,
            "refusal_reason": "" if citations else "未检索到达到阈值的适用规程，不提供SOP依据",
        }

    def _cosine_tfidf(self, query: Counter, document: Counter) -> float:
        if not query or not document or not self.documents:
            return 0.0
        total = len(self.documents)
        common = set(query).intersection(document)
        numerator = 0.0
        query_norm = 0.0
        document_norm = 0.0
        for token, count in query.items():
            weight = math.log((total + 1) / (self._document_frequency[token] + 1)) + 1
            query_norm += (count * weight) ** 2
        for token, count in document.items():
            weight = math.log((total + 1) / (self._document_frequency[token] + 1)) + 1
            document_norm += (count * weight) ** 2
        for token in common:
            weight = math.log((total + 1) / (self._document_frequency[token] + 1)) + 1
            numerator += query[token] * document[token] * weight * weight
        denominator = math.sqrt(query_norm) * math.sqrt(document_norm)
        return 2.5 * numerator / denominator if denominator else 0.0
