# ADR-003: PostgreSQL + pgvector with hybrid retrieval

**Status:** accepted

## Context
About 32 current policy PDFs (~37k words). Answers need exact terms ("EL", "iAssistant") and
paraphrases ("leave when my baby is born"), only from **current** versions, with citations.

## Options
| Option | Notes |
|---|---|
| Put whole PDFs in the prompt | Works at this size, but every question pays for ~50k tokens, citations are coarse, and there is no version or location filtering. |
| Dedicated vector DB (Pinecone, Chroma, ...) | One more system to run and secure; keyword search is weaker. |
| **Postgres full-text + pgvector, fused with RRF** | One datastore, which we already need for HR data and checkpoints. SQL filters on version status and location. |

## Decision
Hybrid retrieval in Postgres. Full-text search uses an OR-query ranked by `ts_rank_cd`; vectors
use cosine distance on an HNSW index; the two are combined with reciprocal rank fusion and
filtered to `status = current` and the employee's location. Embeddings come from local fastembed
(`bge-small-en-v1.5`), so no document text leaves the machine during ingestion. A deterministic
hashing embedder is the offline and test fallback.

## Consequences
Superseded versions cannot leak into answers because they have no chunks. A reranker could be
added if quality data shows the need.
