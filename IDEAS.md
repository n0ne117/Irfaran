# Ideas

Things that are wanted, half-answered, or deliberately postponed. Not a
roadmap and not a promise — a roadmap invites dates, and this is a list of
open questions with what is already known about each, so that picking one up
does not mean re-deriving the investigation behind it.

Anything actually decided lives in `CHANGELOG.md`. Anything actually broken
should be an issue, not an entry here.

---

## Trail colouring strength by zoom

**Wanted:** tracks that read well at every zoom without touching a slider.

The colouring strength is one number (default 50%) applied at every zoom, and
no single number is right everywhere. At z14 half strength is correct — full
strength drowns the streets underneath. At z10 half strength is too weak: what
is on screen there is mostly long single-pass routes, which the ember ramp
draws dark red, and at half strength over a dark basemap they read as dust.
There is no basemap detail to protect at that zoom anyway.

Measured, on a real archive, at z10: at 50% the routes are muted brick-red; at
100% they are vivid orange with a white core. Same tiles, same fog — only the
multiplier changed.

**The fix, if wanted:** `heatFade()` in `web/src/map.ts` is already a zoom
interpolation, so the strength can rise as the map zooms out — roughly 50% at
z14 through to 100% by z10. A few lines, no re-render, no new setting.

**Two things ruled out with evidence,** so they are not worth revisiting:

- *Fog veiling the trails.* Fog is drawn on top and is 100% cleared along a
  track at z12 and closer, but only 41% at z7 and 65% at z10 — a compelling
  story. Widening the cleared corridor to match the dilated trail changed the
  image almost not at all. Erosion spreads the most-cleared value it can find,
  and at z10 even that is only 65% cleared, so there is nothing fully clear to
  spread.
- *Folding fog by "any child visited ⇒ visited" instead of by averaging.* A
  better-argued idea — "have I been here" is an existence question, not an
  average — and it also changed the image almost not at all.

## Live ingest re-renders the whole day, every time

Found while testing the History tab, and not caused by it. A single Overland
fix reported **284 tiles touched**, because a live source appends to one event
per day: extending the LineString means re-stamping the whole line, so every
new fix costs a render of everything walked so far that day. Early in the day
that is cheap and by evening it is minutes, which is why three test fixes in a
row appeared to hang the server.

Two ways out, neither started:

- **Stamp only the new segment.** The tiles the appended points cover, rather
  than the tiles the whole event covers. Correct and much smaller, but the
  event's raster contribution and its geometry then have to stay in step
  through undo and rebuild, which is the part to get right.
- **Defer it.** A live fix marks tiles pending and returns; the render happens
  on a timer. Cheap to build on the existing pending-render queue, at the cost
  of a fix not appearing on the map immediately.

Worth measuring how long a whole-day render actually takes before choosing -
it is fine at breakfast and the problem is only at the end of a long day.

**Partly overtaken in 0.18.0.** With the review gate on, a live source is not
rendered per batch at all: the fixes wait in the holding pen and the whole-day
re-stamp happens once, when the day is accepted. The cost is unchanged and the
number of times it is paid went from one per batch the phone posted to one per
day. Both routes above are still worth having - the gate can be switched off,
and a long day accepted in one go still pays the full re-stamp - but neither is
urgent now.

## Review, the rest of it

The holding pen landed in 0.18.0. What was deliberately left out:

- **No bulk accept.** A button that accepts everything is the gate with extra
  steps, and the first thing anybody with a backlog would reach for. If a
  backlog turns out to be the normal state rather than a sign the gate is
  wrong for that source, this changes - but it should be measured, not
  assumed.
- **No rules.** "Accept anything under 500 m", "always discard anything inside
  the home radius" - each is plausible and each is a way of not looking at the
  data, which is what the feature exists to make you do.
- **Trimming is by point, not by place.** Two handles over the fix list, with
  the distance removed shown as you drag. Dragging on the map itself - grab
  the end of the line and pull it back - would be better and is a lot more
  code.
- **No split.** A batch can have parts left out but not be cut into two
  separate tracks. Nothing has wanted it yet.
- **Nothing prunes the pen.** A source left gated and never reviewed grows one
  row a day, a few hundred KB each. Harmless for a year, and the badge is
  hard to miss, but there is no cap and no reminder beyond the badge.

## Search: the rest of it

Built: coordinates (0.17.10), your own pins and tracks (0.17.12), suggestions as
you type (0.17.15), per-kind toggles (0.17.16), Plus Codes (0.17.17), a settings
page of its own (0.17.18) and the basemap's own names (0.17.19).

What is left:

1. **A pasted map URL.** What people paste is often
   `https://.../maps/@27.74367,-15.58338,15z` rather than the bare pair.
   Deliberately not done: pulling the first coordinate-looking pair out of
   arbitrary text invites false positives, and it wants its own tests rather
   than being smuggled into the parser.

### What the gazetteer actually cost

Estimated first, then measured, and the estimates were wrong in both directions.

**Place names.** 478,382 tiles at z10, **2.5 minutes**, 1,069,426 names. The
guess was 1.4 million tiles and "minutes across seven cores" - it is a quarter
of that on one core, because more than half of every zoom is empty ocean and the
archive stores those once.

**Points of interest.** Built: **118,703,792 tiles in about 95 minutes on one
core, 32,837,529 names, 2.9 GB**. The estimate was 2.7-10.7 hours across seven
cores and 10-15 million rows - so quicker than feared and more than twice as many
names. `min_zoom` is advisory: a z15 tile carries POIs marked `min_zoom: 16`,
which is the only reason a restaurant is reachable at all when the archive stops
at z15. Only 12% of what that scan read was a repeat, against 55% for places,
because labels at the deepest zoom are not buffered outward the same way.

**Two things only real use found.** A place is stored under its local name, so
`Wien` could never answer "Vienna" - the English name is now indexed as a second
row where it differs, which added 283,506 rows to a million. And ranking by text
relevance alone cannot tell a city from a shop named after it: around Vienna
there are enough businesses called Vienna-something to fill the window and push
the city out of it. Places and points of interest are now asked for separately,
places first, ordered by exact name and then by population - which is kept in a
side table so that adding it did not mean recreating the FTS table and reading
118 million tiles again.

**What the sampling changed.** Labels are buffered into neighbouring tiles, so
55% of what a scan reads is a repeat - the same pub four times around Caorle.
Dropping them needs a memory of recent keys, and 400,000 of them caught 99.8% of
the duplicates in the real build; query time collapses the rest. Holding every
key of a fifteen-million-row scan was never going to fit on the T490.

### If the basemap is replaced

The gazetteer is derived, so a new planet build means extracting again - the same
cost as the first time, with no useful shortcut. PMTiles archives do not diff,
and finding what changed would mean reading all 135 million blobs, which is the
rebuild.

That is fine if it is designed for from the start:

- **Build beside the old one and swap at the end.** The existing gazetteer keeps
  answering while the new one is built, and a build that fails or is stopped
  leaves a working index rather than a hole.
- **Record which archive it came from** - filename, date, size. Without it a
  gazetteer silently goes stale and starts offering places that have moved, and
  there is no way to tell whether a rebuild is owed.
- Re-downloading 137 GB dwarfs the rebuild either way.

### Not blocking the person using it

A build runs for hours and must never be the reason an edit waits. The rule is
the opposite of locking: **manual work wins, the build yields.** Imports,
drawing, pin edits and the automatic sources all trigger renders, and a render
and a scan competing for eight cores means both crawl.

`renderq` already has every piece of this - a stop flag, finished work written
down so a resume does not repeat it, and a state anyone can poll - so the
gazetteer worker copies that pattern rather than inventing one. One background
worker at a time, renders first, the scan pausing and resuming on its own.

And its progress belongs on **In progress**, with the controls on the Search
page. Two views of the same work drift apart: that is exactly how an import came
to sit at 100% after it had finished, and how a drawing bar stuck at three
quarters.

**A limit worth knowing before extending this.** A track can only be searched by
year, because the year is the only date stored: `created_at` on an event is
`datetime.now()` at ingest, for every source, and the activity's own date
survives only as the layer it was filed under. Anything finer - "what did I do
on 11 June" - needs the per-fix timestamps kept somewhere, which is a schema
change and a rebuild, not a search feature.

## Snap drawing to real paths

Wanted as a drawing tool: trace a trail rather than approximate it freehand.

The installed basemap already carries a `roads` layer — confirmed by reading
the archive's own metadata, alongside `boundaries`, `water`, `places` and the
rest — and it is rendered, so `queryRenderedFeatures` can hand back path
geometry under the cursor with **no new data and no new container**. Two
useful levels:

- **Snap** each drawn point onto the nearest path within a few pixels, which
  removes the wobble and puts the line on the trail.
- **Follow one way** from a snapped point along that feature's own geometry to
  the next click, when both land on the same path.

What this cannot do is route across junctions: vector tiles clip geometry at
tile boundaries and carry no topology, so connectivity between segments is not
in there.

## BRouter, or another routing engine

Shelved deliberately, and cheaper than expected if it comes back.

- Segment files are 5°×5°, prebuilt weekly, and the one covering northern
  Italy and Austria is **189 MB** — noise beside a 137 GB basemap. 475 cover
  the world. MIT licensed. No build step; they are downloads.
- A routed line is just a LineString event, so fog clearing and trail drawing
  come free. The integration surface is small.

Against it:

- A third container, and a JVM one, on a stack somebody already had to fight
  through twice.
- A second dataset with its own lifecycle: which segments, downloading them,
  weekly staleness, and "you drew outside your segments". That is a second copy
  of the basemap-download machinery, which took several releases to get right
  the first time.
- It duplicates data already on disk. The basemap holds these paths; vector
  tiles simply discarded the topology routing needs.
- It fights half the use case. Fog-of-war drawing is "I walked this trail" and
  "I walked across this field" in equal measure.
- The tempting shortcut — calling a public routing API — sends where you are
  mapping to a third party, which is the one thing this project exists to
  avoid. Self-hosted or not at all.

Do the snapping above first, and only reach for this if snapping proves
insufficient.

## Places, extended

The Places page works — pins, labels, folders, tags, 30 m fog clearing on drop
— and was always meant to grow past that. What it grows into has not been
decided.

## Make the wide-zoom walk incremental

**Wanted:** an edit that costs what the edit is worth, rather than what the
archive is worth.

**Start from the current number.** An earlier version of this argument used
timings — 305 seconds for a six-job pass, the same two views going 142s → 324s
over one morning — to conclude the walk was inherently expensive. Those numbers
were a missing index on `blobs(x, y)`: every tile lookup scanned every blob of
its kind, 226 MB of it, three times per tile. With the index (0.17.6) the same
whole-view walk takes **8.8 s**, not 247.5. Any case made here has to start from
that figure.

**What remains true.** The walk still visits every native tile in the view,
however small the edit, because a parent tile is the maximum of its children and
cannot be built without them. The cost is still linear in the archive: roughly
3 ms a tile across 2,954 tiles today, and growing as the map fills in. At this
size it is seconds and nobody minds. Ten times the data is a minute a stroke.

**The idea:** rebuild an ancestor tile from its four children directly rather
than recomposing the whole view from the blob store, so an edit walks its own
fifteen ancestors instead of three thousand unrelated tiles.

**Why it is not done.** It makes derived tiles an input to their own
regeneration, which is what invariant 1 exists to prevent. It would need:

- A lossless way back from a rendered tile to its arrays. Fog is a boolean and
  trail is a pass count, both pushed through a colour ramp, so they are probably
  *not* recoverable from the PNG. Establish that before designing anything else:
  if it holds, the feature becomes "cache the arrays beside the tiles", which is
  a different feature with a disk cost.
- A verification pass — a rebuild from the event log compared byte for byte
  against the incrementally maintained pyramid.
- A known-good way back when the two disagree, which is a full rebuild.

**A warning from the attempt that was made.** The walk was also split into one
job per z10 subtree to spread it across cores. It worked and was byte-identical
to the single-pass walk, and it bought **1.38×** for 2.5× the total CPU — then
1.2× once the index landed, so it was reverted. Parallelising a query problem is
a poor trade, and the profile said so before the work began: 22 ms for a single
`execute` is not a compositing cost, it is a question the database cannot answer
efficiently. The patch is not kept; the measurements are the useful part.

## Statistics over the archive

**Wanted:** curated numbers out of the data already held — how many tracks, how
many pins, how many points.

Cheap, and cheaper than it first looked. The event log is complete and never
rewritten, so almost all of this is arithmetic over two tables:

- **Tracks** — one `events` row per imported track or drawn route, so counts
  split by `source` (Overland, GPX, intervals.icu, hand-drawn), by `op`, by
  `layers`, and by month of `created_at`. First and last recorded day come
  from the same column.
- **Pins** — `places`, already broken down by folder, by prominence, by tag or
  label, and by person through the people join.
- **Points** — `events.geometry` is GeoJSON **text**, not a packed blob, so
  `json_array_length(json_extract(geometry, '$.coordinates'))` counts a
  LineString's points in SQL with no decode pass. Point counts are a single
  query, not a background job.
- **Coverage** — distinct z14 tiles with data, per view and in total, which is
  the honest answer to "how much of the world have I been to". Already computed
  by `tiles_with_data()` for rendering.
- **Activity** — `history` gives imports, edits and renders over time;
  `trackers` gives per-device counts.

Distance is the one that needs real code: a haversine over consecutive
coordinate pairs, which is not a SQLite one-liner. Still a Python pass over
text measured in seconds, not minutes.

**The trap to get right:** `op` is `add | reveal | erase`, and an erase is a
composite-time subtract rather than a deletion. So raw sums over events answer
"what was recorded", not "what is on the map" — a statistic that says
"1,204 km walked" while the map shows less would be worse than no statistic.
Decide which question each number answers before writing the query.

The interesting figures are the derived ones — total distance, distinct tiles
visited, days with any data, longest gap, most-visited pin — and they are cheap
here precisely because the log is append-only.

## Colouring visited countries

Asked for, then skipped, and worth recording why: the basemap's `boundaries`
layer carries border **lines**, not country **polygons**. Filling a country
needs an area to fill, and there isn't one in the archive. It would mean
carrying country geometry separately.

## A second workout tracker

The tracker plumbing is behind a registry (`TRACKERS` in
`api/irfaran/trackers.py`) with intervals.icu as the only entry, precisely so
a second one is a client and a settings block rather than a redesign. Nothing
specific is planned.

## Where you have not been

Asked for as a thought exercise, kept because there is a common thread worth
writing down: **Irfaran is the only thing that knows where you have not
been.** Every other mapping tool records where you went. The absence is the
asset here, and almost nothing reads it yet. Nothing below is started.

### A route that maximises new ground

Compose the two entries above - snap-to-paths and a routing engine - with the
fog, and the objective function stops being distance or elevation and becomes
*new ground cleared per kilometre*: "a 12 km loop from here across as much
ground as possible that I have never covered".

Everything it needs is already here. The road graph is in the basemap and the
MVT reader exists; the coverage test is a lookup against blobs already on
disk. It is the one feature on this list that could not be lifted into another
application, because it needs both halves - and it turns a passive record into
something that decides where to go on a Sunday.

Cost is the honest question. Route search over a road graph with a
non-additive objective is not Dijkstra, and "a loop" makes it worse. Probably
wants a greedy or sampled answer rather than an optimal one, which is fine:
nobody needs the *best* loop.

### The hole finder

Everyone has a street ten minutes from their front door they have never once
walked down. That is computable: connected components over the fog channel,
filtered to holes fully enclosed by cleared ground. "Seventeen unvisited
pockets inside the area you have covered, largest 2.1 ha."

Cheap, and cheap for a good reason - it is morphology over an array that is
already exactly the right shape, at whatever zoom makes a city fit in memory.
Highest delight per line of code on this list.

### A first-visit map

The trail layer answers *how often*. Nothing answers *when first*. Same
events, same stamping loop, `min` instead of `+=`: another blob kind holding
the earliest timestamp per pixel, coloured by year.

What comes out is not a density map, it is the shape of a life expanding - the
childhood blob, the year the map jumps continent, the slow accretion around a
new flat. No new sources and no new data. And it inherits the rebuild
guarantee for free, because `min` is as order-independent as `sum`, which is
the reason it fits this architecture and a "most recent visit" map would not.

### Coverage against the basemap

Thirty-three million points of interest and a roads layer, offline. So: "you
have walked 34% of the streets in this district", computed from geometry
already held. `Colouring visited countries` above is the same idea at the one
scale where it is binary and therefore boring; a district fills slowly, which
is what makes a number worth showing.

### A synthetic life

Aimed at a real and recurring problem: this application cannot be
screenshotted. The repository is public, the website may show false data only,
and every release so far has shipped without a picture of the thing it added.

So generate an archive. An invented town, a home, a commute walked four
hundred times, three summers on a coast, a decade of expansion, a couple of
flights - enough structure that the fog reads as a life rather than as noise.
`irfaran demo` fills an empty instance with it, and the README and the website
get real screenshots of nobody, permanently.

No architectural weight at all - it writes events through the ordinary ingest
path and could live in the CLI beside `token`. It also doubles as the only
honest load-test fixture for the render queue, since the shape of real
movement is what makes a render expensive.

## A written spec for the event log

Not a feature. The event log is already a complete, portable, readable account
of everywhere somebody has been, and its meaning - not its schema, its
*meaning* - is currently defined only by the code that writes it. `db.py` has
the columns and `composite.py` has the semantics, which is enough to
re-implement it only if you can read Python.

That is a document with a fifty-year life expectancy in a repository whose
code has none. Worth writing down while the person who decided what `op` means
is still available to ask.

## Not doing: anything social

Sharing, comparing, following, streaks, leaderboards. Recorded as a decision
rather than an omission, because each one arrives sounding harmless and every
one of them requires this to stop being a private record on somebody's own
hardware. The same goes for notifications: a map that nags is a different
product.

## Not doing: an external geocoder

Nominatim, or any hosted geocoding API, for answering "where is Vienna".

It would work, it is easy, and it is the one thing this project exists not to do.
Every query would leave the machine, and a list of the places somebody looks up
is a better description of their life than the map itself - where they are going,
what they are planning, who they are visiting. Self-hosted Nominatim avoids the
leak and brings a planet-scale import and a second database to keep current,
which is a different project.

The offline gazetteer above is the same feature without the leak, and is the
route to take if "where is Vienna" is ever wanted.

## Not doing: per-vendor workout API sync

Dropped in favour of the tracker above. Asking one service that already holds
your history beats one integration per vendor.
