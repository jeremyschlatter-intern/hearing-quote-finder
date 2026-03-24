"""FastAPI server for the Hearing Quote Finder."""

import asyncio
import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from database import init_db, get_db
from ingest import ingest_hearings
from extract import process_topic, get_quote_detail

app = FastAPI(title="Hearing Quote Finder")

# Track background processing
processing_tasks = {}


class TopicCreate(BaseModel):
    name: str
    description: str = ""


@app.on_event("startup")
async def startup():
    await init_db()


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
async def trigger_ingest(background_tasks: BackgroundTasks, max_hearings: int = 60):
    """Trigger hearing ingestion from GovInfo."""
    async def do_ingest():
        await ingest_hearings(max_hearings=max_hearings)

    background_tasks.add_task(do_ingest)
    return {"status": "ingestion_started", "max_hearings": max_hearings}


@app.get("/api/topics")
async def list_topics():
    db = await get_db()
    topics = await db.execute_fetchall("SELECT id, name, description, created_at FROM topics ORDER BY created_at DESC")

    result = []
    for t in topics:
        # Get processing status
        total = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ?", (t[0],)
        )
        done = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ? AND status IN ('done', 'skipped', 'error')", (t[0],)
        )
        processing = await db.execute_fetchall(
            "SELECT COUNT(*) FROM processing_status WHERE topic_id = ? AND status = 'processing'", (t[0],)
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
async def create_topic(topic: TopicCreate, background_tasks: BackgroundTasks):
    db = await get_db()

    # Check if topic exists
    existing = await db.execute_fetchall("SELECT id FROM topics WHERE name = ?", (topic.name,))
    if existing:
        await db.close()
        raise HTTPException(status_code=409, detail="Topic already exists")

    cursor = await db.execute(
        "INSERT INTO topics (name, description) VALUES (?, ?)",
        (topic.name, topic.description)
    )
    topic_id = cursor.lastrowid
    await db.commit()
    await db.close()

    # Start background processing
    async def do_process():
        await process_topic(topic_id, topic.name, topic.description)

    background_tasks.add_task(do_process)

    return {"id": topic_id, "name": topic.name, "status": "processing_started"}


@app.post("/api/topics/{topic_id}/rescan")
async def rescan_topic(topic_id: int, background_tasks: BackgroundTasks):
    """Re-scan unprocessed hearings for an existing topic."""
    db = await get_db()
    topic = await db.execute_fetchall("SELECT id, name, description FROM topics WHERE id = ?", (topic_id,))
    if not topic:
        await db.close()
        raise HTTPException(status_code=404, detail="Topic not found")

    tid, tname, tdesc = topic[0]
    await db.close()

    async def do_process():
        await process_topic(tid, tname, tdesc or "")

    background_tasks.add_task(do_process)
    return {"status": "rescan_started", "topic": tname}


@app.delete("/api/topics/{topic_id}")
async def delete_topic(topic_id: int):
    db = await get_db()
    await db.execute("DELETE FROM quotes WHERE topic_id = ?", (topic_id,))
    await db.execute("DELETE FROM processing_status WHERE topic_id = ?", (topic_id,))
    await db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    await db.commit()
    await db.close()
    return {"status": "deleted"}


@app.get("/api/quotes")
async def list_quotes(
    topic_id: Optional[int] = None,
    hearing_id: Optional[int] = None,
    speaker: Optional[str] = None,
    search: Optional[str] = None,
    date_after: Optional[str] = None,
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

    where = " AND ".join(conditions) if conditions else "1=1"

    # Get total count
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
    """Get unique speakers across all quotes."""
    db = await get_db()
    rows = await db.execute_fetchall("""
        SELECT DISTINCT speaker, COUNT(*) as quote_count
        FROM quotes
        GROUP BY speaker
        ORDER BY quote_count DESC
    """)
    await db.close()
    return [{"name": r[0], "quote_count": r[1]} for r in rows]


@app.get("/api/status")
async def get_status():
    """Get overall system status."""
    db = await get_db()
    hearings = await db.execute_fetchall("SELECT COUNT(*) FROM hearings")
    transcripts = await db.execute_fetchall("SELECT COUNT(*) FROM hearings WHERE transcript_fetched = 1")
    topics = await db.execute_fetchall("SELECT COUNT(*) FROM topics")
    quotes = await db.execute_fetchall("SELECT COUNT(*) FROM quotes")
    processing = await db.execute_fetchall("SELECT COUNT(*) FROM processing_status WHERE status = 'processing'")
    await db.close()

    return {
        "hearings_total": hearings[0][0],
        "hearings_with_transcripts": transcripts[0][0],
        "topics_count": topics[0][0],
        "quotes_count": quotes[0][0],
        "currently_processing": processing[0][0],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8347)
