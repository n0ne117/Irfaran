// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The holding pen, on the map. Everything automatic waits in it until
// somebody has looked at it, and looking at it means seeing it on the map by
// itself: a route drawn over a fog layer that already covers the ground it
// crosses is unreadable, and "is this worth keeping" is a question about the
// shape of the line, not about the archive around it.
//
// So this is a sidebar and not a settings tab. The sheet covers the map; the
// sidebar sits beside it, the way Places does.
//
// Trimming redraws as the slider moves, which is why the whole batch of
// coordinates comes down in one request and the preview is computed here. The
// server is told what was decided, not asked what it would look like - a
// round trip per pixel of slider travel is not a preview.

import type { Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend, getToken } from './api'
import { element } from './ui'

const SOURCE = 'irfaran-review'
const KEEP_LAYER = 'irfaran-review-keep'
const DROP_LAYER = 'irfaran-review-drop'
const ENDS_LAYER = 'irfaran-review-ends'

/** How often the badge asks. Rare on purpose: nothing here is urgent. */
const POLL_MS = 15_000

/** After the last drag, before the edit is sent. */
const SETTLE_MS = 400

const EMPTY = { type: 'FeatureCollection', features: [] as unknown[] }

export interface Waiting {
  id: number
  source: string
  label: string
  title: string
  day: string
  sealed: boolean
  edited: boolean
  points: number
  metres: number
  first_at: string | null
  last_at: string | null
  bounds: [number, number, number, number] | null
  arrived_at: string
}

interface Overview {
  count: number
  points: number
  by_source: Record<string, number>
  oldest: string | null
  gates: Record<string, boolean>
  items: Waiting[]
}

interface Segment {
  index: number
  begin: number
  end: number
  points: number
  metres: number
  first_at: string | null
  last_at: string | null
  dropped: boolean
}

/** One space between consecutive fixes, and what was decided about it. */
interface Gap {
  index: number
  metres: number
  seconds: number | null
  ratio: number | null
  /** Why the rule wanted a cut here, or '' if it did not. */
  reason: string
  cut: boolean
  by_hand: boolean
  /** What the device said about the fixes either side, when it says anything. */
  accuracy_before: number | null
  accuracy_after: number | null
  motion_before: string | null
  motion_after: string | null
}

interface Detail extends Waiting {
  /** [lon, lat, ISO 8601 | null] per point, exactly as it was received. */
  fixes: [number, number, string | null][]
  segments: Segment[]
  gaps: Gap[]
  edits: {
    title: string
    from: number
    to: number
    dropped: number[]
    cuts: number[]
    joins: number[]
  }
  keeping: number
  keeping_metres: number
  stretches: number
}

/**
 * What the device said about itself either side of a gap.
 *
 * Empty when it said nothing, which is most sources - only Overland reports
 * motion, and accuracy is not always there either.
 */
function describeEnds(gap: Gap): string {
  const parts: string[] = []
  const { accuracy_before: a, accuracy_after: b } = gap
  if (a !== null || b !== null) {
    const round = (value: number | null) => (value === null ? '?' : Math.round(value))
    parts.push(a === b ? `±${round(a)} m` : `±${round(a)}→${round(b)} m`)
  }
  const motions = [gap.motion_before, gap.motion_after].filter(Boolean)
  if (motions.length) {
    parts.push(
      gap.motion_before === gap.motion_after
        ? String(gap.motion_before)
        : `${gap.motion_before ?? '?'}→${gap.motion_after ?? '?'}`,
    )
  }
  return parts.join(' ')
}

/** Why the rule cut here, in words rather than in a keyword. */
function describeReason(reason: string): string {
  if (reason === 'rate') return 'it stopped reporting'
  if (reason === 'distance') return 'too far apart'
  if (reason === 'time') return 'too long a silence'
  return 'cut by hand'
}

export function formatDistance(metres: number): string {
  if (metres < 1000) return `${Math.round(metres)} m`
  return `${(metres / 1000).toFixed(metres < 10_000 ? 2 : 1)} km`
}

function formatWhen(first: string | null, last: string | null): string {
  if (!first) return 'no timestamps'
  const from = new Date(first)
  const day = from.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
  const clock = (value: string) =>
    new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (!last || last === first) return `${day}, ${clock(first)}`
  return `${day}, ${clock(first)} to ${clock(last)}`
}

function minutesBetween(first: string | null, last: string | null): number {
  if (!first || !last) return 0
  return Math.max(0, (new Date(last).getTime() - new Date(first).getTime()) / 60_000)
}

function formatGap(minutes: number): string {
  if (minutes < 1) return 'under a minute'
  if (minutes < 90) return `${Math.round(minutes)} min`
  return `${(minutes / 60).toFixed(1)} h`
}

/** Metres between two coordinates. Only ever used to describe a trim. */
function haversine(a: [number, number], b: [number, number]): number {
  const toRad = Math.PI / 180
  const phiA = a[1] * toRad
  const phiB = b[1] * toRad
  const dPhi = phiB - phiA
  const dLambda = (b[0] - a[0]) * toRad
  const inner =
    Math.sin(dPhi / 2) ** 2 +
    Math.cos(phiA) * Math.cos(phiB) * Math.sin(dLambda / 2) ** 2
  return 2 * 6_371_008.8 * Math.asin(Math.min(1, Math.sqrt(inner)))
}

export class Review {
  private readonly map: MapLibreMap
  private readonly onOpen: () => void
  private readonly onApproved: (summary: string) => void
  /** Hides everything else on the map while one batch is being looked at. */
  private readonly setRestVisible: (visible: boolean) => void
  private readonly onDrawGap: (
    from: [number, number],
    to: [number, number],
    year: string,
  ) => void

  private items: Waiting[] = []
  private current: Detail | null = null
  private timer: number | undefined
  private settle: number | undefined
  /** Saves run one after another. See queue(). */
  private chain: Promise<void> = Promise.resolve()
  /** Boundaries added and removed by hand, mirrored so a save carries them. */
  private cuts = new Set<number>()
  private joins = new Set<number>()
  private watching = false

  constructor(
    map: MapLibreMap,
    hooks: {
      onOpen: () => void
      onApproved: (summary: string) => void
      setRestVisible: (visible: boolean) => void
      onDrawGap: (
        from: [number, number],
        to: [number, number],
        year: string,
      ) => void
    },
  ) {
    this.map = map
    this.onOpen = hooks.onOpen
    this.onApproved = hooks.onApproved
    this.setRestVisible = hooks.setRestVisible
    this.onDrawGap = hooks.onDrawGap
  }

  // ------------------------------------------------------------- map layers

  /**
   * Add the preview source and its layers. Only valid once the style has
   * loaded, so the caller attaches on style.load like Trails does.
   */
  attach(): void {
    if (this.map.getSource(SOURCE)) return
    this.map.addSource(SOURCE, { type: 'geojson', data: EMPTY as never })

    // What is being left out, underneath and dashed, rather than removed from
    // the drawing entirely. Seeing what a trim discards is the only way to
    // know the trim is in the right place.
    this.map.addLayer({
      id: DROP_LAYER,
      type: 'line',
      source: SOURCE,
      filter: ['==', ['get', 'keep'], false],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': '#8a8a94',
        'line-width': ['interpolate', ['linear'], ['zoom'], 6, 1.5, 14, 2.5, 18, 3.5],
        'line-opacity': 0.7,
        'line-dasharray': [2, 2],
      },
    })
    this.map.addLayer({
      id: KEEP_LAYER,
      type: 'line',
      source: SOURCE,
      filter: ['==', ['get', 'keep'], true],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': '#ffb454',
        'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2, 14, 4, 18, 6],
        'line-opacity': 0.95,
      },
    })
    // Where it starts and where it stops - the two ends a trim is usually
    // about.
    this.map.addLayer({
      id: ENDS_LAYER,
      type: 'circle',
      source: SOURCE,
      filter: ['==', ['geometry-type'], 'Point'],
      paint: {
        'circle-radius': 5,
        'circle-color': ['case', ['==', ['get', 'start'], true], '#5ad18a', '#e2604f'],
        'circle-stroke-width': 1.5,
        'circle-stroke-color': '#000000aa',
      },
    })
  }

  private paint(collection: unknown): void {
    const source = this.map.getSource(SOURCE)
    if (source && 'setData' in source) {
      ;(source as { setData: (data: unknown) => void }).setData(collection)
    }
  }

  // ---------------------------------------------------------------- polling

  wire(): void {
    element('review-badge').addEventListener('click', () => {
      this.onOpen()
      void this.load()
    })
    element('review-close').addEventListener('click', () => this.leave())
    element('review-back').addEventListener('click', () => {
      this.closeOne()
      void this.load()
    })

    element('review-open-page').addEventListener('click', () => {
      this.onOpen()
      void this.load()
    })
    element('review-review-now').addEventListener('click', () => {
      this.onOpen()
      void this.load()
    })

    const name = element<HTMLInputElement>('review-name')
    name.addEventListener('change', () => void this.queue())

    const from = element<HTMLInputElement>('review-from')
    const to = element<HTMLInputElement>('review-to')
    for (const slider of [from, to]) {
      slider.addEventListener('input', () => {
        // The two handles cannot cross. Pushing rather than refusing: a drag
        // that stops dead at the other handle reads as a broken slider.
        if (Number(from.value) > Number(to.value)) {
          if (slider === from) to.value = from.value
          else from.value = to.value
        }
        this.redraw()
        this.later()
      })
    }

    element<HTMLInputElement>('review-show-rest').addEventListener('change', (event) => {
      this.setRestVisible((event.target as HTMLInputElement).checked)
    })

    element('review-approve').addEventListener('click', () => void this.approve())
    element('review-reset').addEventListener('click', () => void this.reset())
    element('review-discard').addEventListener('click', () => void this.discard())

    void this.load()
  }

  /** Keep the badge current while the map is on screen. */
  watch(on: boolean): void {
    if (on === this.watching) return
    this.watching = on
    if (this.timer !== undefined) window.clearInterval(this.timer)
    this.timer = undefined
    if (!on) return
    this.timer = window.setInterval(() => void this.load(), POLL_MS)
  }

  async load(): Promise<void> {
    try {
      const overview = await apiGet<Overview>('/api/review')
      this.items = overview.items
      this.paintBadge(overview)
      this.paintGates(overview)
      if (!element('review-page').hidden && !this.current) this.paintList()
    } catch {
      // The badge is not worth an error message. A server that cannot be
      // reached is already being said elsewhere, loudly.
    }
  }

  private paintBadge(overview: Overview): void {
    const badge = element<HTMLButtonElement>('review-badge')

    // Nothing to decide, or nothing that can be decided: reading the map
    // needs no token, and offering a decision that will be refused is worse
    // than not mentioning it.
    if (!overview.count || !getToken()) {
      badge.hidden = true
      return
    }

    element('review-badge-count').textContent = String(overview.count)
    element('review-badge-text').textContent = 'waiting to be reviewed'
    badge.hidden = false
    badge.title =
      `${overview.count} ${overview.count === 1 ? 'batch' : 'batches'}, ` +
      `${overview.points.toLocaleString()} points, none of it on the map yet.`
  }

  private paintGates(overview: Overview): void {
    for (const [source, gated] of Object.entries(overview.gates)) {
      const box = document.getElementById(`review-${source}`)
      if (box instanceof HTMLInputElement) box.checked = gated
    }
    const line = document.getElementById('review-waiting-line')
    if (line) {
      line.textContent = overview.count
        ? `${overview.count} ${overview.count === 1 ? 'batch' : 'batches'} waiting, ` +
          `${overview.points.toLocaleString()} points.`
        : 'Nothing is waiting.'
    }
  }

  // ------------------------------------------------------------------- list

  private paintList(): void {
    const list = element('review-list')
    list.textContent = ''
    element('review-empty').hidden = this.items.length > 0
    element('review-heading').textContent = this.items.length
      ? `To review (${this.items.length})`
      : 'To review'

    for (const item of this.items) {
      const row = document.createElement('button')
      row.type = 'button'
      row.className = 'review-item'

      const title = document.createElement('strong')
      title.textContent = item.title
      const detail = document.createElement('span')
      detail.className = 'review-item-detail'
      detail.textContent =
        `${item.label} · ${formatWhen(item.first_at, item.last_at)} · ` +
        `${item.points.toLocaleString()} points · ${formatDistance(item.metres)}`

      row.append(title, detail)
      if (!item.sealed) {
        const open = document.createElement('span')
        open.className = 'review-item-open'
        open.textContent = 'still collecting'
        row.append(open)
      }
      row.addEventListener('click', () => void this.open(item.id))
      list.append(row)
    }
  }

  // -------------------------------------------------------------- one batch

  private async open(id: number): Promise<void> {
    try {
      // Sealing is the point of the request: from here the set cannot change
      // underneath the person reading it.
      const detail = await apiSend<Detail>('POST', `/api/review/${id}/open`)
      this.current = detail
      this.paintOne(detail)
      this.setRestVisible(element<HTMLInputElement>('review-show-rest').checked)
      this.redraw()
      this.frame(detail)
    } catch (error) {
      this.say(error, 'review-list-message')
      void this.load()
    }
  }

  private paintOne(detail: Detail): void {
    element('review-list-view').hidden = true
    element('review-one').hidden = false
    element('review-heading').textContent = 'Reviewing'
    element('review-message').hidden = true

    element('review-source').textContent = detail.label
    element('review-when').textContent = formatWhen(detail.first_at, detail.last_at)
    element('review-points').textContent = detail.points.toLocaleString()
    element('review-distance').textContent = formatDistance(detail.metres)

    element<HTMLInputElement>('review-name').value = detail.title
    this.cuts = new Set(detail.edits.cuts)
    this.joins = new Set(detail.edits.joins)

    const last = Math.max(0, detail.points - 1)
    const from = element<HTMLInputElement>('review-from')
    const to = element<HTMLInputElement>('review-to')
    from.max = String(last)
    to.max = String(last)
    from.value = String(Math.min(detail.edits.from, last))
    to.value = String(detail.edits.to < 0 ? last : Math.min(detail.edits.to, last))

    this.paintGaps(detail)
    this.paintSegments(detail)
  }

  /**
   * Every gap worth a decision, with the numbers it was decided on.
   *
   * A phone stops reporting and starts again somewhere else; joining the two
   * ends draws a route nobody took and clears fog along it. The rule decides,
   * and this is where it can be overruled either way - which matters because
   * the thresholds were chosen from one archive and will be wrong for some
   * other one.
   */
  private paintGaps(detail: Detail): void {
    const host = element('review-gaps')
    host.textContent = ''

    const shown = detail.gaps.length > 0
    for (const id of ['review-gaps', 'review-gaps-heading', 'review-gaps-hint']) {
      element(id).hidden = !shown
    }
    if (!shown) return

    const cuts = detail.gaps.filter((gap) => gap.cut).length
    element('review-gaps-heading').textContent =
      cuts === 0
        ? 'Where it breaks — nowhere'
        : cuts === 1
          ? 'Where it breaks — one place'
          : `Where it breaks — ${cuts} places`

    for (const gap of detail.gaps) {
      const row = document.createElement('div')
      row.className = 'review-gap'
      row.dataset.cut = String(gap.cut)
      row.dataset.byHand = String(gap.by_hand)

      const text = document.createElement('span')
      const headline = document.createElement('strong')
      headline.textContent = formatDistance(gap.metres)
      const detailLine = document.createElement('span')
      detailLine.className = 'review-gap-detail'
      const parts: string[] = []
      if (gap.seconds !== null) parts.push(`${Math.round(gap.seconds)} s`)
      if (gap.ratio !== null) parts.push(`${gap.ratio.toFixed(1)}× the usual`)
      // What the device said about itself either side of the silence. A coarse
      // accuracy or a motion that cannot cover the ground is the difference
      // between a stale position and a real unreported stretch, and nothing
      // else in the data can tell them apart.
      const said = describeEnds(gap)
      if (said) parts.push(said)
      parts.push(gap.cut ? describeReason(gap.reason) : 'joined')
      detailLine.textContent = ` — ${parts.join(' · ')}`
      text.append(headline, detailLine)

      const buttons = document.createElement('span')
      buttons.className = 'review-gap-buttons'

      const toggle = document.createElement('button')
      toggle.type = 'button'
      toggle.textContent = gap.cut ? 'Rejoin' : 'Cut'
      toggle.title = gap.cut
        ? 'Draw the line straight across this gap after all'
        : 'Stop the line being drawn across this gap'
      toggle.addEventListener('click', () => void this.toggleGap(gap))
      buttons.append(toggle)

      // Only where the line is not being drawn: there is nothing to fill in
      // across a gap that is already joined.
      if (gap.cut) {
        const drawIt = document.createElement('button')
        drawIt.type = 'button'
        drawIt.textContent = 'Draw it'
        drawIt.title =
          'Draw the missing stretch by hand, then come back here'
        drawIt.addEventListener('click', () => this.handOver(gap))
        buttons.append(drawIt)
      }

      row.append(text, buttons)
      host.append(row)
    }
  }

  /**
   * Hand this gap to the Track tool, and expect to come back.
   *
   * Saved first: a trim or a rename made a moment ago must not be lost to a
   * detour through the drawing tools.
   */
  private handOver(gap: Gap): void {
    const detail = this.current
    if (!detail) return
    const before = detail.fixes[gap.index - 1]
    const after = detail.fixes[gap.index]
    if (!before || !after) return

    void this.flush().then(() => {
      this.onDrawGap(
        [before[0], before[1]],
        [after[0], after[1]],
        (detail.day || '').slice(0, 4),
      )
    })
  }

  /** Cut a gap the rule joined, or rejoin one it cut. */
  private async toggleGap(gap: Gap): Promise<void> {
    // A boundary the rule found is removed by disagreeing with it; one it did
    // not find is removed by taking back the disagreement. Keeping the two
    // apart is what lets the thresholds change later without silently
    // overruling somebody - a join stays a join, whatever the rule now thinks.
    const byRule = gap.reason !== ''
    if (gap.cut) {
      this.cuts.delete(gap.index)
      if (byRule) this.joins.add(gap.index)
    } else {
      this.joins.delete(gap.index)
      if (!byRule) this.cuts.add(gap.index)
    }

    await this.flush()
    if (this.current) {
      this.paintOne(this.current)
      this.redraw()
    }
  }

  private paintSegments(detail: Detail): void {
    const host = element('review-segments')
    host.textContent = ''
    const heading = element('review-parts-heading')
    heading.textContent = detail.segments.length === 1 ? 'One part' : `${detail.segments.length} parts`

    // One part is the whole thing, and a checkbox that can only turn the
    // whole thing off is the Discard button with extra steps.
    host.hidden = detail.segments.length < 2
    if (detail.segments.length < 2) return

    for (const segment of detail.segments) {
      const label = document.createElement('label')
      label.className = 'check review-segment'

      const box = document.createElement('input')
      box.type = 'checkbox'
      box.checked = !segment.dropped
      box.addEventListener('change', () => {
        this.redraw()
        void this.queue()
      })

      const text = document.createElement('span')
      const strong = document.createElement('strong')
      strong.textContent = `Part ${segment.index + 1}`
      const rest = document.createElement('span')
      rest.className = 'review-item-detail'
      rest.textContent =
        ` ${segment.points.toLocaleString()} points · ${formatDistance(segment.metres)}` +
        (segment.first_at
          ? ` · ${formatGap(minutesBetween(segment.first_at, segment.last_at))}`
          : '')
      text.append(strong, rest)

      label.append(box, text)
      label.dataset.index = String(segment.begin)
      host.append(label)
    }
  }

  private closeOne(): void {
    this.current = null
    element('review-one').hidden = true
    element('review-list-view').hidden = false
    element('review-heading').textContent = 'To review'
    this.paint(EMPTY)
    this.setRestVisible(true)
  }

  /** Close the sidebar entirely and put the map back the way it was. */
  private leave(): void {
    this.closeOne()
    element('review-page').hidden = true
  }

  /** Called by the owner when the sidebar is closed some other way. */
  closed(): void {
    if (element('review-page').hidden) this.closeOne()
  }

  // ------------------------------------------------------------- the preview

  private get trimmed(): { from: number; to: number } {
    return {
      from: Number(element<HTMLInputElement>('review-from').value || 0),
      to: Number(element<HTMLInputElement>('review-to').value || 0),
    }
  }

  private get dropped(): Set<number> {
    const chosen = new Set<number>()
    for (const label of document.querySelectorAll<HTMLElement>('#review-segments .review-segment')) {
      const box = label.querySelector('input')
      if (box instanceof HTMLInputElement && !box.checked) {
        chosen.add(Number(label.dataset.index))
      }
    }
    return chosen
  }

  /**
   * Draw what accepting would add, and what it would leave behind.
   *
   * Runs are broken at a segment boundary as well as where kept turns into
   * dropped, so a batch containing a flight is two lines rather than one line
   * across an ocean.
   */
  private redraw(): void {
    const detail = this.current
    if (!detail) return

    const { from, to } = this.trimmed
    const dropped = this.dropped
    const owner = new Map<number, number>()
    for (const segment of detail.segments) {
      for (let index = segment.begin; index <= segment.end; index += 1) {
        owner.set(index, segment.begin)
      }
    }

    const features: unknown[] = []
    let run: [number, number][] = []
    let runKeep = false
    let runSegment = -1

    const flush = (carry: [number, number] | null) => {
      if (run.length === 0) {
        run = carry ? [carry] : []
        return
      }
      features.push({
        type: 'Feature',
        properties: { keep: runKeep },
        geometry: {
          type: 'LineString',
          coordinates: run.length === 1 ? [run[0], run[0]] : run,
        },
      })
      // The two runs share the point where they meet. Without it a trim opens
      // a visible gap in the line exactly where it cuts, which reads as data
      // missing rather than as a boundary.
      run = carry ? [carry] : []
    }

    let keptFirst: [number, number] | null = null
    let keptLast: [number, number] | null = null
    let keptCount = 0
    let keptMetres = 0
    let previousKept: [number, number] | null = null
    let previousKeptSegment = -1

    for (let index = 0; index < detail.fixes.length; index += 1) {
      const fix = detail.fixes[index]
      const point: [number, number] = [fix[0], fix[1]]
      const segment = owner.get(index) ?? 0
      const keep = index >= from && index <= to && !dropped.has(segment)

      if (keep) {
        keptCount += 1
        if (!keptFirst) keptFirst = point
        keptLast = point
        if (previousKept && previousKeptSegment === segment) {
          keptMetres += haversine(previousKept, point)
        }
        previousKept = point
        previousKeptSegment = segment
      }

      if (keep !== runKeep || segment !== runSegment) {
        // Carried across a change of kept-ness, but never across a segment
        // boundary: the gap between two segments is the whole reason they are
        // two, and bridging it would draw a line over the flight.
        const joins = index > 0 && segment === runSegment
        flush(joins ? (run[run.length - 1] ?? null) : null)
        runKeep = keep
        runSegment = segment
      }
      run.push(point)
    }
    flush(null)

    for (const [point, start] of [
      [keptFirst, true],
      [keptLast, false],
    ] as [[number, number] | null, boolean][]) {
      if (!point) continue
      features.push({
        type: 'Feature',
        properties: { start },
        geometry: { type: 'Point', coordinates: point },
      })
    }

    this.paint({ type: 'FeatureCollection', features })
    this.paintTrimNote(detail, from, to, keptCount, keptMetres)
  }

  private paintTrimNote(
    detail: Detail,
    from: number,
    to: number,
    keptCount: number,
    keptMetres: number,
  ): void {
    const note = element('review-trim-note')
    const cut = detail.points - keptCount

    if (cut === 0) {
      note.textContent = `Keeping all ${detail.points.toLocaleString()} points, ${formatDistance(keptMetres)}.`
      return
    }

    const head = this.spanOf(detail, 0, from - 1)
    const tail = this.spanOf(detail, to + 1, detail.points - 1)
    const parts: string[] = []
    if (head > 0) parts.push(`${formatDistance(head)} off the start`)
    if (tail > 0) parts.push(`${formatDistance(tail)} off the end`)

    note.textContent =
      `Keeping ${keptCount.toLocaleString()} of ${detail.points.toLocaleString()} points, ` +
      `${formatDistance(keptMetres)}` +
      (parts.length ? ` — ${parts.join(', ')}.` : `, ${cut.toLocaleString()} left out.`)
  }

  /** Ground covered between two indexes, for describing a trim. */
  private spanOf(detail: Detail, begin: number, end: number): number {
    if (!detail || end <= begin) return 0
    let total = 0
    for (let index = begin + 1; index <= Math.min(end, detail.fixes.length - 1); index += 1) {
      const before = detail.fixes[index - 1]
      const after = detail.fixes[index]
      total += haversine([before[0], before[1]], [after[0], after[1]])
    }
    return total
  }

  private frame(detail: Detail): void {
    if (!detail.bounds) return
    const [west, south, east, north] = detail.bounds
    // A batch that never moved is a point, and fitBounds on a zero-size box
    // zooms to the maximum. Centre on it instead.
    if (east - west < 1e-6 && north - south < 1e-6) {
      this.map.flyTo({ center: [west, south], zoom: 15 })
      return
    }
    this.map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      { padding: 70, maxZoom: 16, duration: 600 },
    )
  }

  // ------------------------------------------------------------- the writes

  /** Save once the handles have stopped moving. */
  private later(): void {
    if (this.settle !== undefined) window.clearTimeout(this.settle)
    this.settle = window.setTimeout(() => void this.queue(), SETTLE_MS)
  }

  /**
   * One save at a time, always carrying the whole decision.
   *
   * The first version sent deltas - the trim from the sliders, the parts from
   * the checkboxes, the name from the field - each as its own request. The
   * server reads the current edits, applies the change and writes them back,
   * so two requests in flight together both read the same starting point and
   * the second one silently undid the first. Unticking a part and then
   * renaming the batch lost the unticked part, with the panel still showing
   * it unticked: found by doing exactly that.
   *
   * Two things fix it and both are worth having. Every save carries the full
   * document, so whatever arrives last is a complete and coherent state
   * rather than a fragment applied to a guess. And saves are chained, so a
   * second one cannot overtake the first.
   */
  private queue(): Promise<void> {
    this.chain = this.chain.then(() => this.saveNow()).catch(() => {})
    return this.chain
  }

  /** Send anything outstanding and wait for it. Before accepting. */
  private async flush(): Promise<void> {
    if (this.settle !== undefined) {
      window.clearTimeout(this.settle)
      this.settle = undefined
    }
    await this.queue()
  }

  private async saveNow(): Promise<void> {
    const detail = this.current
    if (!detail) return

    // Read at the moment the save runs, not when it was asked for, so a save
    // queued behind another one sends what the panel says now.
    const { from, to } = this.trimmed
    const changes = {
      title: element<HTMLInputElement>('review-name').value,
      from,
      to,
      dropped: [...this.dropped],
      cuts: [...this.cuts],
      joins: [...this.joins],
    }

    try {
      const updated = await apiSend<Detail>('PATCH', `/api/review/${detail.id}`, changes)
      // The coordinates never change, and re-reading them would mean throwing
      // away the slider position mid-drag.
      this.current = { ...updated, fixes: detail.fixes }
      element('review-message').hidden = true
    } catch (error) {
      this.say(error, 'review-message')
    }
  }

  private async reset(): Promise<void> {
    const detail = this.current
    if (!detail) return
    try {
      this.cuts.clear()
      this.joins.clear()
      const fresh = await apiSend<Detail>('POST', `/api/review/${detail.id}/reset`)
      this.current = fresh
      this.paintOne(fresh)
      this.redraw()
    } catch (error) {
      this.say(error, 'review-message')
    }
  }

  private async approve(): Promise<void> {
    const detail = this.current
    if (!detail) return
    const button = element<HTMLButtonElement>('review-approve')
    button.disabled = true
    try {
      // A trim decided a quarter of a second ago is still on its way. Sending
      // it first is the difference between accepting what is on screen and
      // accepting what was on screen before the last drag.
      await this.flush()
      const done = await apiSend<{
        points: number
        left_out: number
        stretches: number
        tiles_touched: number
      }>('POST', `/api/review/${detail.id}/approve`)
      const stretches =
        done.stretches > 1 ? ` as ${done.stretches} stretches` : ''
      const summary =
        `Added ${done.points.toLocaleString()} points${stretches}` +
        (done.left_out ? `, ${done.left_out.toLocaleString()} left out` : '')

      this.closeOne()
      await this.load()
      this.paintList()
      // Told before the local note, so the bar above the time bar goes up
      // while the sidebar is still saying what happened.
      this.onApproved(summary)
      this.note(
        `${summary}. ${done.tiles_touched.toLocaleString()} tiles are being redrawn.`,
      )
    } catch (error) {
      this.say(error, 'review-message')
    } finally {
      button.disabled = false
    }
  }

  private async discard(): Promise<void> {
    const detail = this.current
    if (!detail) return
    if (
      !window.confirm(
        `Throw away ${detail.title}? ${detail.points.toLocaleString()} points, ` +
          'never added to the map. This cannot be undone.',
      )
    ) {
      return
    }
    try {
      await apiSend('DELETE', `/api/review/${detail.id}`)
      this.closeOne()
      await this.load()
      this.paintList()
      this.note('Discarded. Nothing entered the archive.')
    } catch (error) {
      this.say(error, 'review-message')
    }
  }

  private note(message: string): void {
    const line = element('review-list-message')
    line.textContent = message
    line.hidden = false
    window.setTimeout(() => {
      if (line.textContent === message) line.hidden = true
    }, 8000)
  }

  private say(error: unknown, where: string): void {
    const line = element(where)
    line.textContent =
      error instanceof ApiError
        ? error.message
        : 'That did not work. The browser console has the detail.'
    line.hidden = false
    if (!(error instanceof ApiError)) console.error('Irfaran review', error)
  }
}
