"""Qdrant connection and collection lifecycle.

Vectors live in the `qdrant_data` Docker volume, never in the repository.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from ..postgres.load import PROJECT_ROOT

COLLECTION_NAME = "ernakulam_docs"

# all-MiniLM-L6-v2 emits 384-dim vectors; cosine matches its normalised output.
VECTOR_SIZE = 384
DISTANCE = "Cosine"


def get_client():
    """A QdrantClient built from .env (QDRANT_URL, else QDRANT_HOST/PORT)."""
    from qdrant_client import QdrantClient

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("QDRANT_URL")
    if url:
        return QdrantClient(url=url)
    return QdrantClient(
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
    )


def ensure_collection(client, vector_size: int = VECTOR_SIZE, recreate: bool = False) -> bool:
    """Create the collection if absent. Returns True if it was created here.

    vector_size is passed in rather than assumed, so a change of embedding model
    surfaces as an explicit argument instead of a dimension mismatch at upsert.
    """
    from qdrant_client.models import Distance, VectorParams

    if recreate and client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    if client.collection_exists(COLLECTION_NAME):
        return False
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=vector_size, distance=Distance[DISTANCE.upper()]),
    )
    return True
