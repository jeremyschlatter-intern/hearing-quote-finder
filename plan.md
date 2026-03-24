# Hearing Quote Finder - Implementation Plan

## Problem
Congressional staffers need to efficiently find relevant quotes from recent hearing transcripts based on topics they or their member care about. Currently this requires manually reading through lengthy transcripts.

## Solution
A web application that:
1. Ingests transcripts from recent congressional hearings (via GovInfo API)
2. Lets staffers enter topics of interest
3. Uses Claude AI to extract relevant quotes from all recent hearing transcripts
4. Presents quotes in a clean reverse-chronological feed with filtering

## Data Source
- **GovInfo API** (`api.govinfo.gov`): Full official transcripts for 119th Congress hearings
  - 301+ hearings available, covering 2025-2026
  - Full HTML transcripts with speaker attributions
  - Metadata: title, date, committee, chamber

## Architecture

### Backend (Python/FastAPI)
- `server.py` - FastAPI app with endpoints:
  - `GET /api/hearings` - list ingested hearings
  - `GET /api/topics` - list user topics
  - `POST /api/topics` - add a new topic (triggers quote extraction)
  - `DELETE /api/topics/{id}` - remove a topic
  - `GET /api/quotes` - get quotes feed (with topic filter param)
  - `GET /api/quotes/{id}` - get quote detail with surrounding context + AI summary
- `ingest.py` - Hearing ingestion pipeline:
  - Fetch hearing list from GovInfo
  - Download and parse transcripts (HTML → structured text)
  - Store in SQLite
- `extract.py` - AI quote extraction:
  - Send transcript chunks + topic to Claude
  - Parse and store extracted quotes with speaker, context, relevance

### Database (SQLite)
- `hearings` - hearing metadata
- `transcripts` - full transcript text, chunked by speaker turns
- `topics` - user-defined topics
- `quotes` - extracted quotes with hearing_id, topic_id, speaker, text, context

### Frontend (static HTML/CSS/JS)
- Single `index.html` file served by FastAPI
- Professional, clean design (appropriate for Hill staff)
- Components:
  - Topic sidebar: add/remove topics
  - Main feed: reverse-chronological quotes
  - Filter bar: by topic, hearing, speaker
  - Quote cards: speaker, snippet, hearing name, date
  - Detail modal: full quote + surrounding context + AI summary

## Key Design Decisions
1. **Pre-process on topic creation**: When a topic is added, process all hearings. Cache results.
2. **Chunked processing**: Break transcripts into ~4000-token chunks for Claude processing.
3. **Speaker extraction**: Parse transcripts to identify individual speakers and their statements.
4. **Background processing**: Quote extraction happens async; frontend polls for progress.

## Tech Stack
- Python 3, FastAPI, uvicorn
- anthropic Python SDK
- SQLite (via aiosqlite)
- Vanilla HTML/CSS/JS (no build step)
