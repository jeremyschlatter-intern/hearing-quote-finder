"""FastAPI server for the Hearing Quote Finder."""

import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

from database import init_db, get_db
from ingest import ingest_hearings
from extract import process_topic_for_hearing, get_quote_detail, screen_hearing_relevance, extract_quotes_for_topic
from embeddings import init_embedding_tables, embed_all_hearings, semantic_search, synthesize_answer

# Background worker state
_worker_task = None
_embedding_task = None


@asynccontextmanager
async def lifespan(app):
    """Startup and shutdown logic."""
    await init_db()
    await init_embedding_tables()
    await recover_stale_processing()
    await ensure_all_pending_records()
    # Start background workers
    global _worker_task, _embedding_task
    _worker_task = asyncio.create_task(scan_worker())
    _embedding_task = asyncio.create_task(embedding_worker())
    yield
    # Shutdown
    for task in [_worker_task, _embedding_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Hearing Quote Finder", lifespan=lifespan)


async def recover_stale_processing():
    """On startup, reset any 'processing' records to 'pending' (interrupted by restart)."""
    db = await get_db()
    result = await db.execute(
        "UPDATE processing_status SET status = 'pending' WHERE status = 'processing'"
    )
    count = result.rowcount
    await db.commit()
    await db.close()
    if count:
        print(f"Recovered {count} interrupted processing records")


async def ensure_all_pending_records():
    """Ensure every topic/hearing combo has a processing_status record."""
    db = await get_db()
    inserted = 0
    topics = await db.execute_fetchall("SELECT id FROM topics")
    hearings = await db.execute_fetchall("SELECT id FROM hearings WHERE transcript_fetched = 1")

    for (tid,) in topics:
        existing = await db.execute_fetchall(
            "SELECT hearing_id FROM processing_status WHERE topic_id = ?", (tid,)
        )
        existing_ids = {r[0] for r in existing}
        for (hid,) in hearings:
            if hid not in existing_ids:
                await db.execute(
                    "INSERT INTO processing_status (topic_id, hearing_id, status) VALUES (?, ?, 'pending')",
                    (tid, hid)
                )
                inserted += 1

    await db.commit()
    await db.close()
    if inserted:
        print(f"Created {inserted} pending processing records")


SCAN_CONCURRENCY = 4  # Process up to 4 hearings in parallel


async def scan_worker():
    """Background worker that continuously processes pending items in parallel."""
    print(f"Scan worker started (concurrency={SCAN_CONCURRENCY})")
    while True:
        try:
            db = await get_db()
            rows = await db.execute_fetchall("""
                SELECT ps.topic_id, ps.hearing_id, t.name, t.description
                FROM processing_status ps
                JOIN topics t ON ps.topic_id = t.id
                WHERE ps.status = 'pending'
                ORDER BY t.created_at ASC, ps.hearing_id ASC
                LIMIT ?
            """, (SCAN_CONCURRENCY,))
            await db.close()

            if not rows:
                await asyncio.sleep(5)
                continue

            # Process batch in parallel
            tasks = [
                process_topic_for_hearing(r[0], r[2], r[3] or "", r[1])
                for r in rows
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            # Brief pause between batches
            await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            print("Scan worker shutting down")
            return
        except Exception as e:
            print(f"Scan worker error: {e}")
            await asyncio.sleep(5)


async def embedding_worker():
    """Background worker that embeds hearings for semantic search."""
    print("Embedding worker started")
    # Wait a bit for the server to settle before starting
    await asyncio.sleep(10)
    while True:
        try:
            db = await get_db()
            # Find hearings that need embedding
            row = await db.execute_fetchall("""
                SELECT h.id, h.title FROM hearings h
                WHERE h.transcript_fetched = 1
                AND h.id NOT IN (
                    SELECT DISTINCT hearing_id FROM transcript_chunks WHERE embedding IS NOT NULL
                )
                LIMIT 1
            """)
            await db.close()

            if not row:
                await asyncio.sleep(30)  # Check less frequently when caught up
                continue

            from embeddings import embed_hearing
            hid, title = row[0]
            n = await embed_hearing(hid)
            if n > 0:
                print(f"Embedded: {title[:50]}... ({n} chunks)")
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            print("Embedding worker shutting down")
            return
        except Exception as e:
            print(f"Embedding worker error: {e}")
            await asyncio.sleep(10)


class TopicCreate(BaseModel):
    name: str
    description: str = ""


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/hearings")
async def list_hearings():
    db = await get_db()
    rows = await db.execute_fetchall("""
        SELECT id, package_id, title, date_held, chamber, committee, transcript_fetched
        FROM hearings ORDER BY date_held DESC
    """)
    await db.close()
    return [{
        "id": r[0], "package_id": r[1], "title": r[2], "date_held": r[3],
        "chamber": r[4], "committee": r[5], "has_transcript": r[6] == 1,
    } for r in rows]


@app.post("/api/ingest")
async def trigger_ingest(max_hearings: int = 60):
    """Trigger hearing ingestion from GovInfo."""
    asyncio.create_task(ingest_hearings(max_hearings=max_hearings))
    return {"status": "ingestion_started", "max_hearings": max_hearings}


@app.get("/api/topics")
async def list_topics():
    db = await get_db()
    topics = await db.execute_fetchall("SELECT id, name, description, created_at FROM topics ORDER BY created_at DESC")

    result = []
    for t in topics:
        total = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ?", (t[0],)
        )
        done = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ? AND status IN ('done', 'skipped', 'error')", (t[0],)
        )
        processing = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ? AND status IN ('processing', 'pending')", (t[0],)
        )
        quote_count = await db.execute_fetchall(
            "SELECT COUNT(*) FROM quotes WHERE topic_id = ?", (t[0],)
        )

        result.append({
            "id": t[0], "name": t[1], "description": t[2], "created_at": t[3],
            "hearings_total": total[0][0] if total else 0,
            "hearings_done": done[0][0] if done else 0,
            "hearings_processing": processing[0][0] if processing else 0,
            "quote_count": quote_count[0][0] if quote_count else 0,
        })

    await db.close()
    return result


@app.post("/api/topics")
async def create_topic(topic: TopicCreate):
    db = await get_db()

    existing = await db.execute_fetchall("SELECT id FROM topics WHERE name = ?", (topic.name,))
    if existing:
        await db.close()
        raise HTTPException(status_code=409, detail="Topic already exists")

    cursor = await db.execute(
        "INSERT INTO topics (name, description) VALUES (?, ?)",
        (topic.name, topic.description)
    )
    topic_id = cursor.lastrowid

    # Create pending records for all hearings - the worker will pick them up
    hearings = await db.execute_fetchall("SELECT id FROM hearings WHERE transcript_fetched = 1")
    for (hid,) in hearings:
        await db.execute(
            "INSERT INTO processing_status (topic_id, hearing_id, status) VALUES (?, ?, 'pending')",
            (topic_id, hid)
        )

    await db.commit()
    await db.close()

    return {"id": topic_id, "name": topic.name, "status": "queued"}


@app.delete("/api/topics/{topic_id}")
async def delete_topic(topic_id: int):
    db = await get_db()
    await db.execute("DELETE FROM quotes WHERE topic_id = ?", (topic_id,))
    await db.execute("DELETE FROM processing_status WHERE topic_id = ?", (topic_id,))
    await db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    await db.commit()
    await db.close()
    return {"status": "deleted"}


@app.get("/api/semantic-search")
async def api_semantic_search(
    q: str = "",
    committee: Optional[str] = None,
    date_after: Optional[str] = None,
    synthesize: bool = False,
    limit: int = 20,
):
    """Semantic search across hearing transcripts using embeddings."""
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    # Check if we have any embeddings
    db = await get_db()
    count = await db.execute_fetchall("SELECT COUNT(*) FROM transcript_chunks WHERE embedding IS NOT NULL")
    await db.close()

    if not count or count[0][0] == 0:
        return {"results": [], "synthesis": None, "message": "Embeddings are still being generated. Please try again shortly."}

    results = await semantic_search(q, limit=limit, committee_filter=committee, date_after=date_after)

    synthesis_text = None
    if synthesize and results:
        try:
            synthesis_text = await synthesize_answer(q, results)
        except Exception as e:
            synthesis_text = f"Synthesis error: {e}"

    return {
        "query": q,
        "results": [{
            "chunk_text": r["chunk_text"][:2000],
            "speaker": r["speaker"],
            "similarity": round(r["similarity"], 4),
            "hearing_title": r["hearing_title"],
            "date_held": r["date_held"],
            "chamber": r["chamber"],
            "committee": r["committee"],
            "package_id": r["package_id"],
        } for r in results],
        "synthesis": synthesis_text,
    }


@app.get("/api/quotes")
async def list_quotes(
    topic_id: Optional[int] = None,
    hearing_id: Optional[int] = None,
    speaker: Optional[str] = None,
    search: Optional[str] = None,
    date_after: Optional[str] = None,
    committee: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    db = await get_db()
    conditions = []
    params = []

    if topic_id:
        conditions.append("q.topic_id = ?")
        params.append(topic_id)
    if hearing_id:
        conditions.append("q.hearing_id = ?")
        params.append(hearing_id)
    if speaker:
        conditions.append("q.speaker LIKE ?")
        params.append(f"%{speaker}%")
    if search:
        conditions.append("(q.quote_text LIKE ? OR q.speaker LIKE ? OR h.title LIKE ? OR h.committee LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if date_after:
        conditions.append("h.date_held >= ?")
        params.append(date_after)
    if committee:
        # Support comma-separated committee filter
        committees = [c.strip() for c in committee.split(",")]
        placeholders = " OR ".join(["h.committee LIKE ?" for _ in committees])
        conditions.append(f"({placeholders})")
        params.extend([f"%{c}%" for c in committees])

    where = " AND ".join(conditions) if conditions else "1=1"

    count_row = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM quotes q JOIN hearings h ON q.hearing_id = h.id WHERE {where}",
        params
    )
    total = count_row[0][0] if count_row else 0

    rows = await db.execute_fetchall(f"""
        SELECT q.id, q.speaker, q.quote_text, q.relevance, q.context_before, q.context_after,
               h.title as hearing_title, h.date_held, h.chamber, h.committee, h.package_id,
               t.name as topic_name, t.id as topic_id
        FROM quotes q
        JOIN hearings h ON q.hearing_id = h.id
        JOIN topics t ON q.topic_id = t.id
        WHERE {where}
        ORDER BY h.date_held DESC, q.id DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset])
    await db.close()

    return {
        "total": total,
        "quotes": [{
            "id": r[0], "speaker": r[1], "quote_text": r[2], "relevance": r[3],
            "context_before": r[4], "context_after": r[5],
            "hearing_title": r[6], "date_held": r[7], "chamber": r[8],
            "committee": r[9], "package_id": r[10], "topic_name": r[11], "topic_id": r[12],
        } for r in rows],
    }


@app.get("/api/quotes/{quote_id}")
async def get_quote(quote_id: int):
    detail = await get_quote_detail(quote_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Quote not found")
    return detail


@app.get("/api/export")
async def export_quotes(
    topic_id: Optional[int] = None,
    search: Optional[str] = None,
    date_after: Optional[str] = None,
    committee: Optional[str] = None,
):
    """Export all matching quotes as CSV."""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    db = await get_db()
    conditions = []
    params = []
    if topic_id:
        conditions.append("q.topic_id = ?")
        params.append(topic_id)
    if search:
        conditions.append("(q.quote_text LIKE ? OR q.speaker LIKE ? OR h.title LIKE ? OR h.committee LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if date_after:
        conditions.append("h.date_held >= ?")
        params.append(date_after)
    if committee:
        committees = [c.strip() for c in committee.split(",")]
        placeholders = " OR ".join(["h.committee LIKE ?" for _ in committees])
        conditions.append(f"({placeholders})")
        params.extend([f"%{c}%" for c in committees])

    where = " AND ".join(conditions) if conditions else "1=1"
    rows = await db.execute_fetchall(f"""
        SELECT q.speaker, q.quote_text, h.title, h.date_held, h.chamber, h.committee,
               t.name, q.relevance, h.package_id
        FROM quotes q
        JOIN hearings h ON q.hearing_id = h.id
        JOIN topics t ON q.topic_id = t.id
        WHERE {where}
        ORDER BY h.date_held DESC
    """, params)
    await db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Speaker", "Quote", "Hearing", "Date", "Chamber", "Committee", "Topic", "Relevance", "GovInfo URL"])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                        f"https://www.govinfo.gov/app/details/{r[8]}"])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hearing_quotes.csv"}
    )


@app.get("/api/speakers")
async def list_speakers():
    db = await get_db()
    rows = await db.execute_fetchall("""
        SELECT DISTINCT speaker, COUNT(*) as quote_count
        FROM quotes
        GROUP BY speaker
        ORDER BY quote_count DESC
    """)
    await db.close()
    return [{"name": r[0], "quote_count": r[1]} for r in rows]


@app.get("/api/committees")
async def list_committees():
    """Get unique committees that have quotes, grouped by chamber."""
    db = await get_db()
    rows = await db.execute_fetchall("""
        SELECT DISTINCT h.committee, h.chamber, COUNT(q.id) as quote_count
        FROM quotes q
        JOIN hearings h ON q.hearing_id = h.id
        WHERE h.committee IS NOT NULL AND h.committee != ''
        GROUP BY h.committee, h.chamber
        ORDER BY h.chamber, quote_count DESC
    """)
    await db.close()
    return [{"name": r[0], "chamber": r[1], "quote_count": r[2]} for r in rows]


@app.get("/api/status")
async def get_status():
    db = await get_db()
    hearings = await db.execute_fetchall("SELECT COUNT(*) FROM hearings")
    transcripts = await db.execute_fetchall("SELECT COUNT(*) FROM hearings WHERE transcript_fetched = 1")
    topics = await db.execute_fetchall("SELECT COUNT(*) FROM topics")
    quotes = await db.execute_fetchall("SELECT COUNT(*) FROM quotes")
    pending = await db.execute_fetchall("SELECT COUNT(*) FROM processing_status WHERE status IN ('processing', 'pending')")
    await db.close()

    return {
        "hearings_total": hearings[0][0],
        "hearings_with_transcripts": transcripts[0][0],
        "topics_count": topics[0][0],
        "quotes_count": quotes[0][0],
        "currently_processing": pending[0][0],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8347)
