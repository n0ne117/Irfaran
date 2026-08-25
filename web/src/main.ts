// SPDX-License-Identifier: AGPL-3.0-or-later

import 'maplibre-gl/dist/maplibre-gl.css'

import type { Map as MapLibreMap } from 'maplibre-gl'

import {
  countFrames,
  recordMapError,
  watchLifecycle,
  wireDiagnostics,
} from './diagnostics'
import { ApiError, apiGet, apiSend, getToken } from './api'
import { Brush } from './brush'
import { Draw, MIN_DRAW_ZOOM, type Tool } from './draw'
import { Backup } from './backup'
import { Imports } from './imports'
import { carryOldSettings } from './legacy'
import {
  applyBorders,
  applyScale,
  applyFogOpacity,
  applyMapTheme,
  applyView,
  basemapAvailable,
  bustTileCache,
  buildStyle,
  createMap,
  getBordersVisible,
  getScaleVisible,
  getFogOpacity,
  getHeatOpacity,
  openArchive,
  pmtilesProtocol,
  setArchiveVisible,
  applyHeatOpacity,
  setBordersVisible,
  setScaleVisible,
  setFogOpacity,
  setHeatOpacity,
  type MapSetup,
} from './map'
import { Labels } from './labels'
import { Gazetteer } from './gazetteer'
import { getPinsVisible, Places, setPinsVisible } from './places'
import { PinImport, describeStaged, type StageReport } from './pinimport'
import { Review } from './review'
import { Search } from './search'
import { describeRemaining, runRender, watchRender } from './render'
import { Setup } from './setup'
import { hydrateIcons, setIcon } from './icons'
import { History } from './history'
import { People } from './people'
import { Progress } from './progress'
import { Sources } from './sources'
import { Trackers } from './trackers'
import { Timeline } from './timeline'
import {
  getTrailPopups,
  getTrailStyle,
  setTrailPopups,
  setTrailStyle,
  Trails,
  type TrailStyle,
} from './trails'
import {
  applyUiTheme,
  getMapTheme,
  getUiTheme,
  setMapTheme,
  setUiTheme,
  watchSystemTheme,
  type MapTheme,
  type UiTheme,
} from './theme'
import {
  element,
  notice,
  radioGroup,
  Sheets,
  wireTabs,
  wireTokenField,
  wireZoom,
  type Notice,
} from './ui'

/**
 * The drawing tools, drivable from outside.
 *
 * Handing a gap in a track over to the Track tool means arming it from the
 * review sidebar, which is on the other side of the app. Rather than a second
 * drawing implementation inside the review - undo stack, zoom lock, brush ring
 * and all - the one that exists is given a door.
 */
interface Drawing {
  draw: Draw
  arm(tool: Tool): void
  putAway(): void
}

/** Named trail colour ramps, matching composite.TRAIL_RAMP_SETS. */
type TrailRamp = 'ember' | 'ice' | 'moss' | 'mono'

const REPO_URL = 'https://github.com/n0ne117/Irfaran'
const CHANGELOG_URL = `${REPO_URL}/blob/main/CHANGELOG.md`
const webVersion = __IRFARAN_VERSION__

function showVersion(): void {
  const corner = element<HTMLAnchorElement>('version-corner')
  corner.textContent = `v${webVersion}`
  corner.href = `${REPO_URL}/releases/tag/v${webVersion}`
  corner.title = `Irfaran ${webVersion} — release notes`

  const link = element<HTMLAnchorElement>('version-link')
  link.textContent = `v${webVersion}`
  link.href = CHANGELOG_URL
}

/**
 * One version is enough. The api is mentioned only when it disagrees, which
 * is the only moment the distinction is worth anyone's attention.
 */
async function checkApiVersion(): Promise<void> {
  const mismatch = element('version-mismatch')
  try {
    const response = await fetch('/healthz', { headers: { accept: 'application/json' } })
    const body = (await response.json()) as { version?: string }
    const apiVersion = body.version ?? 'unknown'

    if (apiVersion === webVersion) return
    mismatch.textContent =
      `The api reports ${apiVersion} but this page was built from ` +
      `${webVersion}. One of the two containers is out of date.`
    mismatch.hidden = false
  } catch {
    mismatch.textContent = 'The api is unreachable.'
    mismatch.hidden = false
  }
}

/** Built-in fog colours, matching composite.FOG_COLOUR on the server. */
/**
 * What the server falls back to when no colour has been stored.
 *
 * A second copy of composite.FOG_COLOUR, because the settings endpoint returns
 * what is stored and not what is defaulted, so the picker has nothing else to
 * show on an instance that has never set one. test_fog_defaults.py fails if
 * these two drift - which they did, the first time this changed.
 */
const FOG_COLOUR_DEFAULTS: Record<MapTheme, string> = {
  dark: '#5e5c64',
  light: '#e8e8e4',
}

/**
 * The fog colour picker.
 *
 * Fog colour is baked into the tiles, so unlike thickness it costs a render
 * and cannot be previewed as the wheel moves. It is per theme, because a
 * colour that reads as haze over a dark basemap is a fog bank over a light
 * one - so the control follows whichever map theme is selected.
 */
function wireFogColour(map: MapLibreMap, options: MapSetup): { load: () => Promise<void> } {
  const wheel = element<HTMLInputElement>('fog-colour')
  const hex = element<HTMLInputElement>('fog-colour-hex')
  const apply = element<HTMLButtonElement>('fog-colour-apply')
  const themeLabel = element('fog-colour-theme')
  const status = element('fog-colour-status')

  const key = () => `fog_colour_${options.theme}`

  const show = (value: string) => {
    wheel.value = value
    hex.value = value
  }

  const load = async () => {
    themeLabel.textContent = options.theme
    show(FOG_COLOUR_DEFAULTS[options.theme])
    try {
      const body = await apiGet<{ settings: Record<string, string> }>('/api/settings')
      const stored = body.settings?.[key()]
      if (stored) show(stored)
    } catch {
      /* the built-in is a fine thing to show when settings will not load */
    }
  }

  wheel.addEventListener('input', () => (hex.value = wheel.value))
  hex.addEventListener('change', () => {
    if (/^#?[0-9a-fA-F]{6}$/.test(hex.value.trim())) {
      wheel.value = hex.value.trim().startsWith('#') ? hex.value.trim() : `#${hex.value.trim()}`
    }
  })

  apply.addEventListener('click', () => {
    const value = hex.value.trim() || wheel.value
    apply.disabled = true
    status.hidden = false
    status.dataset.state = ''
    status.textContent =
      'The colour is baked into every tile, so this re-renders all of them. ' +
      'On a large archive that is several minutes. Settings are locked until it finishes.'

    void apiSend('PATCH', '/api/settings', { [key()]: value })
      .then(() =>
        runRender((state) => {
          status.textContent =
            `Recolouring the fog — ${describeRemaining(state)}. ` +
            'This carries on if you close the browser.'
        }),
      )
      .then(() => {
        status.textContent = 'Fog recoloured.'
        bustTileCache()
        applyView(map, options)
      })
      .catch((error: unknown) => {
        status.dataset.state = 'bad'
        status.textContent = error instanceof ApiError ? error.message : String(error)
      })
      .finally(() => (apply.disabled = false))
  })

  void load()
  return { load }
}

/** What each tool wants you to do with the pointer. */
const HINTS: Record<Tool, string> = {
  off: 'Drag to pan. Pick a tool to start drawing.',
  freehand: 'Drag on the map to draw a route.',
  line: 'Click to add points, double click to finish.',
  reveal: 'Drag to clear fog without drawing a route through it.',
  area: 'Click round the edge, double click to close it.',
  eraser: 'Drag to put the fog back. The track underneath stays.',
}

function wireDrawing(
  map: MapLibreMap,
  options: MapSetup,
  timeline: Timeline,
  trails: Trails,
  brush: Brush,
  status: Notice,
  onStroke: () => void,
): Drawing {
  const hint = element('draw-hint')
  const undoButton = element<HTMLButtonElement>('draw-undo')

  const draw = new Draw(
    map as never,
    () => {
      bustTileCache()
      applyView(map, options)
      void timeline.load()
      void trails.refresh()
      refreshUndo()
      onStroke()
    },
    (message, bad) => status.show(message, bad),
    // Rasterising a stroke into every view takes seconds on a full archive,
    // and the only sign of it used to be the preview refusing to disappear.
    (done, total) => status.progress(done, total, 'Drawing…'),
  )
  draw.attach()
  draw.onPreview = (points) => brush.preview(points)

  // The ring follows the pointer over the map, and goes away when it leaves.
  const canvas = map.getCanvas()
  canvas.addEventListener('pointermove', (event) => brush.track(event))
  canvas.addEventListener('pointerenter', (event) => brush.track(event))
  canvas.addEventListener('pointerleave', () => brush.hideRing())

  // Deleting a stroke rebuilds tiles, which takes long enough to look like
  // nothing happened. The button says what it is doing instead.
  const refreshUndo = () => {
    undoButton.disabled = draw.busy
    undoButton.textContent = draw.busy ? 'Undoing…' : 'Undo'
  }

  // Being a fraction of a level short of z14 is not worth making anyone solve
  // with a scroll wheel.
  const zoomToDraw = element<HTMLButtonElement>('draw-zoom-in')
  zoomToDraw.addEventListener('click', () => map.easeTo({ zoom: MIN_DRAW_ZOOM }))

  let paintTool: (value: Tool) => void = () => {}

  /** Drawing below z14 produces meaningless geometry, so it is locked out. */
  const refreshLock = () => {
    const allowed = draw.canDraw
    const zoom = map.getZoom()

    const group = element('draw-tool')
    group.dataset.locked = String(!allowed)
    for (const button of group.querySelectorAll('button')) button.disabled = !allowed

    hint.textContent = allowed
      ? (HINTS[draw.activeTool] ?? '')
      : // Floored, not rounded. At 13.96 a rounded reading says "Currently
        // 14.0" next to a message demanding zoom 14, which reads as a broken
        // lock rather than as being a fraction of a level short.
        `Zoom to ${MIN_DRAW_ZOOM} or closer to draw. Currently ${
          Math.floor(zoom * 10) / 10
        }.`

    zoomToDraw.hidden = allowed

    if (!allowed && draw.activeTool !== 'off') {
      draw.setTool('off')
      brush.setTool('off')
      paintTool('off')
    }
  }

  paintTool = radioGroup<Tool>('draw-tool', 'off', (value) => {
    draw.setTool(value)
    brush.setTool(value)
    refreshLock()
  })
  element<HTMLInputElement>('draw-layers').addEventListener('input', (event) => {
    draw.layers = (event.target as HTMLInputElement).value
  })
  // Brush width has two controls - a slider on the toolbar and a number field
  // in settings - because both are the right one at different moments. They
  // are the same setting, so each follows the other.
  const radiusField = element<HTMLInputElement>('draw-radius')
  const radiusSlider = element<HTMLInputElement>('draw-size')
  const radiusLabel = element<HTMLOutputElement>('draw-size-label')

  const applyRadius = (value: number) => {
    if (!Number.isFinite(value) || value <= 0) return
    draw.radiusM = value
    brush.setRadius(value)
    radiusLabel.textContent = `${value} m`
    if (Number(radiusField.value) !== value) radiusField.value = String(value)
    if (Number(radiusSlider.value) !== value) radiusSlider.value = String(value)
  }

  radiusField.addEventListener('input', (event) =>
    applyRadius(Number((event.target as HTMLInputElement).value)),
  )
  radiusSlider.addEventListener('input', (event) =>
    applyRadius(Number((event.target as HTMLInputElement).value)),
  )

  // Steppers, for the last metre or two the slider makes fiddly.
  const step = (by: number) => {
    const low = Number(radiusSlider.min)
    const high = Number(radiusSlider.max)
    applyRadius(Math.min(high, Math.max(low, draw.radiusM + by)))
  }
  element('draw-size-down').addEventListener('click', () => step(-1))
  element('draw-size-up').addEventListener('click', () => step(1))

  applyRadius(Number(radiusField.value) || draw.radiusM)
  undoButton.addEventListener('click', () => {
    void draw.undo()
    refreshUndo()
  })
  refreshUndo()

  // The pencil opens the toolbar. Closing it puts the brush away too, so the
  // map is never left in drawing mode with nothing on screen saying so.
  const bar = element('draw-bar')
  const toggle = element('draw-toggle')
  toggle.addEventListener('click', () => {
    const opening = bar.hidden
    bar.hidden = !opening
    toggle.setAttribute('aria-pressed', String(opening))
    if (!opening) {
      draw.setTool('off')
      brush.setTool('off')
      paintTool('off')
    }
    refreshLock()
  })

  map.on('zoomend', refreshLock)
  map.on('load', refreshLock)
  refreshLock()

  return {
    draw,
    arm(tool: Tool) {
      bar.hidden = false
      toggle.setAttribute('aria-pressed', 'true')
      draw.setTool(tool)
      brush.setTool(tool)
      paintTool(tool)
      refreshLock()
    },
    putAway() {
      draw.setTool('off')
      brush.setTool('off')
      paintTool('off')
      bar.hidden = true
      toggle.setAttribute('aria-pressed', 'false')
      refreshLock()
    },
  }
}

/**
 * Wire one part of the interface, and survive it failing.
 *
 * These calls used to run in a straight line, so the first one to throw took
 * every handler after it with it - and threw nothing on screen. A stale element
 * id in the search bar would leave the Import button inert-looking and
 * unexplained, thirty lines further down, with the console the only clue.
 *
 * Reported exactly that way: "a token is set, but the Import button does not
 * react". Whatever the cause turns out to be, one broken panel taking the rest
 * of the page with it is a separate fault, and this is that fault.
 */
/**
 * Which kinds of thing the search bar looks through.
 *
 * Server settings rather than browser ones, because the filtering happens where
 * the searching happens - and because a preference about your own archive
 * belongs to the archive rather than to whichever browser you last used.
 *
 * Tracks are off to begin with: a name search otherwise answers mostly with
 * track segments. Coordinates are on, which is a deliberate exception to "pins
 * only" - pasting a coordinate reads what was typed rather than searching
 * anything stored, so defaulting it off would remove a working feature instead
 * of quietening a noisy one.
 */
function wireSearchSettings(): void {
  const status = notice('search-settings-status')
  const boxes: Record<string, HTMLInputElement> = {
    search_pins: element<HTMLInputElement>('search-pins'),
    search_tracks: element<HTMLInputElement>('search-tracks'),
    search_coordinates: element<HTMLInputElement>('search-coordinates'),
    search_plus_codes: element<HTMLInputElement>('search-plus-codes'),
    search_place_names: element<HTMLInputElement>('search-place-names'),
    search_pois: element<HTMLInputElement>('search-pois'),
  }

  for (const [key, box] of Object.entries(boxes)) {
    box.addEventListener('change', () => {
      void apiSend('PATCH', '/api/settings', { [key]: String(box.checked) })
        .then(() => status.show(''))
        .catch((error: unknown) => {
          // Put it back: nothing changed on the server, so nothing should look
          // as though it did.
          box.checked = !box.checked
          status.show(
            error instanceof ApiError ? error.message : String(error),
            true,
          )
        })
    })
  }

  void apiGet<{ settings: Record<string, string> }>('/api/settings')
    .then((body) => {
      const stored = body.settings ?? {}
      const fallback: Record<string, string> = {
        search_pins: 'true',
        search_tracks: 'false',
        search_coordinates: 'true',
        search_plus_codes: 'false',
        search_place_names: 'false',
        search_pois: 'false',
      }
      for (const [key, box] of Object.entries(boxes)) {
        box.checked = (stored[key] ?? fallback[key]) === 'true'
      }
    })
    .catch(() => {})
}

/**
 * Which automatic sources wait to be reviewed.
 *
 * Ordinary settings rows, like the search toggles, so the server has one way
 * of storing a preference rather than an endpoint per feature. Painted from
 * /api/review, which reports the gates alongside what is waiting.
 */
function wireReviewGates(refresh: () => void): void {
  const status = notice('review-gates-status')
  for (const source of ['workout', 'overland', 'owntracks', 'ha']) {
    const box = element<HTMLInputElement>(`review-${source}`)
    box.addEventListener('change', () => {
      apiSend('PATCH', '/api/settings', { [`review_${source}`]: String(box.checked) })
        .then(() => {
          status.show(
            box.checked
              ? `${source} will wait to be reviewed.`
              : `${source} will go straight onto the map.`,
          )
          refresh()
        })
        .catch((error: unknown) => {
          box.checked = !box.checked
          status.show(
            error instanceof ApiError ? error.message : 'Could not save that.',
            true,
          )
        })
    })
  }
}

/**
 * Follow whatever the queue is drawing, on the bar above the time bar.
 *
 * Anything that defers a render owes somebody this. Drawing has done it since
 * strokes stopped rendering inline; accepting a reviewed track did not, so the
 * map redrew itself in silence and the only sign was tiles changing underneath
 * you. Reported as "whenever a track is accepted it gets drawn - this needs to
 * be represented by the progress bar on top of the time bar".
 *
 * An indeterminate bar goes up first rather than waiting for the first poll:
 * a small accept can be finished before a poll comes back, and a bar that
 * never appears is indistinguishable from one that is broken.
 *
 * It always ends on a message rather than on a bar, because painting progress
 * cancels the timer that hides a notice - the reason a bar once sat at three
 * quarters for good.
 */
async function followTheQueue(status: Notice, summary: string): Promise<void> {
  status.progress(0, 0, summary)
  const finished = await watchRender((state) => {
    if (state.state === 'running' || state.state === 'stopping') {
      status.progress(state.done, state.total, `${summary} — drawing the map`)
    }
  })
  status.show(
    finished === null
      ? `${summary}. Still drawing on the server — Settings, In progress ` +
        'shows where it got to.'
      : summary,
  )
}

function wirePart(name: string, wire: () => void): void {
  try {
    wire()
  } catch (error) {
    console.error(`Irfaran: ${name} failed to wire up`, error)
    const line = document.getElementById('wiring-error')
    if (!line) return
    const already = line.dataset.parts ? `${line.dataset.parts}, ` : ''
    line.dataset.parts = `${already}${name}`
    line.textContent =
      `Some of the interface could not start: ${line.dataset.parts}. ` +
      'The rest still works. The browser console has the detail.'
    line.hidden = false
  }
}

async function start(): Promise<void> {
  // Before anything reads a preference: the browser stored them all under the
  // old name until 0.10.0, and the API token is among them.
  carryOldSettings()

  countFrames()
  showVersion()
  void checkApiVersion()

  applyUiTheme()
  watchSystemTheme(() => applyUiTheme())

  const hasBasemap = await basemapAvailable()
  const options: MapSetup = {
    container: 'map',
    theme: getMapTheme(),
    view: Timeline.remembered(),
    hasBasemap,
  }

  const mapError = notice('map-error')

  let map: MapLibreMap
  try {
    map = createMap(options)
  } catch (error) {
    mapError.show('The map could not start. Check the browser console.', true)
    console.error('Irfaran could not create the map', error)
    return
  }

  map.on('error', (event) => {
    const message = event.error?.message ?? String(event)
    console.warn('maplibre', message)
    recordMapError(message)

    // Tiles missing at the edge of the world are normal; anything about the
    // basemap or the style is worth putting in front of someone.
    if (/pmtiles|protomaps|style|source|glyph|sprite/i.test(message)) {
      mapError.show(`Map problem: ${message}`, true)
    }
  })

  const handle: Record<string, unknown> = { map, options, buildStyle, openArchive, pmtilesProtocol }
  ;(window as unknown as { irfaran: unknown }).irfaran = handle

  // The sheets are mutually exclusive: opening places closes settings.
  //
  // Declared before the panels that use it and told about the review sidebar
  // afterwards, because the sidebar needs the map and the map is what this
  // whole function is building.
  let sheetsChanged: () => void = () => {}
  const sheets = new Sheets(
    ['panel', 'places-page', 'review-page', 'pin-import-page'],
    () => sheetsChanged(),
  )
  element('panel-toggle').addEventListener('click', () => sheets.toggle('panel'))
  element('panel-close').addEventListener('click', () => sheets.close())
  element('places-toggle').addEventListener('click', () => sheets.toggle('places-page'))
  element('places-close').addEventListener('click', () => sheets.close())
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') sheets.close()
  })

  // Static chrome that asked for an icon in the markup gets it here.
  hydrateIcons()

  const progress = new Progress(() => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
    void trails.refresh()
  })
  wirePart('progress', () => progress.wire())

  // Panels that poll say so here, and are told which tab is showing. One
  // dispatcher rather than a tab listener each: there is no reason to ask the
  // server every second about a panel nobody is looking at, and two listeners
  // on the same buttons is two chances to disagree about which tab that is.
  const watchers: ((tab: string) => void)[] = [
    (tab) => progress.watch(tab === 'progress'),
  ]
  wireTabs('tabs', (tab) => {
    for (const watcher of watchers) watcher(tab)
  })
  wireZoom(map as never)
  watchLifecycle(map)
  wireDiagnostics(map, hasBasemap, options.theme)

  // Fog thickness is a viewing choice applied on the GPU: it changes as the
  // slider moves, with no re-render and no request to the server.
  const fogSlider = element<HTMLInputElement>('fog-opacity')
  const fogValue = element('fog-opacity-value')
  const paintFog = (percent: number) => {
    fogValue.textContent = `${Math.round(percent)}%`
  }
  fogSlider.value = String(Math.round(getFogOpacity() * 100))
  paintFog(Number(fogSlider.value))
  fogSlider.addEventListener('input', () => {
    const percent = Number(fogSlider.value)
    paintFog(percent)
    setFogOpacity(map, percent / 100)
  })

  // Trail colouring: strength is a viewing choice on the GPU, the colours are
  // baked into the tiles and cost a render.
  const heatSlider = element<HTMLInputElement>('heat-opacity')
  const heatValue = element('heat-opacity-value')
  heatSlider.value = String(Math.round(getHeatOpacity() * 100))
  heatValue.textContent = `${heatSlider.value}%`
  heatSlider.addEventListener('input', () => {
    heatValue.textContent = `${heatSlider.value}%`
    setHeatOpacity(map, Number(heatSlider.value) / 100)
  })

  const rampStatus = element('trail-ramp-status')
  let rampReady = false
  const rampButtons = radioGroup<TrailRamp>('trail-ramp', 'ember', (value) => {
    if (!rampReady) return
    rampStatus.hidden = false
    rampStatus.dataset.state = ''
    rampStatus.textContent =
      'The colours are baked into every tile, so this re-renders all of them. ' +
      'On a large archive that is several minutes. Settings are locked until it finishes.'

    void apiSend('PATCH', '/api/settings', { trail_ramp: value })
      .then(() =>
        runRender((state) => {
          rampStatus.textContent =
            `Recolouring the trails — ${describeRemaining(state)}. ` +
            'This carries on if you close the browser.'
        }),
      )
      .then(() => {
        rampStatus.textContent = 'Trails recoloured.'
        bustTileCache()
        applyView(map, options)
      })
      .catch((error: unknown) => {
        rampStatus.dataset.state = 'bad'
        rampStatus.textContent = error instanceof ApiError ? error.message : String(error)
      })
  })
  void apiGet<{ settings: Record<string, string> }>('/api/settings')
    .then((body) => rampButtons((body.settings?.trail_ramp ?? 'ember') as TrailRamp))
    .catch(() => {})
    .finally(() => (rampReady = true))

  const trailPopups = element<HTMLInputElement>('trail-popups')
  trailPopups.checked = getTrailPopups()
  trailPopups.addEventListener('change', () => setTrailPopups(trailPopups.checked))

  wireSearchSettings()

  const borders = element<HTMLInputElement>('show-borders')
  borders.checked = getBordersVisible()
  borders.addEventListener('change', () => setBordersVisible(map, borders.checked))

  // A class on the container rather than a style layer, so unlike the borders
  // it needs no reapplying after a restyle.
  const scale = element<HTMLInputElement>('show-scale')
  scale.checked = getScaleVisible()
  scale.addEventListener('change', () => setScaleVisible(map, scale.checked))
  applyScale(map)

  const fogColour = wireFogColour(map, options)

  radioGroup<UiTheme>('ui-theme', getUiTheme(), (value) => setUiTheme(value))
  radioGroup<MapTheme>('map-theme', getMapTheme(), (value) => {
    setMapTheme(value)
    options.theme = value
    applyMapTheme(map, options)
    void fogColour.load()
  })

  const trailNotice = notice('trail-notice')
  const trails = new Trails(map, (message) => {
    if (message) trailNotice.show(message)
    else trailNotice.hide()
  })
  const brush = new Brush(map, element('draw-cursor'))
  const attachTrails = () => {
    try {
      applyFogOpacity(map)
      applyHeatOpacity(map)
      applyBorders(map)
      trails.attach()
      void trails.refresh()
    } catch (error) {
      console.error('Irfaran could not attach the trail layer', error)
    }
    // Its own try: losing the trail layer should not also cost the drawing
    // preview, which is what someone is actively looking at when it matters.
    // Added after the trails, so a stroke in progress is never underneath the
    // tracks it is being drawn between.
    try {
      brush.attach()
    } catch (error) {
      console.error('Irfaran could not attach the drawing preview', error)
    }
  }
  map.on('style.load', attachTrails)
  if (map.isStyleLoaded()) attachTrails()

  // Restyled rather than refreshed: which way the same tracks are drawn does
  // not depend on fetching them again, and a round trip to change a paint
  // property is a round trip nobody asked for.
  radioGroup<TrailStyle>('trail-style', getTrailStyle(), (value) => {
    setTrailStyle(value)
    trails.restyle()
  })

  const timeline = new Timeline((view) => {
    options.view = view
    applyView(map, options)
    trails.view = view
    void trails.refresh()
  })
  void timeline.load()

  // One notice for the bar above the time bar, made here rather than inside
  // wireDrawing: more than one thing draws the map now, and two notice()
  // instances over the same element replace each other's contents.
  const drawStatus = notice('draw-status')

  // Set while a gap in a review is being drawn by hand, and run by the next
  // stroke that lands. One shot: whatever the stroke was, the review is what
  // we came from and what we go back to.
  let backFromDrawing: (() => void) | null = null
  const drawing = wireDrawing(
    map,
    options,
    timeline,
    trails,
    brush,
    drawStatus,
    () => {
      const back = backFromDrawing
      backFromDrawing = null
      back?.()
    },
  )
  const draw = drawing.draw

  const places = new Places(map, () => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
  })
  wirePart('places', () => places.wire())
  void places.load()

  // Every pin off and on again, beside the Places button. A viewing choice
  // like the fog slider: nothing is asked of the server, nothing is
  // re-rendered, and an archive full of pins can be got out of the way to look
  // at the fog underneath. Its own state, deliberately not the one a review
  // sidebar uses to hold the pins off - see Places.suspend.
  wirePart('pins-toggle', () => {
    const button = element<HTMLButtonElement>('pins-toggle')
    const paint = () => {
      const visible = getPinsVisible()
      button.setAttribute('aria-pressed', String(!visible))
      // The icon says what is on screen, and the label says what pressing it
      // would do, which is the way round people read a toggle.
      setIcon(button, visible ? 'eye' : 'eye-off', 17)
      const label = visible ? 'Hide all pins' : 'Show all pins'
      button.title = label
      button.setAttribute('aria-label', label)
      places.applyVisible()
    }
    button.addEventListener('click', () => {
      setPinsVisible(!getPinsVisible())
      paint()
    })
    paint()
  })

  // Search drops a pin nobody has saved yet, so keeping one has to reach the
  // sidebar and the map the same way dropping one by hand does.
  const search = new Search(map, () => {
    void places.load()
    bustTileCache()
    applyView(map, options)
  })
  wirePart('search', () => search.wire())

  // Reading names out of the basemap. Its own panel on the Search page and one
  // line on In progress, both painted from the same poll - two independent
  // views of one job is how an import came to sit at 100% after finishing.
  const gazetteer = new Gazetteer(() => {
    bustTileCache()
    applyView(map, options)
  })
  wirePart('gazetteer', () => gazetteer.wire())
  watchers.push((tab) => gazetteer.watch(tab === 'search' || tab === 'progress'))

  // Pins imported out of another application's database. Nothing in the
  // interface points at this; it opens when such a file is dropped into the
  // Import picker and not otherwise.
  const pins = new PinImport(map, {
    onOpen: () => sheets.open('pin-import-page'),
    onCommitted: () => {
      void places.load()
      bustTileCache()
      applyView(map, options)
      void timeline.load()
    },
    setRestVisible: (visible) => {
      setArchiveVisible(map, visible)
      trails.suspend(!visible)
      places.suspend(!visible)
    },
  })
  wirePart('pin-import', () => pins.wire())
  // An import left open is picked up again rather than stranded.
  void pins.resume()

  // The holding pen. Nothing automatic reaches the map until it has been
  // looked at, and looking at it means seeing it on its own: the fog and the
  // trails are held off while one candidate route is on screen, and put back
  // the moment the sidebar closes however it was closed.
  const review = new Review(map, {
    onOpen: () => sheets.open('review-page'),
    onApproved: (summary) => {
      bustTileCache()
      applyView(map, options)
      void timeline.load()
      void trails.refresh()
      // Accepting a track is a render, and it is the same render drawing a
      // stroke is - so it is reported the same way, in the same place.
      void followTheQueue(drawStatus, summary)
    },
    setRestVisible: (visible) => {
      setArchiveVisible(map, visible)
      trails.suspend(!visible)
    },
    // Hand a gap over to the Track tool, and come back afterwards.
    //
    // The sidebar is hidden directly rather than through the sheets, so the
    // review keeps its candidate on the map and its place in the list - the
    // point of doing this here rather than later is that you are looking at
    // the gap and know where you went, and that is gone tomorrow.
    onDrawGap: (from, to, year) => {
      const camera = map.cameraForBounds(
        [
          [Math.min(from[0], to[0]), Math.min(from[1], to[1])],
          [Math.max(from[0], to[0]), Math.max(from[1], to[1])],
        ],
        { padding: 80, maxZoom: 17 },
      )
      // Jumped rather than eased: drawing is locked out below z14 and the tool
      // is armed on the next line, so the camera has to already be there.
      map.jumpTo({
        center: (camera?.center as never) ?? [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2],
        zoom: Math.max(MIN_DRAW_ZOOM, Number(camera?.zoom ?? MIN_DRAW_ZOOM)),
      })

      const before = draw.layers
      // The hand-drawn piece belongs to the same year as the track it fills a
      // hole in, not to prehistory.
      if (year) draw.layers = year
      element('review-page').hidden = true
      drawing.arm('freehand')
      drawStatus.show(
        'Draw the stretch the phone missed. It is saved as a hand-drawn ' +
          'route, and the review is waiting.',
      )

      backFromDrawing = () => {
        draw.layers = before
        drawing.putAway()
        sheets.open('review-page')
      }
    },
  })
  sheetsChanged = () => {
    review.closed()
    pins.closed()
  }
  const attachReview = () => {
    try {
      review.attach()
    } catch (error) {
      console.error('Irfaran could not attach the review layer', error)
    }
  }
  map.on('style.load', attachReview)
  if (map.isStyleLoaded()) attachReview()
  wirePart('review', () => review.wire())
  wirePart('review-gates', () => wireReviewGates(() => void review.load()))
  review.watch(true)

  // Labels are a setting, but the pins wear them, so changing one has to
  // reach the map.
  const labels = new Labels(() => void places.load())
  wirePart('labels', () => labels.wire())
  void labels.load()

  const sources = new Sources()
  void sources.load()

  // Loaded when the tab is first opened rather than on startup: it is a page
  // nobody has looked at yet, and a query for it is a query for nothing.
  const people = new People(() => void places.load())
  wirePart('people', () => people.wire())
  void people.load()

  const history = new History()
  wirePart('history', () => history.wire())
  document
    .querySelector<HTMLButtonElement>('#tabs [data-tab="history"]')
    ?.addEventListener('click', () => void history.loadOnce())

  const trackers = new Trackers(() => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
    void trails.refresh()
  })
  wirePart('trackers', () => trackers.wire())
  void trackers.load()

  wireTokenField(() => {
    void sources.load()
    void trackers.load()
  })

  const imports = new Imports(
    () => {
      bustTileCache()
      applyView(map, options)
      void timeline.load()
      void trails.refresh()
    },
    // A places database from another application. Staged, never added, so
    // there is nothing to redraw here - the sidebar opens instead.
    async (file) => {
      const body = new FormData()
      body.append('file', file)
      const response = await fetch('/api/import/pins', {
        method: 'POST',
        headers: { 'X-Irfaran-Token': getToken() },
        body,
      })
      const text = await response.text()
      let parsed: unknown = null
      try {
        parsed = JSON.parse(text)
      } catch {
        /* fall through to the status line */
      }
      if (!response.ok) {
        const detail =
          parsed && typeof parsed === 'object' && 'detail' in parsed
            ? String((parsed as { detail: unknown }).detail)
            : `${response.status} ${response.statusText}`
        throw new ApiError(response.status, detail)
      }
      await pins.begin()
      return describeStaged(parsed as StageReport)
    },
  )
  wirePart('imports', () => imports.wire())

  const backup = new Backup(() => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
    void trails.refresh()
    void places.load()
    void labels.load()
  })
  wirePart('backup', () => backup.wire())

  // A brand new instance is exactly where a backup is most useful, so the
  // setup screen offers it rather than making somebody find the tab first.
  void Backup.isEmpty().then((empty) => {
    element('setup-restore-row').hidden = !empty
  })
  element('setup-restore').addEventListener('click', () => {
    setup.close()
    sheets.open('panel')
    document
      .querySelector<HTMLButtonElement>('#tabs [data-tab="backup"]')
      ?.click()
  })

  const setup = new Setup(() => {
    if (options.hasBasemap) return
    options.hasBasemap = true
    applyMapTheme(map, options)
    void timeline.load()
  })
  wirePart('setup', () => setup.wire())
  void setup.maybeShow()

  Object.assign(handle, {
    sheets,
    timeline,
    places,
    sources,
    trails,
    imports,
    draw,
    brush,
    labels,
    backup,
    setup,
  })
}

void start()
