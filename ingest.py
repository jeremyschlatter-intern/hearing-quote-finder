"""Hearing ingestion pipeline - fetches hearing data and transcripts from GovInfo."""

import asyncio
import httpx
import os
import re
from database import get_db

GOVINFO_API_KEY = os.environ.get("CONGRESS_API_KEY", "")
GOVINFO_BASE = "https://api.govinfo.gov"


async def fetch_hearing_list(since="2025-01-01T00:00:00Z", max_hearings=100):
    """Fetch list of 119th Congress hearings from GovInfo."""
    hearings = []
    url = (
        f"{GOVINFO_BASE}/collections/CHRG/{since}"
        f"?pageSize=50&offsetMark=*&congress=119&api_key={GOVINFO_API_KEY}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        while url and len(hearings) < max_hearings:
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    break
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    if attempt < 2:
                        print(f"  Retry {attempt+1} for hearing list: {e}")
                        await asyncio.sleep(2 ** attempt)
                    else:
                        raise

            data = resp.json()

            for pkg in data.get("packages", []):
                hearings.append({
                    "package_id": pkg["packageId"],
                    "title": pkg["title"],
                    "date_issued": pkg["dateIssued"],
                    "doc_class": pkg.get("docClass", ""),
                })

            next_page = data.get("nextPage")
            if next_page and len(hearings) < max_hearings:
                # nextPage URL already has params, just add api_key
                sep = "&" if "?" in next_page else "?"
                url = f"{next_page}{sep}api_key={GOVINFO_API_KEY}"
            else:
                break

            await asyncio.sleep(0.3)  # Rate limiting

    return hearings[:max_hearings]


async def fetch_hearing_summary(package_id):
    """Fetch detailed summary for a hearing package."""
    url = f"{GOVINFO_BASE}/packages/{package_id}/summary?api_key={GOVINFO_API_KEY}"
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                else:
                    raise


async def fetch_transcript_text(package_id):
    """Download and parse the transcript HTML from GovInfo."""
    url = f"{GOVINFO_BASE}/packages/{package_id}/htm?api_key={GOVINFO_API_KEY}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                break
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                else:
                    raise
        html = resp.text

    # Extract text from <pre> tags (GovInfo hearing format)
    text = extract_text_from_html(html)
    return text


def extract_text_from_html(html):
    """Extract clean text from GovInfo hearing HTML."""
    # Most GovInfo hearing transcripts are in <pre> tags
    pre_match = re.search(r"<pre>(.*?)</pre>", html, re.DOTALL | re.IGNORECASE)
    if pre_match:
        text = pre_match.group(1)
    else:
        # Fallback: strip all HTML tags
        text = re.sub(r"<[^>]+>", "", html)

    # Clean up
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", "", text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    return text.strip()


def parse_chamber(doc_class):
    """Parse chamber from document class."""
    if doc_class.startswith("H"):
        return "House"
    elif doc_class.startswith("S"):
        return "Senate"
    elif doc_class.startswith("J"):
        return "Joint"
    return "Unknown"


async def store_hearings(hearings_data):
    """Store hearing metadata in the database."""
    db = await get_db()
    stored = 0
    for h in hearings_data:
        try:
            summary = await fetch_hearing_summary(h["package_id"])
            committee = ""
            if "committees" in summary:
                committees = summary.get("committees", [])
                if committees:
                    committee = committees[0].get("committeeName", "")
            elif "governmentAuthor2" in summary:
                committee = summary.get("suDocClassNumber", "")

            # Extract held date
            date_held = h["date_issued"]
            held_dates = summary.get("heldDates", [])
            if held_dates:
                date_held = held_dates[0]

            await db.execute("""
                INSERT OR IGNORE INTO hearings (package_id, title, date_held, chamber, committee)
                VALUES (?, ?, ?, ?, ?)
            """, (
                h["package_id"],
                h["title"],
                date_held,
                parse_chamber(h.get("doc_class", "")),
                committee,
            ))
            stored += 1
        except Exception as e:
            print(f"Error storing hearing {h['package_id']}: {e}")

    await db.commit()
    await db.close()
    return stored


async def fetch_and_store_transcript(hearing_id, package_id):
    """Fetch transcript for a hearing and store it."""
    db = await get_db()
    try:
        text = await fetch_transcript_text(package_id)
        if text:
            await db.execute("""
                UPDATE hearings SET transcript_text = ?, transcript_fetched = 1
                WHERE id = ?
            """, (text, hearing_id))
            await db.commit()
            return True
        else:
            await db.execute("""
                UPDATE hearings SET transcript_fetched = -1 WHERE id = ?
            """, (hearing_id,))
            await db.commit()
            return False
    except Exception as e:
        print(f"Error fetching transcript for {package_id}: {e}")
        await db.execute("""
            UPDATE hearings SET transcript_fetched = -1 WHERE id = ?
        """, (hearing_id,))
        await db.commit()
        return False
    finally:
        await db.close()


async def ingest_hearings(max_hearings=60):
    """Main ingestion pipeline: fetch hearing list, store metadata, fetch transcripts."""
    print("Fetching hearing list from GovInfo...")
    hearings_data = await fetch_hearing_list(max_hearings=max_hearings)
    print(f"Found {len(hearings_data)} hearings")

    print("Storing hearing metadata...")
    stored = await store_hearings(hearings_data)
    print(f"Stored {stored} hearings")

    # Fetch transcripts for hearings that don't have them yet
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, package_id FROM hearings WHERE transcript_fetched = 0"
    )
    await db.close()

    print(f"Fetching {len(rows)} transcripts...")
    success = 0
    for i, row in enumerate(rows):
        ok = await fetch_and_store_transcript(row[0], row[1])
        if ok:
            success += 1
        if (i + 1) % 10 == 0:
            print(f"  Progress: {i+1}/{len(rows)} ({success} successful)")
        # Rate limiting
        await asyncio.sleep(0.5)

    print(f"Fetched {success}/{len(rows)} transcripts successfully")
    return success


if __name__ == "__main__":
    from database import init_db
    asyncio.run(init_db())
    asyncio.run(ingest_hearings())
