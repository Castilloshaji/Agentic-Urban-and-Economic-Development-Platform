"""Stage 5 — retrieval over the Qdrant document index.

Semantic first, then re-ranked toward the target admin unit. The boost is
reported separately from the raw score so a caller can see how much of a rank
came from relevance versus location.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache

from ..storage.qdrant.client import COLLECTION_NAME, get_client
from .ingest import EMBEDDING_MODEL

# Additive bonuses on a cosine similarity in [-1, 1].
DIRECT_MENTION_BOOST = 0.15   # the chunk names this admin unit (or something in it)
RELATED_MENTION_BOOST = 0.05  # the chunk names a relative of it (parent/child)

DEFAULT_TOP_K = 5
CANDIDATE_MULTIPLIER = 4  # over-fetch before re-ranking, so a boost can change the order


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL)


@lru_cache(maxsize=64)
def _related_admin_ids(admin_id: str) -> frozenset[str]:
    """Parents and children of an admin unit, for the weaker relevance boost."""
    try:
        from ..digital_twin.twin import _neo4j

        with _neo4j() as session:
            record = session.run(
                """
                MATCH (a {admin_id: $admin_id})
                OPTIONAL MATCH (a)-[:LOCATED_IN*1..3]->(ancestor)
                OPTIONAL MATCH (descendant)-[:LOCATED_IN*1..3]->(a)
                RETURN collect(DISTINCT ancestor.admin_id) + collect(DISTINCT descendant.admin_id) AS ids
                """,
                admin_id=admin_id,
            ).single()
        return frozenset(i for i in (record["ids"] if record else []) if i)
    except Exception as error:  # retrieval must not die because the graph is down
        print(f"  note: could not load relatives of {admin_id}: {error}", file=sys.stderr)
        return frozenset()


def retrieve(query: str, admin_id: str | None = None, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """Top-k chunks for a query, boosted toward admin_id when given.

    Returns dicts carrying the text, its provenance (source_file, chunk_index)
    and the score breakdown. Provenance travels with every chunk because Step 9
    has to cite each claim back to a filename.
    """
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        raise RuntimeError(
            f"Qdrant collection {COLLECTION_NAME!r} does not exist. "
            f"Run: python -m src.rag.ingest"
        )

    vector = _model().encode(query, normalize_embeddings=True).tolist()
    candidates = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=max(top_k * CANDIDATE_MULTIPLIER, top_k),
        with_payload=True,
    ).points

    related = _related_admin_ids(admin_id) if admin_id else frozenset()

    results = []
    for point in candidates:
        payload = point.payload or {}
        mentioned = payload.get("admin_ids_mentioned") or []
        boost, reason = 0.0, None
        if admin_id and admin_id in mentioned:
            boost, reason = DIRECT_MENTION_BOOST, "mentions_admin_id"
        elif admin_id and related.intersection(mentioned):
            boost, reason = RELATED_MENTION_BOOST, "mentions_related_admin_id"

        results.append({
            "text": payload.get("text"),
            "source_file": payload.get("source_file"),
            "chunk_index": payload.get("chunk_index"),
            "admin_ids_mentioned": mentioned,
            "matched_entities": payload.get("matched_entities", []),
            "score": round(point.score + boost, 6),
            "raw_score": round(point.score, 6),
            "boost": round(boost, 6),
            "boost_reason": reason,
            "truncated_by_encoder": payload.get("truncated_by_encoder", False),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag.retrieve", description="Query the document index.")
    parser.add_argument("query")
    parser.add_argument("--admin-id", default=None)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)
    print(json.dumps(retrieve(args.query, args.admin_id, args.top_k), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
