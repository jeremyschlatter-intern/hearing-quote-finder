# After-Action Report: Hearing Quote Finder

## What I Built

A web application that helps congressional staffers find relevant quotes from recent hearing transcripts based on topics they care about. The tool:

1. **Ingests 60 hearing transcripts** from the 119th Congress via the GovInfo API
2. **Lets users enter topics** (e.g., "Artificial Intelligence", "Defense Spending")
3. **Uses AI to extract relevant quotes** from all transcripts for each topic
4. **Displays a reverse-chronological feed** of quotes with filtering, search, and export

The app runs as a Python/FastAPI backend with a single-page HTML frontend.

## Process and Obstacles

### 1. Finding the Right Data Source

**Challenge:** The project spec asked about getting transcripts "via an api? from youtube?" — neither route was straightforward.

**What I tried:**
- **Congress.gov API**: Has hearing metadata but not full transcripts
- **GovInfo API**: Has full official transcripts in HTML format

**Resolution:** GovInfo turned out to be the right source. It provides complete transcripts for 577 hearings in the 119th Congress, in a parseable HTML format with full speaker attributions. I built a pipeline that fetches hearing lists, downloads transcripts, and parses them into clean text.

**Key detail:** GovInfo transcripts are in `<pre>` tags within HTML, with committee/member information structured in the headers. I wrote a parser that extracts committee names from the transcript headers since the API metadata didn't reliably include them.

### 2. Quote Extraction Quality

**Challenge:** The initial AI extraction was too loose — matching "data analytics technology" as relevant to "Artificial Intelligence", or any mention of someone's educational background as relevant to "Education Policy."

**What I tried:**
- First attempt: Send each transcript chunk to Claude Haiku with a basic prompt asking for relevant quotes. Result: 113 quotes with many tangential matches.
- Second attempt: Tightened the prompt with explicit criteria (quote must be "substantively" about the topic, not just a passing mention). Better, but still noisy.
- Third attempt: Added a **two-stage pipeline** — first a quick relevance screening call to check if the hearing is even about the topic, then full extraction only for relevant hearings.

**Resolution:** The two-stage approach (pre-screen + strict extraction) dramatically improved quality. It also saved API costs by skipping irrelevant hearings entirely. I also added a cap of 15 quotes per hearing per topic to prevent one massive hearing (like DoD Appropriations, which had 103 initial matches) from dominating the feed.

### 3. Processing Speed and Background Tasks

**Challenge:** Processing 60 hearings x 3 topics = 180+ API calls to Claude, each involving transcript chunks. This takes several minutes, and users shouldn't have to wait.

**Resolution:** Used FastAPI's background tasks to process asynchronously. The frontend polls every 8 seconds and shows a real-time progress indicator ("Scanning hearings 24/60"). Quotes appear in the feed as they're found.

### 4. Date Filter Bug

**Challenge:** The date filter appeared to work but actually wasn't applied — the old server process was cached in memory while I was editing code.

**What happened:** After updating the backend to add `date_after` parameter support, I tried to restart the server, but the old process (PID 39300) held onto port 8347. My kill commands silently failed because background task management created new processes that couldn't bind to the port.

**Resolution:** Had to use `lsof -ti :8347 | xargs kill -9` to find and kill the actual process holding the port. This was confirmed by the DC review agent catching the bug.

### 5. Server-Side CSV Export

**Challenge:** The initial CSV export only exported quotes loaded in the browser (typically 30 of 200+). A staffer expecting to export all matching quotes would get an incomplete file.

**Resolution:** Added a server-side `/api/export` endpoint that generates a complete CSV with all matching quotes, respecting the same filters (topic, search, date) as the UI.

## Team Structure

I used two types of agent teammates:

1. **DC Reviewer Agent** (simulating Daniel Schuman): Reviewed the app twice, providing detailed feedback from a Hill staffer's perspective. Key contributions:
   - Identified copy/export as the #1 missing workflow feature
   - Caught the date filter bug
   - Pointed out that the CSV export only included loaded quotes
   - Suggested committee names should be more prominent
   - Recommended topic descriptions for better AI extraction

2. **Research Agents**: Used for initial API exploration and codebase analysis.

## Final Feature Set

| Feature | Description |
|---------|-------------|
| Hearing ingestion | 60 hearings from 119th Congress via GovInfo API |
| Topic management | Add/remove topics with optional descriptions |
| AI extraction | Two-stage pipeline: relevance screening + quote extraction |
| Quote feed | Reverse-chronological, filterable by topic, date, search |
| Quote detail | Click to see context, relevance explanation, source link |
| Copy workflow | Copy button on cards, "Copy quote" and "Copy for memo" in modal |
| CSV export | Server-side export of all matching quotes |
| Date filtering | Past month / 3 months / 6 months / year / all |
| Search | Across quotes, speakers, hearing titles, and committee names |
| Suggested topics | Quick-add chips with pre-written descriptions |
| Real-time updates | Live progress indicator during processing |

## What I Would Do Next

If I had more time or could contact people:

1. **More hearings**: Ingest all 577 available 119th Congress hearings (currently capped at 60)
2. **Speaker identification**: Parse party and state from transcript headers for better attribution
3. **Speaker filter**: Wire up the existing `/api/speakers` endpoint as a sidebar filter
4. **Scheduled re-ingestion**: Automatically fetch new hearings as they're published
5. **YouTube transcripts**: For very recent hearings not yet on GovInfo, fall back to YouTube auto-captions
6. **User accounts**: Multi-user support with saved topic preferences

## Technical Stack

- **Backend**: Python 3.9, FastAPI, aiosqlite, httpx
- **AI**: Claude Haiku 4.5 (via anthropic SDK) for cost-effective extraction
- **Data**: GovInfo API for hearing transcripts, SQLite for storage
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **Port**: 8347 (chosen to avoid conflicts with other projects on shared machine)
