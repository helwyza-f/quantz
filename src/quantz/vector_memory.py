from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorMemoryDocument:
    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorMemoryStore(Protocol):
    def upsert(self, documents: list[VectorMemoryDocument]) -> int:
        ...

    def search(self, query: str, limit: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        ...


class LocalHashEmbedder:
    """Tiny deterministic fallback embedder.

    This is not a replacement for sentence-transformers quality. It lets local
    vector plumbing run in tests/dev without a model download.
    """

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [round(value / norm, 8) for value in vector]


class QdrantVectorMemoryStore:
    def __init__(
        self,
        url: str = "http://127.0.0.1:6333",
        collection: str = "quantz_memory",
        embedder: Any | None = None,
        dimensions: int = 384,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams
        except ImportError as exc:
            raise RuntimeError("Install qdrant-client to use QdrantVectorMemoryStore.") from exc
        self.QdrantClient = QdrantClient
        self.Distance = Distance
        self.FieldCondition = FieldCondition
        self.Filter = Filter
        self.MatchValue = MatchValue
        self.PointStruct = PointStruct
        self.VectorParams = VectorParams
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.embedder = embedder or LocalHashEmbedder(dimensions=dimensions)
        self.dimensions = dimensions
        self._ensure_collection()

    def upsert(self, documents: list[VectorMemoryDocument]) -> int:
        points = [
            self.PointStruct(
                id=document.doc_id,
                vector=self.embedder.embed(document.text),
                payload={"text": document.text, **document.metadata},
            )
            for document in documents
        ]
        if points:
            self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, query: str, limit: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query_filter = None
        if filters:
            query_filter = self.Filter(
                must=[
                    self.FieldCondition(key=key, match=self.MatchValue(value=value))
                    for key, value in filters.items()
                ]
            )
        results = self.client.search(
            collection_name=self.collection,
            query_vector=self.embedder.embed(query),
            limit=limit,
            query_filter=query_filter,
        )
        return [
            {"score": item.score, "payload": dict(item.payload or {})}
            for item in results
        ]

    def _ensure_collection(self) -> None:
        collections = self.client.get_collections().collections
        if any(collection.name == self.collection for collection in collections):
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=self.VectorParams(size=self.dimensions, distance=self.Distance.COSINE),
        )


class LocalSQLiteVectorMemoryStore:
    """Persistent local vector store with deterministic embeddings.

    This gives Quantz a no-service vector DB for autonomous memory plumbing.
    Qdrant can still be used for production scale, but this store keeps dev,
    tests, and offline training runs self-contained.
    """

    def __init__(
        self,
        path: str | Path = "data/quantz-vector-memory.db",
        embedder: Any | None = None,
        dimensions: int = 384,
    ) -> None:
        self.path = Path(path)
        self.embedder = embedder or LocalHashEmbedder(dimensions=dimensions)
        self.dimensions = dimensions
        self._ensure_schema()

    def upsert(self, documents: list[VectorMemoryDocument]) -> int:
        if not documents:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            for document in documents:
                vector = self.embedder.embed(document.text)
                metadata = dict(document.metadata)
                created_at = str(metadata.get("timestamp") or datetime.now(timezone.utc).isoformat())
                connection.execute(
                    """
                    insert into vector_documents (
                        doc_id, text, metadata_json, vector_json, created_at, compacted
                    )
                    values (?, ?, ?, ?, ?, 0)
                    on conflict(doc_id) do update set
                        text = excluded.text,
                        metadata_json = excluded.metadata_json,
                        vector_json = excluded.vector_json,
                        created_at = excluded.created_at,
                        compacted = 0
                    """,
                    (
                        document.doc_id,
                        document.text,
                        json.dumps(metadata, sort_keys=True, default=str),
                        json.dumps(vector, separators=(",", ":")),
                        created_at,
                    ),
                )
        return len(documents)

    def search(self, query: str, limit: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query_vector = self.embedder.embed(query)
        rows = self._rows(filters=filters, include_compacted=False)
        scored: list[dict[str, Any]] = []
        for row in rows:
            vector = json.loads(str(row["vector_json"]))
            score = self._cosine(query_vector, vector)
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            scored.append(
                {
                    "score": score,
                    "payload": {
                        "doc_id": row["doc_id"],
                        "text": row["text"],
                        **metadata,
                    },
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def count(self, filters: dict[str, Any] | None = None, include_compacted: bool = False) -> int:
        return len(self._rows(filters=filters, include_compacted=include_compacted))

    def compact(
        self,
        filters: dict[str, Any] | None = None,
        max_documents: int = 500,
        target_documents: int = 300,
    ) -> dict[str, Any]:
        """Summarize old matching documents when memory grows past budget."""
        active_rows = self._rows(filters=filters, include_compacted=False)
        source_rows = [
            row
            for row in active_rows
            if json.loads(str(row["metadata_json"] or "{}")).get("kind") != "context_summary"
        ]
        if len(source_rows) <= max_documents:
            return {"compacted": 0, "active_documents": len(active_rows), "summary_doc_id": None}

        compact_count = max(0, len(source_rows) - max(target_documents, 1))
        candidates = sorted(source_rows, key=lambda row: str(row["created_at"]))[:compact_count]
        if not candidates:
            return {"compacted": 0, "active_documents": len(active_rows), "summary_doc_id": None}

        summary = self._summary_document(candidates, filters or {})
        with self._connect() as connection:
            connection.executemany(
                "update vector_documents set compacted = 1 where doc_id = ?",
                [(row["doc_id"],) for row in candidates],
            )
        self.upsert([summary])
        return {
            "compacted": len(candidates),
            "active_documents": self.count(filters=filters),
            "summary_doc_id": summary.doc_id,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists vector_documents (
                    doc_id text primary key,
                    text text not null,
                    metadata_json text not null,
                    vector_json text not null,
                    created_at text not null,
                    compacted integer not null default 0
                )
                """
            )
            connection.execute(
                "create index if not exists idx_vector_documents_created_at on vector_documents(created_at)"
            )
            connection.execute(
                "create index if not exists idx_vector_documents_compacted on vector_documents(compacted)"
            )

    def _rows(self, filters: dict[str, Any] | None, include_compacted: bool) -> list[sqlite3.Row]:
        with self._connect() as connection:
            query = "select * from vector_documents"
            params: list[Any] = []
            if not include_compacted:
                query += " where compacted = 0"
            rows = list(connection.execute(query, params))
        if not filters:
            return rows
        filtered = []
        for row in rows:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            if all(metadata.get(key) == value for key, value in filters.items()):
                filtered.append(row)
        return filtered

    def _summary_document(self, rows: list[sqlite3.Row], filters: dict[str, Any]) -> VectorMemoryDocument:
        metadatas = [json.loads(str(row["metadata_json"] or "{}")) for row in rows]
        symbol = str(filters.get("symbol") or self._most_common(metadatas, "symbol") or "ALL")
        actions = self._counts(metadatas, "action")
        risk_statuses = self._counts(metadatas, "risk_status")
        outcomes = [
            float(metadata.get("outcome_r", 0.0) or 0.0)
            for metadata in metadatas
            if metadata.get("outcome_r") is not None
        ]
        reason_counts: dict[str, int] = {}
        for metadata in metadatas:
            for reason in metadata.get("reason_codes", []) or []:
                key = str(reason)
                reason_counts[key] = reason_counts.get(key, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:8]
        avg_r = round(sum(outcomes) / len(outcomes), 4) if outcomes else 0.0
        created_at = datetime.now(timezone.utc).isoformat()
        first_seen = min(str(row["created_at"]) for row in rows)
        last_seen = max(str(row["created_at"]) for row in rows)
        digest = hashlib.sha256("|".join(str(row["doc_id"]) for row in rows).encode("utf-8")).hexdigest()[:16]
        text = (
            f"Compacted Quantz memory for {symbol}: {len(rows)} older experiences "
            f"from {first_seen} to {last_seen}. Actions={actions}. Risk={risk_statuses}. "
            f"Average closed/outcome R={avg_r}. Top reasons={top_reasons}."
        )
        return VectorMemoryDocument(
            doc_id=f"summary-{symbol}-{digest}",
            text=text,
            metadata={
                "kind": "context_summary",
                "symbol": symbol,
                "source": "auto_compactor",
                "compacted_count": len(rows),
                "timestamp": created_at,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "outcome": {"average_r": avg_r, "count": len(outcomes)},
            },
        )

    def _cosine(self, left: list[float], right: list[float]) -> float:
        return round(sum(a * b for a, b in zip(left, right)), 8)

    def _counts(self, metadatas: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for metadata in metadatas:
            value = metadata.get(key)
            if value is None:
                continue
            label = str(value)
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8])

    def _most_common(self, metadatas: list[dict[str, Any]], key: str) -> Any:
        counts = self._counts(metadatas, key)
        return next(iter(counts), None)


class ExperienceVectorIndexer:
    """Convert agent experience records into semantic memory documents."""

    def __init__(
        self,
        vector_store: Any,
        compact_after_documents: int = 500,
        compact_to_documents: int = 300,
    ) -> None:
        self.vector_store = vector_store
        self.compact_after_documents = compact_after_documents
        self.compact_to_documents = compact_to_documents

    def append_record(self, record: Any) -> dict[str, Any]:
        document = self.document_from_record(record)
        inserted = self.vector_store.upsert([document])
        compacted: dict[str, Any] | None = None
        if hasattr(self.vector_store, "compact"):
            compacted = self.vector_store.compact(
                filters={"symbol": document.metadata["symbol"]},
                max_documents=self.compact_after_documents,
                target_documents=self.compact_to_documents,
            )
        return {"indexed": inserted, "document": document.doc_id, "compaction": compacted}

    def document_from_record(self, record: Any) -> VectorMemoryDocument:
        context = record.context
        market = context.market
        decision = record.decision
        risk = record.risk
        execution = record.execution
        outcome_r = self._outcome_r(record.outcome)
        reason_codes = list(getattr(decision, "reason_codes", []) or [])
        timestamp = getattr(decision, "timestamp", None) or getattr(market, "timestamp", None)
        side = getattr(getattr(decision, "side", None), "value", None)
        action = getattr(getattr(decision, "action", None), "value", getattr(decision, "action", None))
        risk_status = getattr(getattr(risk, "status", None), "value", getattr(risk, "status", None))
        text = " ".join(
            [
                f"symbol={market.symbol}",
                f"session={market.session}",
                f"trend={market.trend_score:.4f}",
                f"volatility={market.volatility_score:.4f}",
                f"spread={market.spread_points:.2f}",
                f"news={market.news_risk}",
                f"action={action}",
                f"side={side or 'none'}",
                f"confidence={decision.confidence:.4f}",
                f"risk_status={risk_status}",
                f"execution_accepted={bool(execution and execution.accepted)}",
                f"outcome_r={outcome_r if outcome_r is not None else 'none'}",
                "reasons=" + ",".join(reason_codes),
            ]
        )
        return VectorMemoryDocument(
            doc_id=f"experience-{decision.decision_id}",
            text=text,
            metadata={
                "kind": "experience",
                "symbol": market.symbol,
                "source": "agent_loop",
                "decision_id": decision.decision_id,
                "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                "action": action,
                "side": side,
                "risk_status": risk_status,
                "execution_accepted": bool(execution and execution.accepted),
                "outcome_r": outcome_r,
                "reason_codes": reason_codes[:12],
                "outcome": {"r": outcome_r} if outcome_r is not None else None,
            },
        )

    def _outcome_r(self, outcome: dict[str, Any] | None) -> float | None:
        if not outcome:
            return None
        values = []
        for position in outcome.get("closed_positions", []) or []:
            try:
                values.append(float(position.get("r_multiple", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
        if not values:
            return None
        return round(sum(values) / len(values), 4)
