// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the archive adds up to.
//
// Deliberately not polled. Measuring the cleared ground reads every fog blob
// in the archive - under a second today and more later - so the server caches
// it against a fingerprint and this asks once, when the tab is opened.

import { apiGet } from './api'
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

interface Overview {
  ground: Ground
  routes: Routes
  points: number
  marks: Marks
  time: Time
  cached: boolean
}

const count = (value: number) => value.toLocaleString()

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

  /** Told which tab is showing; fetches once, the first time it is this one. */
  watch(showing: boolean): void {
    if (!showing || this.asked) return
    this.asked = true
    void this.load()
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

  private paint(figures: Overview): void {
    const { ground, routes, marks, time } = figures

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
