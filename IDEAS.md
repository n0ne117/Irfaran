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

**Largely overtaken in 0.18.8**, which cut a day into one event per
continuous stretch. Appending now re-stamps only the stretch that grew, not
the whole day - which is the first of the two routes below, arrived at from a
different direction. What is left of the problem is a single long unbroken
stretch, where the cost is still proportional to the whole of it.

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

## Multi-user: a family on one instance

**Wanted:** a household on one Irfaran. Members are created by an admin, each
gets their own token, and each uploads their own trips and tracks with it.
Single-user or multi-user is asked at setup and can be changed afterwards, with
the existing single user becoming the admin who can create members and issue
tokens. On the map, a control for whose ground to show: your own, everybody's,
or a chosen few.

The largest thing on this list. It is written up in some detail because the
expensive parts are not the obvious ones.

### What this is, and what it is not

**It is attribution and filtering. It is not access control.** Every member
would be able to see every other member's ground; the switch decides what is
*drawn*, not what is *permitted*. That is almost certainly what a family wants,
and it is the difference between a fortnight and a rewrite.

Read isolation - a member whose ground the others genuinely cannot see - is a
different feature and a much larger one. Reading is open by design: the map
needs no token, and tiles are static PNGs served from a path
(`/api/tiles/{theme}/{view}/{kind}/{z}/{x}/{y}.png`). Per-member fog would put
the member in that path, and anyone could then read anyone's by typing it. Real
isolation means authorising every tile read, which is the one request Irfaran
serves in bulk and the one it deliberately made as cheap as a file read.

It is also **not accounts**. No passwords, no sessions, no login screen. A
token is the credential, as it is today. Passwords bring session management,
resets, lockout and a login page to a self-hosted family instance that is
already behind whatever the household is behind.

### Why the shape of the archive makes this feasible

Three facts, all in the code today:

- **Blobs are already partitioned by source.** The primary key is
  `(kind, source, layer, x, y)` and `composite_tile` unions across every source
  for the layers a view asks for. Filtering by member is a `WHERE` clause in
  `_blobs`, once the blobs know who they belong to.
- **The event log is the truth and a rebuild is byte-identical.** Adding
  attribution to derived state does not need a migration that is clever - it
  needs a column and a rebuild. That is invariant 1 being useful rather than
  merely virtuous.
- **Writes already go through one gate.** One middleware checks one header on
  every `POST`, `PATCH`, `PUT` and `DELETE`. Turning "is this token right" into
  "which member is this" is a change in one function.

### Identity

Two tables and one habit change.

- `users(id, name, role, created_at, active)` where role is `admin | member`.
- `tokens(hash, user_id, label, created_at, last_used_at, revoked_at)`.

**Store a hash, not the token.** Today's single token sits in plain text in
`settings.api_token`, which is defensible for one shared secret that the setup
screen has to be able to show once. It stops being defensible at four: an admin
who can read back a member's token can post as them, and "issued once, shown
once, revoked and reissued if lost" is both safer and simpler to explain.
Comparison stays `secrets.compare_digest`, over the hash.

`IRFARAN_TOKEN` keeps working and becomes the admin's - it is the bootstrap
credential and the way back in when a database is moved to a new machine.

### Attribution on the way in

`events` gains `user_id`, null meaning *before there were users*. Every ingest
path stamps the authenticated member. Then the blobs need it too, or the fog
cannot be filtered: either widen `blobs.source` to carry it (`overland:3`) or
add a column and change the primary key. The second is cleaner and costs a
rebuild, which is exactly the operation this architecture guarantees.

**The trap, and it is a bad one:** the dedup index is
`UNIQUE(source, external_id)`. Two members who walked the same route together
and both import the GPX would have the second import silently swallowed as a
duplicate of the first. Dedup has to become per-member. The same applies to
live day keys - `track_id()` builds `live-{day}`, which two phones posting on
the same day would collide on - and to the holding pen, whose open-batch
lookup is `(source, day)`.

### The pyramid is what multiplies

Here are the numbers. The rendered tiles today are **1.2 GB in 276,896 PNGs**,
for 2 themes across 19 views (all, prehistory and 17 years). Per-member fog
multiplies that by members plus one: a family of four is roughly **6 GB and 1.4
million files**, and every render pass grows the same way.

And a *multiple choice* list is worse than linear. Arbitrary subsets of N
members is 2^N view sets. Pre-rendering that is out for any N worth having.

**The way out is to notice that fog and tracks are different questions.**

> Fog is collective. It is where *this household* has been, and it is the
> thing a family map is for. Tracks are individual: whose route that is, and
> when.

So: **one shared fog pyramid, and a trail pyramid per member**, drawn as
separate raster layers the browser can switch on and off. That makes the
control free - it is layer visibility, no render and no request - and it makes
the multiple-choice list free too, which is the version of the switch that
would otherwise have been impossible. It also suggests giving each member a
colour, which answers *whose track is that* at a glance and sidesteps a real
problem: the trail ramp encodes how many times a pixel was crossed, and
stacking two ramps does not sum. A colour per member says something true; a
stack of ember ramps says something false.

"Show me only the fog **I** cleared" is then the one question this does not
answer, and it is the rarer one. Render it on demand when a member asks for it,
cache it, and let it fall out of cache. Bounded by what is actually used
instead of by what is possible.

### Everywhere else identity leaks in

- **The holding pen.** Batches belong to a member; the badge counts what is
  waiting for *you*. An admin reviewing everybody's is a decision, not an
  obligation.
- **Live sources.** Each phone posts with its owner's token, which is the whole
  mechanism - Overland needs no change beyond the token in its settings.
- **Workout trackers.** The intervals.icu key is a single row in `settings`
  and would become per-member, which also means per-member sync state.
- **Pins are shared, and `people` is not `users`.** The `people` table records
  who was *there* - which includes people who have never touched Irfaran, and
  should keep doing so. A member might *link* to a person. Merging the two
  concepts would be the easy mistake and would take a grandmother who does not
  own a phone out of the record.
- **History** already records what happened; it would record who.
- **Export and backup** start carrying members, so an export from a
  multi-user instance restored onto a single-user one needs a defined answer
  rather than whatever falls out.

### Turning it on, and off

**On** is clean: there is exactly one candidate for who owns everything already
there. The existing user becomes admin and every existing event is theirs -
one `UPDATE`, no ambiguity.

**Off** is the interesting direction, and the honest answer is to refuse it
while other members have data. Anything else is a question with no good answer:
whose is that track now?

### If it is built, build it in this order

Each of these is shippable on its own and reversible.

1. **Users, tokens, attribution.** Members exist, tokens work, `user_id` is
   recorded on every event - and nothing filters yet. The map renders exactly
   as it does now, which is what makes this phase safe.
2. **The setup question and the admin page.** Create members, issue a token
   once, revoke one. Switch the mode.
3. **Per-member trail layers and the control.** The visible feature, and the
   first phase anybody would notice.
4. **Per-member fog, on demand.** Only if somebody actually asks for it.

Phase 1 is the one that touches everything: every ingest path, the dedup keys,
the holding pen, the blob key, and a rebuild. Nothing after it is difficult.

## Trips and vacations

**Wanted:** a log of trips, off by default and switched on in settings. A trip
gathers tracks and pins into one thing, with a title, dates, written notes with
some formatting, and photos. Its own icon on the map tools - a book rather than
another pin - and its own page, because it is only half a map feature.

**Off by default is part of the design, not politeness.** This is the first
thing that would give Irfaran a second kind of storage - photos are files, not
rows and not tiles - and an instance that does not want trips should pay
nothing for them: no icon, no tables touched, no directory made.

### What a trip is made of

`trips(id, name, started_on, ended_on, notes, created_at)` with join tables to
events and places, both ordered, since a trip has a shape: the flight out, four
walks, the drive home.

**Assembling one by date mostly works already.** `_meta_for` in
`ingest/common.py` writes `started_at` into `events.meta` whenever the source
carried timestamps, so "everything between 3 and 17 August" is a
`json_extract(meta, '$.started_at')` away over a table with a few thousand rows
in it. Pins have `date_from` and `date_to` already. What has no real date is a
hand-drawn stroke - it has an ingest time and a year - so those get added by
hand. Worth knowing before designing a date picker that promises more than it
can find.

**A trip is not a folder, and the overlap should be resisted.** Folders nest
and hide; a trip is dated and narrated. What a trip might reasonably offer is
*make a folder of these pins*.

### Formatting without a dependency

Store the text somebody typed, render it at display time, and support a small
subset - paragraphs, bold, italic, links, bullet lists. Rendered into DOM
nodes, never through `innerHTML`: the notes are the first free text in Irfaran
that gets displayed back, and an archive that renders arbitrary HTML is an
archive that can be made to do something else. A markdown library is a
dependency for the sake of syntax nobody asked for, in a project that
hand-wrote its own MVT and PMTiles readers and drew its own icons.

### Photos are the real decision

There are two entirely different answers, and choosing between them is most of
the design:

- **Files of its own.** A directory under the data dir, a row pointing at each
  file, thumbnails made with PIL, which is already here. This adds a storage
  class - and with it a question the backup has no answer for: the export is
  one file that means *everything worth keeping*, and photos would make it
  enormous. Something has to be decided rather than fallen into: excluded with
  the references kept, a separate photo archive, or an export that warns.
- **Somebody else's, by reference.** See the Immich entry below. Nothing is
  copied, nothing is stored, and the backup question never comes up.

Photos would also be the most sensitive thing Irfaran holds by a distance. The
repository is public and the rule about fixtures applies twice over: generated
images only, never a real one.

## Immich

**Wanted:** talk to a self-hosted [Immich](https://immich.app). Mainly to give
trips their photos, and beyond that to read the GPS in a library and offer pins
from it - compared against the pins already on the map, always as a suggestion,
and with every answer remembered so a rejected one never comes back.

It fits the one rule this project does not bend: Immich is self-hosted, so
nothing leaves the house. An API key in `SECRET_SETTINGS` beside
`intervals_api_key`, and the same shape as the tracker registry.

### Photos by reference

Store the Immich asset id and nothing else. Thumbnails are proxied through
Irfaran so the key never reaches the browser and the browser never needs to
reach Immich. That answers the storage question in the entry above by not
having it: no files, no directory, no backup problem, and the photos stay in
the application built to look after photos.

### Suggested pins, and remembering the answer

**The pattern already exists.** The one-off pin importer built exactly this in
0.18.5: rows staged in a table of their own, each one reviewed, saved or
discarded, and nothing touching the map until it is decided. A suggestion table
keyed by Immich asset id with `suggested | approved | rejected` is that again,
with two differences - it is not one-off, and *rejected* has to be permanent,
which is the ask.

Comparing against what is already there is a proximity test the pin importer
also already does when it collapses near-duplicates, so the threshold is
already a decided number rather than a new argument.

**Two things to get right before writing any of it:**

- **Never write a pin by itself.** The ask says manual and so does the rest of
  the application - the holding pen exists because nothing automatic should
  reach the map unlooked-at. A photo library would be the largest source of
  suggestions Irfaran has ever had, which makes the gate matter more, not less.
- **Sync incrementally and yield.** A library is tens of thousands of assets
  and only some carry GPS. Pull by `updatedAt` since the last run, and obey the
  rule already written down for the gazetteer: manual work wins, the background
  job pauses.

## A view for mobile devices

**Wanted:** it was out of scope and it is being used on a phone anyway.

**Some of it is already there.** There is a 46rem breakpoint: the sheets go
from a comfortable inset to nearly full screen, the two-column sections
collapse to one, and the scale bar takes itself away because the time bar and
the attribution already share that edge. The statistics page was measured at
375 px while it was being built - one column, nothing scrolling sideways.

**What is genuinely hard, in the order it will hurt:**

- **Drawing with a finger.** One-finger drag is how the map pans, and it is
  also how a stroke would be drawn. MapLibre's `dragPan` has to yield while a
  tool is armed and take the gesture back when it is put away, with two-finger
  pan and pinch still working throughout. This is the one that needs designing
  rather than adjusting.
- **The settings sheet.** Thirteen tabs in a horizontal scrolling strip is a
  filing cabinet through a letterbox. An accordion, or a two-level page that
  opens one section at a time.
- **The review sidebar.** Two trim handles over a fix list, and a gap list
  beside a map - the densest screen in the application, and the one most likely
  to be wanted on a phone, since reviewing yesterday's tracking is a thing
  somebody does on a sofa.
- **The time bar.** Nineteen stops across 375 pixels, which is the same
  question as the 2052 ruler and should be answered once for both.
- **iOS.** `100vh` against a disappearing address bar, and safe-area insets
  under the notch, both of which affect a full-screen map more than they affect
  a page.

**Cheap things worth doing first,** none of which need any of the above: a web
app manifest so it can be added to the home screen and open without browser
chrome, a `theme-color` that follows the interface theme, larger touch targets
on the map tools, and sheets that go properly full screen rather than nearly.

**And one thing to measure rather than assume:** the globe on a phone GPU.

**How to build it.** Not as one release. This is a pass over every panel, and
the honest order is what a phone is actually used for: looking first - the map,
the time bar, the pins - then reviewing, then the panels nobody edits on a
train. One panel per release, each shippable, none of them blocking the next.

## A written spec for the event log

Not a feature. The event log is already a complete, portable, readable account
of everywhere somebody has been, and its meaning - not its schema, its
*meaning* - is currently defined only by the code that writes it. `db.py` has
the columns and `composite.py` has the semantics, which is enough to
re-implement it only if you can read Python.

That is a document with a fifty-year life expectancy in a repository whose
code has none. Worth writing down while the person who decided what `op` means
is still available to ask.

## Equal Earth, when MapLibre can draw it

**Wanted:** the third projection, offered in Appearance and greyed out since
0.19.4.

Blocked upstream rather than unbuilt. MapLibre has exactly three projections -
`mercator`, `globe` and `vertical-perspective` - and they are compiled into its
shaders; there is no plugin interface to add a fourth. So this waits for
MapLibre, and the placeholder exists to say so rather than to promise anything.

Worth wanting for a reason beyond looks: Equal Earth is **equal-area**, and
Mercator is the projection that lies about exactly the quantity the statistics
page reports. The pixel weighting in `stats.py` exists to undo that distortion
in the arithmetic; an equal-area projection would make the picture agree with
the arithmetic by construction, so a cleared patch in Norway would look the
size it is measured to be.

If it ever lands: it is one more member of `MapProjection` in `web/src/map.ts`,
the `disabled` off the button, and nothing else - the choice is already stored
per browser and already read by `buildStyle`.

## Look at a track from the import log

**Wanted:** after a GPX or TCX import finishes, the name in the import log is a
link. Clicking it opens a new tab showing that one track on the map, drawn the
way the holding pen draws a route under review — the track alone, over the
basemap, with everything else out of the way.

Most of this exists. `review.ts` already frames one route on its own and hides
the fog and the trail rasters while it does it (`setArchiveVisible`), and the
pin import does the same trick for a staged pin. What is missing is a way to
address a track from outside: the import log knows what it created, but there
is nothing to link *to*.

So the work is roughly:

- **A URL that means one track.** Something like `#track=<event id>`, read on
  startup, before the normal view is applied. This is the real piece of design:
  Irfaran has no routing at all today, and one hash parameter is the thin end
  of a wedge that ends in a router.
- **An endpoint that returns one track's geometry** by event id. `/api/trails`
  is bounded by a viewport and a cap, which is the wrong shape for "this one,
  wherever it is".
- **The import log keeping ids.** It reports names and outcomes; the event ids
  are known at ingest and thrown away by the time the log is painted.

**The thing to decide first:** whether that tab is a *view* or a *page*. A view
is the whole app with one track showing, which is cheap and means the sidebar,
the time bar and the settings are all still there behind a route that is not
about them. A page is a stripped screen that shows a track and nothing else,
which is more code and reads better. The review sidebar is the precedent for
the first, and it is the cheaper of the two.

Would also give a natural answer to something else: a track that has already
landed cannot be looked at on its own today, which is the other half of
*editing a track that has already landed*, further up.

## Not doing: colouring visited countries on the map itself

Asked for, skipped, and worth recording twice - because the reason changed and
the answer did not.

It was skipped because it could not be done: the basemap's `boundaries` layer
carries border **lines**, not country **polygons**, and there was nothing in
the archive to fill. That stopped being true in 0.19.2, which ships Natural
Earth polygons for the statistics page.

It is now not wanted, which is a better reason. The borders drawn over the fog
already say which country a cleared patch is in, and the statistics page says
how much of each. Filling a whole country because somebody walked two hundred
metres into it is the overclaiming this map exists not to do - the fog is a
record of where you have been, at the resolution you were there, and a country
flooded with colour on the strength of one afternoon is the opposite of that.

The small world map on the statistics page is where the country-level picture
belongs, and it is there as of 0.19.7.

## Not doing: a polar projection

Recorded because it looks like a bug and is not one. Everything is on the Web
Mercator z14 grid, which stops at 85.0511°, and `geo.clamp_lat` clamps rather
than refusing - so a fix nearer a pole than that is stored exactly as recorded
and drawn on the top row of tiles, with the last 551 km to the pole collapsed
onto one line. Area up there is undercounted by the same factor the projection
stretches by: 0.044 km² per z14 tile at 85° against 2.776 km² at 47°.

Doing it properly means a second tile pyramid in a polar projection - a
separate grid, separate blobs, separate rendering, and a basemap that has no
polar tiles to draw underneath it. That is a second map rather than a fix, for
ground nobody in this archive is going to walk. Written up in the README so it
is a documented limit rather than a surprise.

## Not doing: anything social

Sharing, comparing, following, streaks, leaderboards. Recorded as a decision
rather than an omission, because each one arrives sounding harmless and every
one of them requires this to stop being a private record on somebody's own
hardware. The same goes for notifications: a map that nags is a different
product.

## Not doing: unlocking "what is searched" without a token

Asked for and then withdrawn once the reason was clear, so it is recorded
rather than reconsidered. Searching is read-only and needs no token, which
makes it look as though choosing what to search should need none either - but
the choice is stored in the `settings` table and read by the search endpoint,
so a browser that cannot write to the server cannot change it.

Unlocking the switches without moving the setting would give controls that
answer 401, which is the thing 0.18.14 existed to remove. Moving it to the
browser would work - the client would send `?kinds=pins,coordinates` with each
query, and it is arguably where a UI preference for a read-only feature
belongs - at the cost of an API change and of those settings no longer
travelling in a backup. Decided against: not worth the churn.

## Not doing: separating the two opacity sliders from their sections

Fog thickness and trail strength are applied on the GPU and reach no server,
but they sit in the Fog and Tracks sections, which are gated without a token
because the fog *colour* and the trail *ramp* are baked into tiles. So a
read-only browser cannot dim the fog.

Separating them means splitting both sections in two, and the answer was that
being gated is fine. Left as it is.

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
