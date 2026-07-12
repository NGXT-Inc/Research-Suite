"""Feed posting cadence policy (Feed_PRD.md).

The primary mechanism that decides when an agent posts is the posting *skill*
(the curation bar). This module holds only the **backup** nudge: a soft,
never-forced "consider posting" hint surfaced on the first page of
``feed.list`` when a main agent has gone a long stretch without posting while
new activity has accrued. The hint never blocks anything (unlike the reflection
hard-cap) — the feed is ungated by design.

The two anchors compared are time-since-last-post and events-since-last-post; the
nudge fires only when BOTH cross their thresholds, so a quiet project never nags
and a freshly-posted agent is never pestered. Conservative by design — a backup,
not a metronome.
"""

from __future__ import annotations

# Surface the nudge only once this many domain events have accrued since the last
# post (or since project start, if there has never been a post). Events are the
# accepted-mutation stream (experiments transitioning, claims changing, reviews
# resolving, etc.) — a proxy for "something worth talking about happened".
NUDGE_AFTER_EVENTS = 8

# ...and only once at least this much wall-clock time has passed since the last
# post. Together with the event count this keeps the nudge to genuinely extended
# silences rather than a busy ten minutes.
NUDGE_AFTER_HOURS = 6.0
