"""Stage 5 — documents -> chunks -> embeddings -> Qdrant.

Chunks are linked to admin units as they are built, resolving place names,
infrastructure names, and the ids of things that spatially overlap an admin
unit — not just literal admin_id strings.

    python -m src.rag.ingest [--recreate]
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..storage.postgres.load import PROJECT_ROOT, connection_string
from ..storage.qdrant.client import COLLECTION_NAME, ensure_collection, get_client

DOCUMENTS_DIR = PROJECT_ROOT / "data" / "raw" / "documents"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# The brief's chunk budget. all-MiniLM-L6-v2 only encodes 256 tokens at a time,
# so a 650-token chunk cannot be embedded in one pass. Rather than shrink the
# chunks (which would break the spec and fragment the excerpts that get quoted
# in reports) or truncate them (which would silently make 61% of each chunk
# unsearchable), a long chunk is embedded as several encoder-sized windows whose
# vectors are mean-pooled. See embed_chunks().
TARGET_TOKENS = 650
MIN_TOKENS = 500
MAX_TOKENS = 800
OVERLAP_TOKENS = 50

# Rough words-per-token for English prose; only used to size windows before the
# real tokenizer sees them.
WORDS_PER_TOKEN = 0.75


@dataclass
class Chunk:
    text: str
    source_file: str
    chunk_index: int
    admin_ids_mentioned: list[str] = field(default_factory=list)
    matched_entities: list[dict] = field(default_factory=list)
    token_count: int = 0
    encoder_windows: int = 1


# entity index, built from the loaded twin

def build_entity_index(edition: str | None = None) -> list[dict]:
    """Names and ids that imply an admin unit, drawn from PostGIS + Neo4j.

    Each entry maps a searchable surface form to the admin_id(s) it implies:
      - an admin unit's own id and name        -> itself
      - a metro station / bus stop id and name -> the admin unit it sits in
      - a flood zone id                        -> every admin unit it overlaps
    """
    import psycopg2

    entries: list[dict] = []
    with psycopg2.connect(connection_string()) as connection, connection.cursor() as cursor:
        edition_clause = "" if edition is None else " AND dataset_edition = %s"
        params = () if edition is None else (edition,)

        cursor.execute(
            f"SELECT DISTINCT admin_id, name, level FROM admin_boundary WHERE TRUE{edition_clause}", params)
        for admin_id, name, level in cursor.fetchall():
            entries.append({"surface": admin_id, "admin_ids": [admin_id], "kind": f"{level}_id"})
            if name:
                entries.append({"surface": name, "admin_ids": [admin_id], "kind": f"{level}_name"})

        for table, id_column, kind in (
            ("metro_station", "station_id", "metro_station"),
            ("bus_stop", "stop_id", "bus_stop"),
        ):
            cursor.execute(
                f"SELECT DISTINCT {id_column}, name, admin_id FROM {table} "
                f"WHERE admin_id IS NOT NULL{edition_clause}", params)
            for entity_id, name, admin_id in cursor.fetchall():
                entries.append({"surface": entity_id, "admin_ids": [admin_id], "kind": f"{kind}_id"})
                if name:
                    entries.append({"surface": name, "admin_ids": [admin_id], "kind": f"{kind}_name"})

    # Flood zones imply every admin unit they overlap — that edge lives in Neo4j.
    from ..digital_twin.twin import _neo4j

    with _neo4j() as session:
        records = session.run(
            "MATCH (f:FloodZone)-[:OVERLAPS]->(a) "
            "RETURN f.flood_zone_id AS zone_id, collect(DISTINCT a.admin_id) AS admin_ids"
        ).data()
    for record in records:
        if record["zone_id"]:
            entries.append({"surface": record["zone_id"], "admin_ids": record["admin_ids"],
                            "kind": "flood_zone_id"})

    # Longest surface forms first, so "Demo Panchayat A Ward A" wins over "Demo Panchayat A".
    entries.sort(key=lambda e: len(e["surface"] or ""), reverse=True)
    return [e for e in entries if e["surface"]]


def find_admin_ids(text: str, entity_index: list[dict]) -> tuple[list[str], list[dict]]:
    """Which admin units a chunk is about, and via which surface form."""
    lowered = text.lower()
    admin_ids: list[str] = []
    matched: list[dict] = []
    for entry in entity_index:
        surface = str(entry["surface"]).lower()
        # Word-boundary match so DEMO-P-1 does not match inside DEMO-P-01.
        if not re.search(rf"(?<![\w-]){re.escape(surface)}(?![\w-])", lowered):
            continue
        matched.append({"surface": entry["surface"], "kind": entry["kind"],
                        "implies": entry["admin_ids"]})
        for admin_id in entry["admin_ids"]:
            if admin_id and admin_id not in admin_ids:
                admin_ids.append(admin_id)
    return admin_ids, matched


# reading and chunking

def read_document(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            print(f"  skipping {path.name}: pypdf is not installed (pip install pypdf)", file=sys.stderr)
            return ""
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def chunk_text(text: str, source_file: str) -> list[Chunk]:
    """Split into ~500-800 token windows with ~50 tokens of overlap.

    Windows are cut on paragraph then sentence boundaries where possible, so a
    chunk is a readable unit rather than an arbitrary slice — a retrieved chunk
    ends up quoted in a report, and half a sentence cites badly.
    """
    words = text.split()
    if not words:
        return []

    target_words = int(TARGET_TOKENS * WORDS_PER_TOKEN)
    max_words = int(MAX_TOKENS * WORDS_PER_TOKEN)
    overlap_words = int(OVERLAP_TOKENS * WORDS_PER_TOKEN)

    chunks: list[Chunk] = []
    start = 0
    while start < len(words):
        end = min(start + target_words, len(words))
        window = words[start:end]

        # Prefer to end on a sentence boundary within the allowed range.
        if end < len(words):
            for offset in range(len(window) - 1, max(len(window) - 60, 0), -1):
                if window[offset].endswith((".", "!", "?")):
                    end = start + offset + 1
                    window = words[start:end]
                    break
        if end - start > max_words:
            end = start + max_words
            window = words[start:end]

        chunks.append(Chunk(text=" ".join(window), source_file=source_file, chunk_index=len(chunks)))
        if end >= len(words):
            break
        start = max(end - overlap_words, start + 1)
    return chunks


def _encoder_windows(text: str, tokenizer, limit: int, overlap_words: int) -> list[str]:
    """Split one chunk into the largest pieces the encoder can take whole.

    Greedy with a binary search for the longest word run that fits the token
    limit — token counts are not proportional to word counts, so guessing a
    fixed word width either wastes the window or overshoots it.
    """
    words = text.split()
    windows: list[str] = []
    start = 0
    while start < len(words):
        low, high, best = 1, len(words) - start, 1
        while low <= high:
            mid = (low + high) // 2
            candidate = " ".join(words[start:start + mid])
            if len(tokenizer.encode(candidate, add_special_tokens=True)) <= limit:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        windows.append(" ".join(words[start:start + best]))
        if start + best >= len(words):
            break
        start += max(best - overlap_words, 1)
    return windows


def embed_chunks(model, chunks: list[Chunk]):
    """Embed chunks, splitting any that exceed the encoder's window.

    A chunk longer than max_seq_length is encoded as several overlapping windows
    and their vectors are mean-pooled, so the whole chunk is searchable rather
    than just its opening. The result is re-normalised because the mean of unit
    vectors is not itself a unit vector, and cosine scoring assumes it is.
    """
    import numpy as np

    tokenizer, limit = model.tokenizer, model.max_seq_length
    overlap_words = max(int(OVERLAP_TOKENS * WORDS_PER_TOKEN), 1)

    texts_to_encode: list[str] = []
    spans: list[tuple[int, int]] = []
    for chunk in chunks:
        chunk.token_count = len(tokenizer.encode(chunk.text, add_special_tokens=True))
        windows = ([chunk.text] if chunk.token_count <= limit
                   else _encoder_windows(chunk.text, tokenizer, limit, overlap_words))
        chunk.encoder_windows = len(windows)
        spans.append((len(texts_to_encode), len(windows)))
        texts_to_encode.extend(windows)

    vectors = model.encode(texts_to_encode, show_progress_bar=False, normalize_embeddings=True)

    pooled = []
    for offset, count in spans:
        window_vectors = vectors[offset:offset + count]
        vector = window_vectors[0] if count == 1 else window_vectors.mean(axis=0)
        norm = np.linalg.norm(vector)
        pooled.append(vector / norm if norm else vector)
    return np.vstack(pooled)


# driver

def ingest(recreate: bool = False) -> dict:
    from qdrant_client.models import PointStruct
    from sentence_transformers import SentenceTransformer

    paths = sorted(p for p in DOCUMENTS_DIR.rglob("*") if p.suffix.lower() in {".txt", ".pdf"})
    if not paths:
        print(f"No .txt/.pdf documents in {DOCUMENTS_DIR}", file=sys.stderr)
        return {"documents": 0, "chunks": 0}

    print("Building entity index from the digital twin")
    entity_index = build_entity_index()
    print(f"  {len(entity_index)} surface form(s) known")

    print(f"Loading {EMBEDDING_MODEL}")
    model = SentenceTransformer(EMBEDDING_MODEL)

    all_chunks: list[Chunk] = []
    for path in paths:
        text = read_document(path)
        if not text.strip():
            continue
        chunks = chunk_text(text, path.name)
        for chunk in chunks:
            chunk.admin_ids_mentioned, chunk.matched_entities = find_admin_ids(chunk.text, entity_index)
        all_chunks.extend(chunks)
        print(f"  {path.name}: {len(chunks)} chunk(s)")

    vectors = embed_chunks(model, all_chunks)
    multi = [c for c in all_chunks if c.encoder_windows > 1]
    if multi:
        print(f"  {len(multi)} chunk(s) exceed the encoder's {model.max_seq_length}-token "
              f"window; embedded as pooled sub-windows (no truncation)")

    client = get_client()
    # The real embedding width is passed through, so swapping the model fails
    # loudly here rather than silently at upsert.
    ensure_collection(client, vector_size=vectors.shape[1], recreate=recreate)

    points = [
        PointStruct(
            # Deterministic id: re-ingesting a document updates its chunks in
            # place instead of duplicating them.
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk.source_file}#{chunk.chunk_index}")),
            vector=vector.tolist(),
            payload={
                "text": chunk.text,
                "source_file": chunk.source_file,
                "chunk_index": chunk.chunk_index,
                "admin_ids_mentioned": chunk.admin_ids_mentioned,
                "matched_entities": chunk.matched_entities,
                "token_count": chunk.token_count,
                "encoder_windows": chunk.encoder_windows,
                "truncated_by_encoder": False,
                "embedding_model": EMBEDDING_MODEL,
            },
        )
        for chunk, vector in zip(all_chunks, vectors)
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)

    count = client.get_collection(COLLECTION_NAME).points_count
    print(f"\nUpserted {len(points)} chunk(s) into {COLLECTION_NAME!r} (points_count={count})")
    for chunk in all_chunks:
        print(f"  {chunk.source_file}#{chunk.chunk_index}: "
              f"{chunk.token_count} tokens in {chunk.encoder_windows} window(s), "
              f"admin_ids={chunk.admin_ids_mentioned}")
    return {"documents": len(paths), "chunks": len(points), "points_count": count}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag.ingest", description="Chunk, embed and index documents.")
    parser.add_argument("--recreate", action="store_true", help="Drop and recreate the collection first.")
    args = parser.parse_args(argv)
    ingest(recreate=args.recreate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
