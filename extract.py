"""AI-powered quote extraction using Claude API."""

import asyncio
import json
import os
import re
import anthropic
from database import get_db

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def chunk_transcript(text, max_chars=12000):
    """Split transcript into manageable chunks, trying to break at speaker boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    # Try to split at speaker turns (lines that look like "Mr. NAME." or "Senator NAME.")
    speaker_pattern = re.compile(
        r"^\s{4,}(Mr\.|Mrs\.|Ms\.|Dr\.|Senator|Representative|Chairman|Chairwoman|"
        r"Chairperson|Ranking Member|The Chairman|The Chairwoman|General|Admiral|"
        r"Secretary|Director|Commissioner|Judge|Ambassador|Governor|Mayor|"
        r"Voice|Witness)\s",
        re.MULTILINE
    )

    positions = [m.start() for m in speaker_pattern.finditer(text)]
    if not positions:
        # Fallback: split by paragraphs
        while text:
            chunk = text[:max_chars]
            # Try to break at a paragraph boundary
            last_break = chunk.rfind("\n\n")
            if last_break > max_chars * 0.5:
                chunk = text[:last_break]
            chunks.append(chunk.strip())
            text = text[len(chunk):].strip()
        return chunks

    current_start = 0
    for pos in positions:
        if pos - current_start >= max_chars:
            # Find the last speaker turn before the limit
            chunk = text[current_start:pos].strip()
            if chunk:
                chunks.append(chunk)
            current_start = pos

    # Add remaining text
    remaining = text[current_start:].strip()
    if remaining:
        chunks.append(remaining)

    return chunks


async def screen_hearing_relevance(topic_name, topic_description, hearing_title, transcript_preview):
    """Quick check if a hearing is likely relevant to a topic before full extraction."""
    prompt = f"""Is this congressional hearing likely to contain substantive discussion of "{topic_name}" ({topic_description})?

HEARING TITLE: {hearing_title}

TRANSCRIPT PREVIEW (first ~2000 chars):
{transcript_preview[:2000]}

Answer with ONLY "yes" or "no". Say "yes" only if the hearing clearly covers this topic substantively, not just a passing mention."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip().lower()
        return answer.startswith("yes")
    except Exception:
        return True  # On error, process anyway


async def extract_quotes_for_topic(topic_name, topic_description, hearing_id, transcript_text, hearing_title, hearing_date):
    """Use Claude to extract relevant quotes from a transcript for a given topic."""

    # Pre-screen: check if hearing is relevant at all
    is_relevant = await screen_hearing_relevance(
        topic_name, topic_description, hearing_title, transcript_text[:2000]
    )
    if not is_relevant:
        return []

    chunks = chunk_transcript(transcript_text)
    all_quotes = []

    for i, chunk in enumerate(chunks):
        if len(chunk.strip()) < 100:
            continue

        prompt = f"""Analyze this congressional hearing transcript excerpt. Extract quotes that are DIRECTLY and SUBSTANTIVELY about the topic.

TOPIC: {topic_name}
TOPIC SCOPE: {topic_description}

HEARING: {hearing_title} ({hearing_date})

TRANSCRIPT EXCERPT (part {i+1}/{len(chunks)}):
---
{chunk}
---

RULES:
- Only extract quotes where the speaker is substantively discussing {topic_name} as a policy matter
- Generic mentions of technology, education backgrounds, military service, etc. do NOT count
- The quote must convey a policy position, factual claim, concern, or recommendation about the topic
- Return 0-3 high-quality quotes per excerpt. Empty array [] is perfectly fine.

JSON format for each quote:
{{"speaker": "Name (Role)", "quote": "exact text, 1-4 sentences", "context_before": "brief context", "context_after": "what followed", "relevance": "why this matters for {topic_name}"}}

Return ONLY a JSON array."""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = response.content[0].text.strip()
            if response_text.startswith("```"):
                response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
                response_text = re.sub(r"\s*```$", "", response_text)

            quotes = json.loads(response_text)
            if isinstance(quotes, list):
                all_quotes.extend(quotes)

        except (json.JSONDecodeError, Exception) as e:
            print(f"  Error processing chunk {i+1} for topic '{topic_name}': {e}")
            continue

        await asyncio.sleep(0.2)

        # Cap at 15 quotes per hearing to keep results focused
        if len(all_quotes) >= 15:
            break

    return all_quotes[:15]


async def process_topic_for_hearing(topic_id, topic_name, topic_description, hearing_id):
    """Process a single hearing for a topic and store results."""
    db = await get_db()

    # Check if already processed
    row = await db.execute_fetchall(
        "SELECT status FROM processing_status WHERE topic_id = ? AND hearing_id = ?",
        (topic_id, hearing_id)
    )
    if row and row[0][0] in ("done", "processing"):
        await db.close()
        return 0

    # Mark as processing
    await db.execute("""
        INSERT OR REPLACE INTO processing_status (topic_id, hearing_id, status)
        VALUES (?, ?, 'processing')
    """, (topic_id, hearing_id))
    await db.commit()

    # Get hearing data
    hearing = await db.execute_fetchall(
        "SELECT title, date_held, transcript_text FROM hearings WHERE id = ?",
        (hearing_id,)
    )
    if not hearing or not hearing[0][2]:
        await db.execute("""
            UPDATE processing_status SET status = 'skipped'
            WHERE topic_id = ? AND hearing_id = ?
        """, (topic_id, hearing_id))
        await db.commit()
        await db.close()
        return 0

    title, date_held, transcript = hearing[0]

    try:
        quotes = await extract_quotes_for_topic(
            topic_name, topic_description, hearing_id, transcript, title, date_held
        )

        for q in quotes:
            await db.execute("""
                INSERT INTO quotes (hearing_id, topic_id, speaker, quote_text, context_before, context_after, relevance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                hearing_id, topic_id,
                q.get("speaker", "Unknown"),
                q.get("quote", ""),
                q.get("context_before", ""),
                q.get("context_after", ""),
                q.get("relevance", ""),
            ))

        await db.execute("""
            UPDATE processing_status SET status = 'done'
            WHERE topic_id = ? AND hearing_id = ?
        """, (topic_id, hearing_id))
        await db.commit()
        await db.close()
        return len(quotes)

    except Exception as e:
        print(f"Error extracting quotes for topic {topic_name}, hearing {hearing_id}: {e}")
        await db.execute("""
            UPDATE processing_status SET status = 'error', error = ?
            WHERE topic_id = ? AND hearing_id = ?
        """, (str(e), topic_id, hearing_id))
        await db.commit()
        await db.close()
        return 0


async def process_topic(topic_id, topic_name, topic_description=""):
    """Process all hearings for a given topic."""
    db = await get_db()
    hearings = await db.execute_fetchall(
        "SELECT id FROM hearings WHERE transcript_fetched = 1 ORDER BY date_held DESC"
    )
    await db.close()

    total_quotes = 0
    for i, (hearing_id,) in enumerate(hearings):
        n = await process_topic_for_hearing(topic_id, topic_name, topic_description, hearing_id)
        total_quotes += n
        if (i + 1) % 5 == 0:
            print(f"  Topic '{topic_name}': processed {i+1}/{len(hearings)} hearings, {total_quotes} quotes found")

    print(f"  Topic '{topic_name}': finished. {total_quotes} quotes from {len(hearings)} hearings.")
    return total_quotes


async def get_quote_detail(quote_id):
    """Get detailed information about a quote including AI-generated context summary."""
    db = await get_db()
    row = await db.execute_fetchall("""
        SELECT q.*, h.title as hearing_title, h.date_held, h.chamber, h.committee,
               h.package_id, t.name as topic_name
        FROM quotes q
        JOIN hearings h ON q.hearing_id = h.id
        JOIN topics t ON q.topic_id = t.id
        WHERE q.id = ?
    """, (quote_id,))
    await db.close()

    if not row:
        return None

    r = row[0]
    return {
        "id": r[0],
        "hearing_id": r[1],
        "topic_id": r[2],
        "speaker": r[3],
        "quote_text": r[4],
        "context_before": r[5],
        "context_after": r[6],
        "relevance": r[7],
        "hearing_title": r[9],
        "date_held": r[10],
        "chamber": r[11],
        "committee": r[12],
        "package_id": r[13],
        "topic_name": r[14],
    }
