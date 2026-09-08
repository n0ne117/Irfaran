// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the archive adds up to.
//
// Deliberately not polled. Measuring the cleared ground reads every fog blob
// in the archive - under a second today and more later - so the server caches
// it against a fingerprint and this asks once, when the tab is opened.

import { apiGet } from './api'
import { shownUiTheme } from './theme'
import { element } from './ui'

interface Ground {
  square_km: number
  tiles: number
  percent_of_planet: number
}

interface Routes {
  total: number
  workouts: number
  live: number
  drawn: number
  named_journeys: number
  by_source: Record<string, number>
}

interface Marks {
  pins: number
  labels: number
  folders: number
  people: number
  revealed: number
  refogged: number
}

interface Time {
  years: string[]
  count: number
  first: string | null
  last: string | null
  undated: boolean
}

interface Visited {
  code: string
  name: string
  square_km: number
  square_metres: number
  of_country_percent: number
  country_square_km: number
  pins: number
  because: string
}

interface Countries {
  available: boolean
  why?: string
  countries: Visited[]
  marginal: Visited[]
  at_sea_square_km: number
}

interface Overview {
  ground: Ground
  countries: Countries
  routes: Routes
  points: number
  marks: Marks
  time: Time
  cached: boolean
}

const count = (value: number) => value.toLocaleString()

/**
 * How wide the world is drawn, in pixels.
 *
 * It runs the full width of the sheet rather than half of it, so 960 was being
 * stretched. 1440 covers the widest the sheet gets, and costs 23 KB against 12
 * - still a thumbnail beside the 2.26 MB of polygons it is made from.
 */
const WORLD_WIDTH = 1440

/** Square kilometres, to a sensible number of digits for how big it is. */
export function formatArea(squareKm: number): string {
  if (squareKm >= 10_000) return `${Math.round(squareKm).toLocaleString()} km²`
  if (squareKm >= 100) return `${squareKm.toFixed(1)} km²`
  if (squareKm >= 1) return `${squareKm.toFixed(2)} km²`
  return `${Math.round(squareKm * 1e6).toLocaleString()} m²`
}

/**
 * The share of the planet, said twice.
 *
 * As a percentage it is five leading zeros and means nothing to anybody. As
 * "one part in two million" it means something immediately, which is the point
 * of showing it at all: not to flatter the number but to say how big the world
 * is.
 */
export function formatShare(percent: number): string {
  if (percent <= 0) return 'none of it yet'
  const digits = percent < 0.001 ? 7 : percent < 1 ? 4 : 2
  const share = `${percent.toFixed(digits)}%`
  const oneIn = Math.round(100 / percent)
  return `${share} — about one part in ${count(oneIn)}`
}

export class Stats {
  private asked = false

  /** Told when the page is showing; fetches once, the first time. */
  watch(showing: boolean): void {
    if (!showing) return
    // Every time it is opened, because the theme can have changed since the
    // figures were fetched and the world is drawn in one of two palettes.
    // Costs nothing when it has not: same URL, and the browser has it.
    this.paintWorld()
    if (this.asked) return
    this.asked = true
    void this.load()
  }

  /**
   * The little world at the bottom.
   *
   * An image the server draws rather than shapes sent here: the polygons are
   * 548,471 vertices and this is eight kilobytes. The URL carries the theme
   * because the picture is drawn in it, and the countries because that is what
   * makes it a different picture - so the browser caches each answer and asks
   * again only when the answer has moved.
   */
  private paintWorld(figures?: Overview): void {
    const world = element<HTMLImageElement>('stat-world')
    const theme = shownUiTheme()
    const next = `/api/stats/world.png?theme=${theme}&width=${WORLD_WIDTH}`
    if (!world.src.endsWith(next)) world.src = next

    if (!figures) return
    const seen = figures.countries.countries.length
    world.alt = figures.countries.available
      ? `A world map with ${seen} ${seen === 1 ? 'country' : 'countries'} ` +
        'marked as visited.'
      : 'A world map. The country borders are not available.'
  }

  wire(): void {
    element('stats-refresh').addEventListener('click', () => void this.load(true))
  }

  async load(refresh = false): Promise<void> {
    const button = element<HTMLButtonElement>('stats-refresh')
    const state = element('stats-state')
    button.disabled = true
    state.textContent = refresh ? 'Counting…' : 'Reading…'
    state.hidden = false

    try {
      const figures = await apiGet<Overview>(
        refresh ? '/api/stats?refresh=true' : '/api/stats',
      )
      this.paint(figures)
      state.hidden = true
    } catch {
      state.textContent =
        'Could not read the figures. The browser console has the detail.'
      state.hidden = false
    } finally {
      button.disabled = false
    }
  }

  /** A share of a country, at whatever precision makes it a number. */
  private static share(percent: number): string {
    if (percent >= 1) return `${percent.toFixed(1)}%`
    if (percent >= 0.01) return `${percent.toFixed(2)}%`
    return `${percent.toFixed(4)}%`
  }

  private paintCountries(visited: Countries): void {
    const host = element('stat-country-list')
    host.textContent = ''

    if (!visited.available) {
      element('stat-countries').textContent = '—'
      element('stat-at-sea').textContent =
        visited.why ?? 'The country borders are not available.'
      return
    }

    element('stat-countries').textContent = String(visited.countries.length)

    for (const row of visited.countries) {
      const line = document.createElement('div')
      line.className = 'country-row'

      const name = document.createElement('span')
      name.textContent = row.name

      const area = document.createElement('span')
      area.className = 'country-area'
      area.textContent = formatArea(row.square_km)

      const share = document.createElement('span')
      share.className = 'country-share'
      share.textContent = Stats.share(row.of_country_percent)
      share.title =
        `${formatArea(row.square_km)} of ${formatArea(row.country_square_km)}` +
        (row.pins ? ` · ${row.pins} pin${row.pins === 1 ? '' : 's'}` : '')

      line.append(name, area, share)
      host.append(line)
    }

    const marginal = element('stat-country-marginal')
    marginal.hidden = visited.marginal.length === 0
    marginal.textContent = visited.marginal.length
      ? 'Too little to call a visit, and shown rather than dropped — this is ' +
        'where an approximate border would turn up: ' +
        visited.marginal.map((row) => row.name).join(', ')
      : ''

    element('stat-at-sea').textContent =
      `${formatArea(visited.at_sea_square_km)} in no country at all — at sea, ` +
      'or past where the borders reach'
  }

  private paint(figures: Overview): void {
    const { ground, routes, marks, time } = figures
    this.paintCountries(figures.countries)
    this.paintWorld(figures)

    element('stat-area').textContent = formatArea(ground.square_km)
    element('stat-share').textContent = formatShare(ground.percent_of_planet)
    element('stat-tiles').textContent =
      `${count(ground.tiles)} squares of the map have been touched`

    element('stat-routes').textContent = count(routes.total)
    element('stat-routes-workouts').textContent = count(routes.workouts)
    element('stat-routes-drawn').textContent = count(routes.drawn)
    element('stat-routes-live').textContent = count(routes.live)
    element('stat-journeys').textContent = count(routes.named_journeys)

    element('stat-points').textContent = count(figures.points)

    element('stat-pins').textContent = count(marks.pins)
    element('stat-labels').textContent = count(marks.labels)
    element('stat-folders').textContent = count(marks.folders)
    element('stat-people').textContent = count(marks.people)
    element('stat-revealed').textContent = count(marks.revealed)
    element('stat-refogged').textContent = count(marks.refogged)

    element('stat-years').textContent = time.count
      ? `${time.first} to ${time.last}`
      : 'nothing dated yet'
    element('stat-years-count').textContent = time.count
      ? `${count(time.count)} ${time.count === 1 ? 'year' : 'years'}` +
        (time.undated ? ', and everything before records' : '')
      : time.undated
        ? 'everything is undated'
        : ''
  }
}
