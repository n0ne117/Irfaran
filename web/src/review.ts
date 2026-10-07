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

import type { MapMouseEvent, Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend, getToken } from './api'
import { MIN_DRAW_ZOOM } from './draw'
import { element, radioGroup } from './ui'

const SOURCE = 'irfaran-review'
const KEEP_LAYER = 'irfaran-review-keep'
const DROP_LAYER = 'irfaran-review-drop'
const ENDS_LAYER = 'irfaran-review-ends'
const FIXES_LAYER = 'irfaran-review-fixes'
const STROKES_LAYER = 'irfaran-review-strokes'
const PICKED_LAYER = 'irfaran-review-picked'

/**
 * Drag the map, or draw. Nothing else: areas and Re-Fog are not filling a gap,
 * and point to point - where a drag pans and a click places a corner - read as
 * a tool that would not draw. Reported as: "Draw it still moves the map".
 */
export type ReviewTool = 'off' | 'freehand'

/**
 * How close, in screen pixels, a drawn end has to come to a reported point to
 * be moved onto it. A drawn piece that stops three metres short of the track
 * it was filling leaves a three-metre hole in the fog, and nobody draws to
 * the metre with a mouse.
 */
const SNAP_PX = 16

type Coordinate = [number, number]

/** A line drawn in the review, and how wide it clears either side. */
interface Stroke {
  line: Coordinate[]
  radius_m: number
}

/**
 * Points left out by hand close up behind them when their neighbours are at
 * most this far apart. The server's HEAL_METRES, mirrored so the preview is
 * what lands.
 */
const HEAL_METRES = 250

/** How far from a dot, in screen pixels, a click still picks it. */
const PICK_PX = 8

/** How far a press on a dot has to travel before it is a drag, not a click. */
const DRAG_PX = 4

/** The review bar's width slider, in metres either side. */
const RADIUS_MIN = 1
const RADIUS_MAX = 60

/**
 * A stretch this short is a dot rather than a line. One point drew as a line of
 * no length, which is nothing at all: a train day had seven of them, each a
 * position the phone did report, and none of them on the map.
 */
const ALONE_POINTS = 3

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
  collecting: boolean
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
  /**
   * [lon, lat, ISO 8601 | null, accuracy in metres | null, motion | null] per
   * point, exactly as it was received. Older batches stop after the time.
   */
  fixes: [number, number, string | null, (number | null)?, (string | null)?][]
  segments: Segment[]
  gaps: Gap[]
  edits: {
    title: string
    from: number
    to: number
    dropped: number[]
    cuts: number[]
    joins: number[]
    removed: number[]
    /** [index, lon, lat] for each point dragged somewhere else. */
    moved: [number, number, number][]
    strokes: Stroke[]
  }
  /** How wide a line drawn here will be: the track's width, not the brush's. */
  drawn_radius_m: number
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
  /** Lend the real drawing tool to the review, and take it back. */
  private readonly startDrawing: (
    tool: Exclude<ReviewTool, 'off'>,
    radiusM: number,
    onLine: (coordinates: Coordinate[]) => void,
  ) => void
  private readonly stopDrawing: () => void

  private items: Waiting[] = []
  private current: Detail | null = null
  private timer: number | undefined
  private settle: number | undefined
  /** Saves run one after another. See queue(). */
  private chain: Promise<void> = Promise.resolve()
  /** Boundaries added and removed by hand, mirrored so a save carries them. */
  private cuts = new Set<number>()
  private joins = new Set<number>()
  /** Lines drawn in this review, mirrored so a save carries them. */
  private strokes: Stroke[] = []
  /** Single points left out by hand, by index. */
  private removed = new Set<number>()
  /** Points dragged by hand, and where to. */
  private moves = new Map<number, Coordinate>()
  /** The point being dragged, while it is. */
  private dragging: { index: number; x: number; y: number; moved: boolean } | null = null
  /** The redraw a drag has asked for, one per animation frame. */
  private pendingFrame = 0
  /** Points picked on the map, and where a shift-click range starts. */
  private picked = new Set<number>()
  private anchor: number | null = null
  private drawTool: ReviewTool = 'off'
  /** The width the next line is drawn at. Starts at the track's own. */
  private radius = 20
  private paintTool: (value: ReviewTool) => void = () => {}
  private watching = false

  constructor(
    map: MapLibreMap,
    hooks: {
      onOpen: () => void
      onApproved: (summary: string) => void
      setRestVisible: (visible: boolean) => void
      startDrawing: (
        tool: Exclude<ReviewTool, 'off'>,
        radiusM: number,
        onLine: (coordinates: Coordinate[]) => void,
      ) => void
      stopDrawing: () => void
    },
  ) {
    this.map = map
    this.onOpen = hooks.onOpen
    this.onApproved = hooks.onApproved
    this.setRestVisible = hooks.setRestVisible
    this.startDrawing = hooks.startDrawing
    this.stopDrawing = hooks.stopDrawing
  }

  // ------------------------------------------------------------- map layers

  /**
   * Add the preview source and its layers. Only valid once the style has
   * loaded, so the caller attaches on style.load like Trails does.
   */
  attach(): void {
    if (this.map.getSource(SOURCE)) return
    this.map.addSource(SOURCE, { type: 'geojson', data: EMPTY as never })

    // Every fix, under the line. Along a dense trace it is a fringe either
    // side, coloured by how sure the phone was; where there is no line - a
    // fix between two cuts - it is the only thing on the map. The colours
    // stop at 50 m because anything coarser never got this far.
    this.map.addLayer({
      id: FIXES_LAYER,
      type: 'circle',
      source: SOURCE,
      filter: ['==', ['get', 'fix'], true],
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['zoom'],
          6, ['case', ['get', 'alone'], 4, 2],
          14, ['case', ['get', 'alone'], 6, 3.5],
          18, ['case', ['get', 'alone'], 8, 5],
        ],
        'circle-color': [
          'case',
          // Put there by hand, so the same blue as a hand-drawn line.
          ['==', ['get', 'moved'], true], '#7cc4ff',
          ['!', ['has', 'accuracy']], '#b8b8c0',
          ['<=', ['get', 'accuracy'], 10], '#6cc4a1',
          ['<=', ['get', 'accuracy'], 25], '#e8c547',
          '#d9534f',
        ],
        'circle-opacity': ['case', ['get', 'keep'], 0.9, 0.35],
        'circle-stroke-width': ['case', ['get', 'alone'], 1.5, 0],
        // White, not the ends' black: a lone red fix outlined in black is
        // the end marker, and the end is exactly what it would be taken for.
        'circle-stroke-color': '#ffffffcc',
      },
    })

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
    // Drawn by hand in this review. A colour of its own, because the whole
    // point of keeping it apart is that it is not what the phone reported.
    this.map.addLayer({
      id: STROKES_LAYER,
      type: 'line',
      source: SOURCE,
      filter: ['==', ['get', 'stroke'], true],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': '#7cc4ff',
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
      filter: ['has', 'start'],
      paint: {
        'circle-radius': 5,
        'circle-color': ['case', ['==', ['get', 'start'], true], '#5ad18a', '#e2604f'],
        'circle-stroke-width': 1.5,
        'circle-stroke-color': '#000000aa',
      },
    })
    // What is picked. A ring rather than a colour, so a picked dot still says
    // how accurate it was.
    this.map.addLayer({
      id: PICKED_LAYER,
      type: 'circle',
      source: SOURCE,
      filter: ['==', ['get', 'picked'], true],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 5, 14, 8, 18, 10],
        'circle-color': 'rgba(0, 0, 0, 0)',
        'circle-stroke-width': 2.5,
        'circle-stroke-color': '#ffffff',
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

    this.paintTool = radioGroup<ReviewTool>('review-draw-tool', 'off', (tool) =>
      this.setDrawTool(tool),
    )
    const size = element<HTMLInputElement>('review-draw-size')
    size.addEventListener('input', () => this.setRadius(Number(size.value)))
    element('review-draw-size-down').addEventListener('click', () =>
      this.setRadius(this.radius - 1),
    )
    element('review-draw-size-up').addEventListener('click', () =>
      this.setRadius(this.radius + 1),
    )
    element('review-draw-zoom-in').addEventListener('click', () =>
      this.map.easeTo({ zoom: MIN_DRAW_ZOOM }),
    )
    // Moving points: a press on a dot holds the dot rather than the map.
    this.map.on('mousedown', (event) => this.beginDrag(event))
    this.map.on('mousemove', (event) => this.moveDrag(event))
    this.map.on('mouseup', () => this.endDrag())
    // Let go of outside the map, the dot is still let go of.
    window.addEventListener('mouseup', () => this.endDrag())

    // Picking points. Only in Drag map: with Draw armed a press is a line.
    this.map.on('click', (event) => {
      if (!this.current || this.drawTool !== 'off') return
      const original = event.originalEvent as MouseEvent
      this.pick(event.point, original.shiftKey)
    })
    document.addEventListener('keydown', (event) => {
      if (!this.current || element('review-page').hidden) return
      const target = event.target as HTMLElement | null
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      if ((event.key === 'Delete' || event.key === 'Backspace') && this.picked.size) {
        event.preventDefault()
        this.leaveOut(true)
      } else if (event.key === 'Escape' && this.picked.size) {
        this.clearPicked()
      }
    })
    element('review-pick-out').addEventListener('click', () => this.leaveOut(true))
    element('review-pick-back').addEventListener('click', () => this.leaveOut(false))
    element('review-pick-clear').addEventListener('click', () => this.clearPicked())

    element('review-draw-undo').addEventListener('click', () =>
      this.removeStroke(this.strokes.length - 1),
    )
    // Drawing is locked out below z14 by the tool itself, which puts itself
    // away; the buttons have to say so rather than stay lit over nothing.
    this.map.on('zoomend', () => {
      if (this.drawTool !== 'off' && this.map.getZoom() < MIN_DRAW_ZOOM) {
        this.drawTool = 'off'
        this.stopDrawing()
      }
      this.paintDrawTools()
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
      // Only for the day a phone is still reporting into. An unsealed batch
      // from last week is joinable in principle and finished in practice, and
      // saying otherwise reads as "wait, there is more coming".
      if (item.collecting) {
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
      this.radius = detail.drawn_radius_m
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
    this.removed = new Set(detail.edits.removed ?? [])
    this.moves = new Map(
      (detail.edits.moved ?? []).map(([index, lon, lat]): [number, Coordinate] => [index, [lon, lat]]),
    )
    this.picked.clear()
    this.anchor = null
    this.strokes = (detail.edits.strokes ?? []).map((stroke) => ({
      line: stroke.line.map((point): Coordinate => [point[0], point[1]]),
      radius_m: stroke.radius_m,
    }))

    const last = Math.max(0, detail.points - 1)
    const from = element<HTMLInputElement>('review-from')
    const to = element<HTMLInputElement>('review-to')
    from.max = String(last)
    to.max = String(last)
    from.value = String(Math.min(detail.edits.from, last))
    to.value = String(detail.edits.to < 0 ? last : Math.min(detail.edits.to, last))

    this.paintGaps(detail)
    this.paintSegments(detail)
    this.paintStrokes()
    this.paintDrawTools()
    this.paintPicked()
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
          'Draw the missing stretch by hand, here in the review'
        drawIt.addEventListener('click', () => this.handOver(gap))
        buttons.append(drawIt)
      }

      row.append(text, buttons)
      host.append(row)
    }
  }

  /**
   * Draw across this gap, here.
   *
   * It used to hand the gap to the world map's drawing tools and come back,
   * which saved the drawn piece there and then - so discarding the batch
   * afterwards left its filling on the map. Now the camera goes to the gap and
   * Draw is armed in the review, and the piece is part of the review until the
   * review is decided.
   */
  private handOver(gap: Gap): void {
    const detail = this.current
    if (!detail) return
    const before = detail.fixes[gap.index - 1]
    const after = detail.fixes[gap.index]
    if (!before || !after) return

    const from: Coordinate = [before[0], before[1]]
    const to: Coordinate = [after[0], after[1]]
    const camera = this.map.cameraForBounds(
      [
        [Math.min(from[0], to[0]), Math.min(from[1], to[1])],
        [Math.max(from[0], to[0]), Math.max(from[1], to[1])],
      ],
      { padding: 80, maxZoom: 17 },
    )
    // Jumped rather than eased: drawing is locked out below z14 and the tool
    // is armed on the next line, so the camera has to already be there. A gap
    // wider than the screen at z14 is drawn in pieces, dragging the map along
    // in between; each piece starts on the end of the one before.
    this.map.jumpTo({
      center: (camera?.center as never) ?? [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2],
      zoom: Math.max(MIN_DRAW_ZOOM, Number(camera?.zoom ?? MIN_DRAW_ZOOM)),
    })
    this.setDrawTool('freehand')
  }

  // ------------------------------------------------------- drawing by hand

  // ---------------------------------------------------------- picking points

  /**
   * Pick the dot nearest a click. Shift picks everything from the last pick
   * to this one - a station's worth of junk is one shift-click, not forty.
   * A click on nothing lets go of what was picked.
   */
  private pick(point: { x: number; y: number }, range: boolean): void {
    const best = this.nearest(point)
    if (best === null) {
      if (!range) this.clearPicked()
      return
    }
    if (range && this.anchor !== null) {
      const [low, high] = this.anchor < best ? [this.anchor, best] : [best, this.anchor]
      for (let index = low; index <= high; index += 1) this.picked.add(index)
    } else if (this.picked.has(best) && this.picked.size === 1) {
      this.picked.clear()
    } else {
      this.picked = new Set([best])
    }
    this.anchor = best
    this.paintPicked()
    this.redraw()
  }

  /** The dot nearest a screen point, within PICK_PX, by index. */
  private nearest(point: { x: number; y: number }): number | null {
    const hits = this.map.queryRenderedFeatures(
      [
        [point.x - PICK_PX, point.y - PICK_PX],
        [point.x + PICK_PX, point.y + PICK_PX],
      ],
      { layers: [FIXES_LAYER] },
    )
    let best: number | null = null
    let bestPx = Infinity
    for (const hit of hits) {
      const index = Number(hit.properties?.index)
      // Where the dot is drawn, which for a moved point is not where the
      // phone put it.
      if (!Number.isInteger(index) || hit.geometry.type !== 'Point') continue
      const [lon, lat] = hit.geometry.coordinates as Coordinate
      const there = this.map.project([lon, lat])
      const px = Math.hypot(there.x - point.x, there.y - point.y)
      if (px < bestPx) {
        bestPx = px
        best = index
      }
    }
    return best
  }

  /** Inside the trim and not in a part left out - the points that can move. */
  private movable(
    index: number,
    from: number,
    to: number,
    dropped: Set<number>,
    owner: Map<number, number>,
  ): boolean {
    return index >= from && index <= to && !dropped.has(owner.get(index) ?? 0)
  }

  // --------------------------------------------------------- moving a point

  /**
   * A press on a dot in Drag map holds the dot instead of the map. It is a
   * drag once it has gone DRAG_PX; short of that it is a click, and selects.
   */
  private beginDrag(event: MapMouseEvent): void {
    const detail = this.current
    if (!detail || this.drawTool !== 'off' || !getToken()) return
    const index = this.nearest(event.point)
    if (index === null) return

    const { from, to } = this.trimmed
    const owner = new Map<number, number>()
    for (const segment of detail.segments) {
      if (index >= segment.begin && index <= segment.end) owner.set(index, segment.begin)
    }
    if (!this.movable(index, from, to, this.dropped, owner)) return

    // Stops MapLibre's drag-pan for this one press, and only this one.
    event.preventDefault()
    this.dragging = { index, x: event.point.x, y: event.point.y, moved: false }
  }

  private moveDrag(event: MapMouseEvent): void {
    const drag = this.dragging
    if (!drag) {
      // Saying what a press here would do: hold a dot, or move the map.
      if (this.current && this.drawTool === 'off') {
        this.map.getCanvas().style.cursor = this.nearest(event.point) === null ? '' : 'move'
      }
      return
    }
    if (!drag.moved && Math.hypot(event.point.x - drag.x, event.point.y - drag.y) < DRAG_PX) {
      return
    }
    drag.moved = true
    this.map.getCanvas().style.cursor = 'grabbing'
    this.moves.set(drag.index, [event.lngLat.lng, event.lngLat.lat])
    this.removed.delete(drag.index)
    // One redraw a frame, however fast the pointer: a day is eight thousand
    // features, and a mousemove can come several times a frame.
    if (!this.pendingFrame) {
      this.pendingFrame = window.requestAnimationFrame(() => {
        this.pendingFrame = 0
        this.redraw()
      })
    }
  }

  private endDrag(): void {
    const drag = this.dragging
    if (!drag) return
    this.dragging = null
    this.map.getCanvas().style.cursor = ''
    if (!drag.moved) return
    this.picked = new Set([drag.index])
    this.anchor = drag.index
    this.paintPicked()
    this.redraw()
    void this.queue()
  }

  private clearPicked(): void {
    this.picked.clear()
    this.anchor = null
    this.paintPicked()
    this.redraw()
  }

  /** Leave the picked points out, or put them back. Then let go of them. */
  private leaveOut(out: boolean): void {
    if (!this.picked.size) return
    for (const index of this.picked) {
      // Removing a moved point removes it; restoring puts it back where the
      // phone said, whichever of the two it was.
      this.moves.delete(index)
      if (out) this.removed.add(index)
      else this.removed.delete(index)
    }
    this.picked.clear()
    this.anchor = null
    this.paintPicked()
    this.redraw()
    void this.queue()
  }

  private paintPicked(): void {
    const count = this.picked.size
    let out = 0
    let shifted = 0
    for (const index of this.picked) {
      if (this.removed.has(index)) out += 1
      if (this.moves.has(index)) shifted += 1
    }

    const done = [
      this.removed.size ? `${this.removed.size.toLocaleString()} removed` : '',
      this.moves.size ? `${this.moves.size.toLocaleString()} moved` : '',
    ].filter(Boolean)
    const of = [
      out ? `${out.toLocaleString()} removed` : '',
      shifted ? `${shifted.toLocaleString()} moved` : '',
    ].filter(Boolean)
    element('review-pick-note').textContent = !count
      ? done.length
        ? `${done.join(', ')} by hand.`
        : 'Nothing selected.'
      : `${count.toLocaleString()} selected` + (of.length ? `, of them ${of.join(', ')}.` : '.')
    element<HTMLButtonElement>('review-pick-out').disabled = !count || out === count
    element<HTMLButtonElement>('review-pick-back').disabled = !out && !shifted
    element<HTMLButtonElement>('review-pick-clear').disabled = !count
  }

  private setDrawTool(tool: ReviewTool): void {
    const detail = this.current
    if (tool !== 'off' && (!detail || this.map.getZoom() < MIN_DRAW_ZOOM)) {
      tool = 'off'
    }
    const was = this.drawTool
    this.drawTool = tool
    if (tool === 'off') {
      if (was !== 'off') this.stopDrawing()
    } else {
      this.startDrawing(tool, this.radius, (line) => this.addStroke(line))
    }
    this.paintDrawTools()
  }

  /** The width of the next line. Lent to the brush at once if it is armed. */
  private setRadius(metres: number): void {
    if (!Number.isFinite(metres)) return
    this.radius = Math.min(RADIUS_MAX, Math.max(RADIUS_MIN, Math.round(metres)))
    if (this.drawTool !== 'off') {
      this.startDrawing(this.drawTool, this.radius, (line) => this.addStroke(line))
    }
    this.paintDrawTools()
  }

  private paintDrawTools(): void {
    // Only with a batch open and a token to save it with: a bar that draws
    // lines nobody can keep is a bar that lies.
    element('review-draw-bar').hidden = !this.current || !getToken()

    const allowed = this.map.getZoom() >= MIN_DRAW_ZOOM
    this.paintTool(this.drawTool)
    const draw = element('review-draw-tool').querySelector<HTMLButtonElement>(
      "button[data-value='freehand']",
    )
    if (draw) draw.disabled = !allowed
    element('review-draw-zoom-in').hidden = allowed

    const size = element<HTMLInputElement>('review-draw-size')
    if (Number(size.value) !== this.radius) size.value = String(this.radius)
    element('review-draw-size-label').textContent = `${this.radius} m`

    element<HTMLButtonElement>('review-draw-undo').disabled = this.strokes.length === 0

    element('review-draw-hint').textContent = !allowed
      ? `Zoom to ${MIN_DRAW_ZOOM} or closer to draw.`
      : this.drawTool === 'freehand'
        ? 'Drag along the way you went. Drag map to move on, then carry on ' +
          'from where the last line ended.'
        : ''
  }

  /** A finished line from the drawing tool, with its ends put on the track. */
  private addStroke(line: Coordinate[]): void {
    if (!this.current || line.length < 2) return
    const snapped = [...line]
    snapped[0] = this.snap(snapped[0])
    snapped[snapped.length - 1] = this.snap(snapped[snapped.length - 1])
    this.strokes = [...this.strokes, { line: snapped, radius_m: this.radius }]
    this.paintStrokes()
    this.paintDrawTools()
    this.redraw()
    void this.queue()
  }

  private removeStroke(index: number): void {
    if (index < 0 || index >= this.strokes.length) return
    this.strokes = this.strokes.filter((_, at) => at !== index)
    this.paintStrokes()
    this.paintDrawTools()
    this.redraw()
    void this.queue()
  }

  /**
   * The nearest point within SNAP_PX worth meeting: a reported fix, or the end
   * of a line already drawn. The second is what lets a gap three screens long
   * be drawn as three lines that join - draw, drag the map along, carry on
   * from where the last one stopped.
   */
  private snap(point: Coordinate): Coordinate {
    const detail = this.current
    if (!detail) return point
    const candidates: Coordinate[] = detail.fixes.map((fix): Coordinate => [fix[0], fix[1]])
    for (const stroke of this.strokes) {
      candidates.push(stroke.line[0], stroke.line[stroke.line.length - 1])
    }

    const at = this.map.project(point)
    let best: Coordinate = point
    let bestPx = SNAP_PX
    for (const candidate of candidates) {
      const there = this.map.project(candidate)
      const px = Math.hypot(there.x - at.x, there.y - at.y)
      if (px <= bestPx) {
        bestPx = px
        best = candidate
      }
    }
    return best
  }

  private paintStrokes(): void {
    const host = element('review-strokes')
    host.textContent = ''
    host.hidden = this.strokes.length === 0
    this.strokes.forEach(({ line, radius_m: radius }, index) => {
      const row = document.createElement('div')
      row.className = 'review-gap'

      const text = document.createElement('span')
      const strong = document.createElement('strong')
      strong.textContent = `Line ${index + 1}`
      const rest = document.createElement('span')
      rest.className = 'review-gap-detail'
      let metres = 0
      for (let at = 1; at < line.length; at += 1) metres += haversine(line[at - 1], line[at])
      rest.textContent = ` — ${formatDistance(metres)} · ${radius} m wide`
      text.append(strong, rest)

      const remove = document.createElement('button')
      remove.type = 'button'
      remove.textContent = 'Remove'
      remove.addEventListener('click', () => this.removeStroke(index))

      const buttons = document.createElement('span')
      buttons.className = 'review-gap-buttons'
      buttons.append(remove)
      row.append(text, buttons)
      host.append(row)
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
    this.setDrawTool('off')
    this.dragging = null
    this.picked.clear()
    this.anchor = null
    this.current = null
    this.paintDrawTools()
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
    const alone = new Set<number>()
    for (const segment of detail.segments) {
      if (segment.points <= ALONE_POINTS) alone.add(segment.begin)
      for (let index = segment.begin; index <= segment.end; index += 1) {
        owner.set(index, segment.begin)
      }
    }

    const features: unknown[] = []
    const fixes = detail.fixes
    const at = (index: number): Coordinate => [fixes[index][0], fixes[index][1]]
    const keep = fixes.map(
      (_, index) =>
        index >= from &&
        index <= to &&
        !dropped.has(owner.get(index) ?? 0) &&
        !this.removed.has(index) &&
        !this.moves.has(index),
    )

    // Whether two kept points are one line - the server's _closes, so what is
    // drawn here is what lands. Never across a part boundary; across a hole
    // only if every point in it was left out by hand and the two ends are
    // close enough that the straight line is the route.
    const closes = (previous: number, index: number): boolean => {
      if (owner.get(previous) !== owner.get(index)) return false
      if (index === previous + 1) return true
      for (let between = previous + 1; between < index; between += 1) {
        // A moved point's hole never closes: the blue line through its new
        // place is what crosses it.
        if (!this.removed.has(between) || this.moves.has(between)) return false
      }
      return haversine(at(previous), at(index)) <= HEAL_METRES
    }

    const line = (coordinates: Coordinate[], kept: boolean) => {
      features.push({
        type: 'Feature',
        properties: { keep: kept },
        geometry: {
          type: 'LineString',
          coordinates: coordinates.length === 1 ? [coordinates[0], coordinates[0]] : coordinates,
        },
      })
    }

    // What lands, as runs of kept points.
    let keptFirst: Coordinate | null = null
    let keptLast: Coordinate | null = null
    let keptCount = 0
    let keptMetres = 0
    let run: Coordinate[] = []
    let previous: number | null = null
    for (let index = 0; index < fixes.length; index += 1) {
      if (!keep[index]) continue
      const point = at(index)
      keptCount += 1
      if (!keptFirst) keptFirst = point
      keptLast = point
      if (previous !== null && closes(previous, index)) {
        keptMetres += haversine(at(previous), point)
        run.push(point)
      } else {
        if (run.length) line(run, true)
        run = [point]
      }
      previous = index
    }
    if (run.length) line(run, true)

    // What is left behind, dashed underneath, reaching to the kept points
    // either side so a trim or a hole reads as a boundary rather than as data
    // missing. Never across a part boundary, which is the flight.
    let left: Coordinate[] = []
    let leftOwner = -1
    const closeLeft = (next: number | null) => {
      if (!left.length) return
      if (next !== null && keep[next] && owner.get(next) === leftOwner) left.push(at(next))
      line(left, false)
      left = []
    }
    for (let index = 0; index < fixes.length; index += 1) {
      const segment = owner.get(index) ?? 0
      if (keep[index]) {
        closeLeft(index)
        continue
      }
      if (left.length && segment !== leftOwner) closeLeft(null)
      if (!left.length) {
        leftOwner = segment
        if (index > 0 && keep[index - 1] && owner.get(index - 1) === segment) {
          left.push(at(index - 1))
        }
      }
      left.push(at(index))
    }
    closeLeft(null)

    // Moved points, as the lines accepting will draw: neighbours moved
    // together are one bend, from the kept point before to the kept point
    // after - the server's _write_moves.
    const placed = [...this.moves.keys()]
      .filter((index) => this.movable(index, from, to, dropped, owner))
      .sort((a, b) => a - b)
    for (let first = 0; first < placed.length; ) {
      let last = first
      while (last + 1 < placed.length && placed[last + 1] === placed[last] + 1) last += 1
      const bend: Coordinate[] = []
      const before = placed[first] - 1
      const after = placed[last] + 1
      if (keep[before]) bend.push(at(before))
      for (let run = first; run <= last; run += 1) {
        bend.push(this.moves.get(placed[run]) as Coordinate)
      }
      if (keep[after]) bend.push(at(after))
      features.push({
        type: 'Feature',
        properties: { stroke: true },
        geometry: { type: 'LineString', coordinates: bend.length === 1 ? [bend[0], bend[0]] : bend },
      })
      first = last + 1
    }

    fixes.forEach((fix, index) => {
      const segment = owner.get(index) ?? 0
      const place = this.moves.get(index)
      if (place) {
        features.push({
          type: 'Feature',
          properties: { fix: true, index, keep: true, moved: true, picked: this.picked.has(index) },
          geometry: { type: 'Point', coordinates: place },
        })
      }
      const accuracy = fix[3]
      features.push({
        type: 'Feature',
        properties: {
          fix: true,
          index,
          keep: keep[index],
          alone: alone.has(segment),
          picked: this.picked.has(index),
          ...(typeof accuracy === 'number' ? { accuracy } : {}),
        },
        geometry: { type: 'Point', coordinates: at(index) },
      })
    })

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

    for (const stroke of this.strokes) {
      features.push({
        type: 'Feature',
        properties: { stroke: true },
        geometry: { type: 'LineString', coordinates: stroke.line },
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
      removed: [...this.removed],
      moved: [...this.moves].map(([index, [lon, lat]]) => [index, lon, lat]),
      strokes: this.strokes,
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
      this.strokes = []
      this.removed.clear()
      this.moves.clear()
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
        drawn: number
        stretches: number
        tiles_touched: number
      }>('POST', `/api/review/${detail.id}/approve`)
      const stretches =
        done.stretches > 1 ? ` as ${done.stretches} stretches` : ''
      const summary =
        `Added ${done.points.toLocaleString()} points${stretches}` +
        (done.left_out ? `, ${done.left_out.toLocaleString()} left out` : '') +
        (done.drawn ? `, ${done.drawn} drawn by hand` : '')

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
