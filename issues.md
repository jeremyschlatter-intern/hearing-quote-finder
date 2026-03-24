# Hearing Quote Finder - Issues

## Open

### 1. Ingest more hearings
Only 60 of ~574 available hearings are ingested (~10%). Should ingest all or most for comprehensive coverage.

## Closed

### 2. Date filter breaks the app
Selecting certain date ranges (e.g., "Past 6 months") caused the app to fail, and it didn't recover when switching back to "Past year".
**Root cause:** `feed.innerHTML = ''` destroyed the `emptyState` div, then `getElementById('emptyState')` returned null and crashed on `.style` access. The 8-second poll kept retriggering the crash.
**Fix:** Recreate the emptyState div when clearing the feed, and look it up after clearing.
