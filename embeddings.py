"""Semantic search using OpenAI embeddings stored in SQLite."""

import asyncio
import json
import os
import numpy as np
import openai
from database import get_db

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHUNK_SIZE = 500  # tokens ~= words * 1.3, so ~380 words per chunk
CHUNK_OVERLAP = 50

_openai_client = None


def get_openai():
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def init_embedding_tables():
    """Create the embedding storage tables."""
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS transcript_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hearing_id INTEGER NOT NULL REFERENCES hearings(id),
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            speaker TEXT DEFAULT '',
            embedding BLOB,
            UNIQUE(hearing_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_hearing ON transcript_chunks(hearing_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON transcript_chunks(embedding);
    """)
    await db.commit()
    await db.close()


def chunk_transcript_for_embedding(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split transcript into overlapping chunks for embedding.

    Tries to break at speaker turns for cleaner chunks.
    Returns list of (chunk_text, speaker) tuples.
    """
    import re
    lines = text.split('\n')
    chunks = []
    current_chunk = []
    current_words = 0
    current_speaker = ""

    speaker_pattern = re.compile(
        r'^\s{4,}((?:Mr\.|Mrs\.|Ms\.|Dr\.|Senator|Representative|Chairman|Chairwoman|'
        r'General|Admiral|Secretary|The Chairman|The Chairwoman|Ranking Member|Voice)\s+\S+)'
    )

    for line in lines:
        # Detect speaker changes
        m = speaker_pattern.match(line)
        if m:
            current_speaker = m.group(1).strip().rstrip('.')

        words = line.split()
        current_chunk.append(line)
        current_words += len(words)

        if current_words >= chunk_size:
            chunk_text = '\n'.join(current_chunk).strip()
            if chunk_text and len(chunk_text) > 50:
                chunks.append((chunk_text, current_speaker))

            # Keep overlap
            overlap_lines = []
            overlap_words = 0
            for prev_line in reversed(current_chunk):
                overlap_words += len(prev_line.split())
                overlap_lines.insert(0, prev_line)
                if overlap_words >= overlap:
                    break
            current_chunk = overlap_lines
            current_words = overlap_words

    # Final chunk
    if current_chunk:
        chunk_text = '\n'.join(current_chunk).strip()
        if chunk_text and len(chunk_text) > 50:
            chunks.append((chunk_text, current_speaker))

    return chunks


async def embed_texts(texts):
    """Get embeddings for a list of texts using OpenAI API."""
    client = get_openai()
    # OpenAI supports batching up to 2048 texts
    embeddings = []
    batch_size = 100
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        response = await asyncio.to_thread(
            lambda b=batch: client.embeddings.create(model=EMBEDDING_MODEL, input=b)
        )
        embeddings.extend([d.embedding for d in response.data])
    return embeddings


async def embed_hearing(hearing_id):
    """Chunk and embed a single hearing's transcript."""
    db = await get_db()

    # Check if already embedded
    existing = await db.execute_fetchall(
        "SELECT COUNT(*) FROM transcript_chunks WHERE hearing_id = ? AND embedding IS NOT NULL",
        (hearing_id,)
    )
    if existing and existing[0][0] > 0:
        await db.close()
        return 0  # Already done

    # Get transcript
    row = await db.execute_fetchall(
        "SELECT transcript_text FROM hearings WHERE id = ? AND transcript_fetched = 1",
        (hearing_id,)
    )
    if not row or not row[0][0]:
        await db.close()
        return 0

    transcript = row[0][0]
    chunks = chunk_transcript_for_embedding(transcript)

    if not chunks:
        await db.close()
        return 0

    # Insert chunks
    for i, (chunk_text, speaker) in enumerate(chunks):
        await db.execute(
            "INSERT OR IGNORE INTO transcript_chunks (hearing_id, chunk_index, chunk_text, speaker) VALUES (?, ?, ?, ?)",
            (hearing_id, i, chunk_text, speaker)
        )
    await db.commit()

    # Embed in batches
    texts = [c[0] for c in chunks]
    embeddings = await embed_texts(texts)

    for i, emb in enumerate(embeddings):
        blob = np.array(emb, dtype=np.float32).tobytes()
        await db.execute(
            "UPDATE transcript_chunks SET embedding = ? WHERE hearing_id = ? AND chunk_index = ?",
            (blob, hearing_id, i)
        )
    await db.commit()
    await db.close()
    return len(chunks)


async def embed_all_hearings():
    """Embed all hearings that haven't been embedded yet."""
    db = await get_db()
    hearings = await db.execute_fetchall(
        "SELECT id, title FROM hearings WHERE transcript_fetched = 1"
    )
    await db.close()

    total_chunks = 0
    for i, (hid, title) in enumerate(hearings):
        n = await embed_hearing(hid)
        if n > 0:
            total_chunks += n
            print(f"  Embedded hearing {i+1}/{len(hearings)}: {title[:50]}... ({n} chunks)")

    print(f"Embedding complete: {total_chunks} new chunks embedded")
    return total_chunks


async def semantic_search(query, limit=20, committee_filter=None, date_after=None):
    """Search transcript chunks by semantic similarity to query.

    Returns list of dicts with chunk_text, speaker, hearing info, and similarity score.
    """
    # Embed the query
    query_emb = (await embed_texts([query]))[0]
    query_vec = np.array(query_emb, dtype=np.float32)

    db = await get_db()

    # Build conditions for filtering
    conditions = ["tc.embedding IS NOT NULL"]
    params = []
    if committee_filter:
        committees = [c.strip() for c in committee_filter.split(",")]
        placeholders = " OR ".join(["h.committee LIKE ?" for _ in committees])
        conditions.append(f"({placeholders})")
        params.extend([f"%{c}%" for c in committees])
    if date_after:
        conditions.append("h.date_held >= ?")
        params.append(date_after)

    where = " AND ".join(conditions)

    rows = await db.execute_fetchall(f"""
        SELECT tc.id, tc.chunk_text, tc.speaker, tc.embedding,
               h.id as hearing_id, h.title, h.date_held, h.chamber, h.committee, h.package_id
        FROM transcript_chunks tc
        JOIN hearings h ON tc.hearing_id = h.id
        WHERE {where}
    """, params)
    await db.close()

    if not rows:
        return []

    # Compute cosine similarities
    results = []
    for row in rows:
        emb_blob = row[3]
        chunk_vec = np.frombuffer(emb_blob, dtype=np.float32)
        # Cosine similarity
        similarity = float(np.dot(query_vec, chunk_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(chunk_vec)))
        results.append({
            "chunk_id": row[0],
            "chunk_text": row[1],
            "speaker": row[2],
            "similarity": similarity,
            "hearing_id": row[4],
            "hearing_title": row[5],
            "date_held": row[6],
            "chamber": row[7],
            "committee": row[8],
            "package_id": row[9],
        })

    # Sort by similarity and return top results
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]


async def synthesize_answer(query, chunks, max_chunks=15):
    """Use Claude to synthesize an answer from the top matching chunks."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    # Build context from chunks
    context_parts = []
    for i, c in enumerate(chunks[:max_chunks]):
        context_parts.append(
            f"[{i+1}] {c['hearing_title']} ({c['date_held']}, {c['committee']})\n"
            f"Speaker: {c['speaker']}\n"
            f"{c['chunk_text'][:1500]}\n"
        )
    context = "\n---\n".join(context_parts)

    prompt = f"""A congressional staffer asked: "{query}"

Below are excerpts from recent congressional hearing transcripts that are relevant to this question. Synthesize a clear, useful answer.

Focus on:
- What specific questions were asked and by whom
- What answers or testimony was given
- Which hearings and dates these came from
- Any notable disagreements or patterns across hearings

Be specific with names, dates, and committees. Cite the excerpt numbers [1], [2], etc.

HEARING EXCERPTS:
{context}

Provide a concise but thorough synthesis."""

    response = await asyncio.to_thread(
        lambda: client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    )

    return response.content[0].text
