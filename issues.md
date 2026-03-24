# Hearing Quote Finder - Issues

## Open

### 1. Ingest more hearings
Only 60 of ~574 available hearings are ingested (~10%). Should ingest all or most for comprehensive coverage.

### 3. Consider removing quote caps
Currently capped at 3 quotes per chunk and 15 per hearing. This was added to prevent one massive hearing (DoD Appropriations) from dominating the feed. But it means we're silently dropping potentially relevant quotes. Consider removing or raising these limits, perhaps with a smarter deduplication or ranking approach instead.

### 4. Quotes should sort most-recent-first
Already implemented - `ORDER BY h.date_held DESC`. The April 2025 results shown are the most recent SASC hearings in the 60-hearing dataset. Will look more recent once more hearings are ingested (#1).

## Closed

### 2. Date filter breaks the app
Selecting certain date ranges (e.g., "Past 6 months") caused the app to fail, and it didn't recover when switching back to "Past year".
**Root cause:** `feed.innerHTML = ''` destroyed the `emptyState` div, then `getElementById('emptyState')` returned null and crashed on `.style` access. The 8-second poll kept retriggering the crash.
**Fix:** Recreate the emptyState div when clearing the feed, and look it up after clearing.
