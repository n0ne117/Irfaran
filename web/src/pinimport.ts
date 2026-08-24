// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reviewing pins imported out of another application's database.
//
// Nothing in the interface offers this. It appears when a places database is
// dropped into the file picker under Import and not otherwise - there is no
// button, no tab and no hint. A one-off for somebody who read the release
// notes.
//
// Its own sidebar rather than the one the holding pen uses. A held batch is a
// set of fixes with a trim; a staged pin is a name, a person and a point, and
// the two panels have almost nothing in common but the word review.
//
// The map isolation is shared, though: the fog, the tracks and the existing
// pins all go away, so what is on screen is the pin being judged and the
// basemap under it. Three hundred and twenty-four decisions is a long sitting,
// so Enter and Delete both save-and-advance - the mouse is the slow way round.

import { Marker, Popup } from 'maplibre-gl'
import type { Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend, getToken } from './api'
import { element } from './ui'

/** After the last keystroke, before the edit is sent. */
const SETTLE_MS = 400

const STAGED_COLOUR = '#ffb454'
const CHOSEN_COLOUR = '#5ad18a'

export interface StagedPin {
  id: number
  source_id: number | null
  name: string
  lat: number
  lon: number
  category: string
  people: string[]
  label_id: number | null
  prominence: string
  state: 'waiting' | 'saved' | 'discarded'
  place_id: number | null
}

interface Staging {
  total: number
  waiting: number
  saved: number
  discarded: number
  finished: boolean
  items: StagedPin[]
}

export interface StageReport {
  read: number
  staged: number
  already_here: string[]
  collapsed: string[]
  unreadable: number
  unmatched: Record<string, number>
}

interface Label {
  id: number
  name: string
  colour: string
}

/** What the file said, summarised for whoever has to trust it. */
export function describeStaged(report: StageReport): string {
  const parts = [`${report.staged} of ${report.read} to review`]
  if (report.already_here.length) parts.push(`${report.already_here.length} already here`)
  if (report.collapsed.length) parts.push(`${report.collapsed.length} duplicate`)
  if (report.unreadable) parts.push(`${report.unreadable} unreadable`)
  const unmatched = Object.keys(report.unmatched)
  if (unmatched.length) {
    parts.push(`nobody matched ${unmatched.join(', ')}`)
  }
  return parts.join(', ')
}

export class PinImport {
  private readonly map: MapLibreMap
  private readonly onOpen: () => void
  private readonly onCommitted: () => void
  private readonly setRestVisible: (visible: boolean) => void

  private items: StagedPin[] = []
  private current: StagedPin | null = null
  private markers = new Map<number, Marker>()
  private people: string[] = []
  private labels: Label[] = []
  private settle: number | undefined
  private chain: Promise<void> = Promise.resolve()

  constructor(
    map: MapLibreMap,
    hooks: {
      onOpen: () => void
      onCommitted: () => void
      setRestVisible: (visible: boolean) => void
    },
  ) {
    this.map = map
    this.onOpen = hooks.onOpen
    this.onCommitted = hooks.onCommitted
    this.setRestVisible = hooks.setRestVisible
  }

  // ------------------------------------------------------------------ wiring

  wire(): void {
    element('pin-import-close').addEventListener('click', () => this.leave())
    element('pin-import-done').addEventListener('click', () => void this.finish())
    element('pin-import-save').addEventListener('click', () => void this.decide('save'))
    element('pin-import-discard').addEventListener('click', () =>
      void this.decide('discard'),
    )

    const name = element<HTMLInputElement>('pin-import-name')
    name.addEventListener('input', () => this.later())
    element<HTMLSelectElement>('pin-import-label').addEventListener('change', () =>
      void this.queue(),
    )
    element<HTMLSelectElement>('pin-import-prominence').addEventListener('change', () =>
      void this.queue(),
    )

    // Enter saves, Delete discards, both advance. Bound on the sidebar so it
    // only applies while this is what somebody is looking at, and skipped
    // inside a field - Enter in the name box must not commit the pin whose
    // name is half typed.
    element('pin-import-page').addEventListener('keydown', (event) => {
      const key = (event as KeyboardEvent).key
      const target = event.target as HTMLElement | null
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLSelectElement ||
        target instanceof HTMLTextAreaElement
      if (typing || !this.current) return
      if (key === 'Enter') {
        event.preventDefault()
        void this.decide('save')
      } else if (key === 'Delete' || key === 'Backspace') {
        event.preventDefault()
        void this.decide('discard')
      }
    })
  }

  /**
   * Reopen an import that is still open, on page load.
   *
   * Three hundred pins is a long sitting and a browser gets reloaded. Without
   * this the staged rows are still there on the server and there is no way
   * back to them, because nothing in the interface opens this sidebar - which
   * would make the one feature with no entry point also the one that cannot
   * be resumed.
   */
  async resume(): Promise<boolean> {
    if (!getToken()) return false
    try {
      const staging = await apiGet<Staging>('/api/import/pins')
      if (!staging.total) return false
    } catch {
      return false
    }
    await this.begin()
    return true
  }

  /** Called by the importer once a places database has been staged. */
  async begin(): Promise<void> {
    await Promise.all([this.loadPeople(), this.loadLabels()])
    await this.load()
    this.onOpen()
    this.enter()
    const first = this.items.find((item) => item.state === 'waiting')
    if (first) this.select(first.id)
  }

  private async loadPeople(): Promise<void> {
    try {
      const body = await apiGet<{ people: { name: string }[] }>('/api/people')
      this.people = body.people.map((person) => person.name)
    } catch {
      this.people = []
    }
  }

  private async loadLabels(): Promise<void> {
    try {
      const body = await apiGet<{ labels: Label[] }>('/api/labels')
      this.labels = body.labels
    } catch {
      this.labels = []
    }
    const select = element<HTMLSelectElement>('pin-import-label')
    select.textContent = ''
    const none = document.createElement('option')
    none.value = ''
    none.textContent = 'No label'
    select.append(none)
    for (const label of this.labels) {
      const option = document.createElement('option')
      option.value = String(label.id)
      option.textContent = label.name
      select.append(option)
    }
  }

  async load(): Promise<void> {
    const staging = await apiGet<Staging>('/api/import/pins')
    this.items = staging.items
    this.paintList(staging)
    this.paintMarkers()
  }

  // ------------------------------------------------------------------- panes

  private enter(): void {
    this.setRestVisible(false)
    element('pin-import-page').setAttribute('tabindex', '-1')
    element('pin-import-page').focus()
  }

  private leave(): void {
    // The staged rows stay where they are. Closing the sidebar is not a
    // decision about them, and coming back to three hundred pins mid-sitting
    // has to be possible.
    element('pin-import-page').hidden = true
    this.clearMarkers()
    this.current = null
    this.setRestVisible(true)
  }

  /** Called by the owner when the sidebar was closed some other way. */
  closed(): void {
    if (element('pin-import-page').hidden) {
      this.clearMarkers()
      this.current = null
      this.setRestVisible(true)
    }
  }

  // -------------------------------------------------------------------- list

  private paintList(staging: Staging): void {
    const list = element('pin-import-list')
    list.textContent = ''

    for (const item of this.items) {
      const row = document.createElement('button')
      row.type = 'button'
      row.className = `pin-row pin-row-${item.state}`
      row.dataset.id = String(item.id)

      const title = document.createElement('span')
      title.className = 'pin-row-name'
      title.textContent = item.name

      const detail = document.createElement('span')
      detail.className = 'pin-row-detail'
      detail.textContent = item.people.length
        ? item.people.join(', ')
        : item.category
          ? `${item.category} — nobody`
          : 'nobody'

      const mark = document.createElement('span')
      mark.className = 'pin-row-mark'
      mark.textContent =
        item.state === 'saved' ? '✓' : item.state === 'discarded' ? '—' : ''

      row.append(mark, title, detail)
      row.addEventListener('click', () => this.select(item.id))
      list.append(row)
    }

    const reviewed = staging.saved + staging.discarded
    element('pin-import-count').textContent =
      `${reviewed} of ${staging.total} reviewed — ${staging.saved} kept, ` +
      `${staging.discarded} discarded`

    const done = element<HTMLButtonElement>('pin-import-done')
    done.disabled = !staging.finished
    done.title = staging.finished
      ? 'Close this import and forget what it held'
      : `${staging.waiting} still waiting`

    if (this.current) this.mark(this.current.id)
  }

  private mark(id: number): void {
    for (const row of document.querySelectorAll<HTMLElement>('#pin-import-list .pin-row')) {
      row.classList.toggle('pin-row-current', row.dataset.id === String(id))
    }
  }

  // ------------------------------------------------------------------ markers

  private clearMarkers(): void {
    for (const marker of this.markers.values()) marker.remove()
    this.markers.clear()
  }

  private paintMarkers(): void {
    this.clearMarkers()
    for (const item of this.items) {
      if (item.state === 'discarded') continue
      const marker = new Marker({
        color: item.state === 'saved' ? CHOSEN_COLOUR : STAGED_COLOUR,
      })
        .setLngLat([item.lon, item.lat])
        .setPopup(new Popup({ offset: 26 }).setText(item.name))
        .addTo(this.map)
      marker.getElement().classList.add('staged-pin')
      marker.getElement().addEventListener('click', () => this.select(item.id))
      this.markers.set(item.id, marker)
    }
  }

  // ------------------------------------------------------------------ editing

  private select(id: number): void {
    const item = this.items.find((candidate) => candidate.id === id)
    if (!item) return
    this.current = item
    this.mark(id)

    element('pin-import-editor').hidden = false
    element('pin-import-decided').hidden = item.state === 'waiting'
    element('pin-import-decided').textContent =
      item.state === 'saved'
        ? 'Already in the archive. Delete it from Places if that was wrong.'
        : 'Discarded. Nothing here can put it back.'

    element<HTMLInputElement>('pin-import-name').value = item.name
    element('pin-import-where').textContent =
      `${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}`
    element('pin-import-category').textContent = item.category || '—'
    element<HTMLSelectElement>('pin-import-label').value =
      item.label_id === null ? '' : String(item.label_id)
    element<HTMLSelectElement>('pin-import-prominence').value = item.prominence

    this.paintPeople(item)

    const waiting = item.state === 'waiting'
    element<HTMLButtonElement>('pin-import-save').disabled = !waiting
    element<HTMLButtonElement>('pin-import-discard').disabled = !waiting
    for (const id_ of ['pin-import-name', 'pin-import-label', 'pin-import-prominence']) {
      element<HTMLInputElement>(id_).disabled = !waiting
    }

    this.map.flyTo({ center: [item.lon, item.lat], zoom: 13, duration: 500 })
  }

  private paintPeople(item: StagedPin): void {
    const host = element('pin-import-people')
    host.textContent = ''
    for (const person of this.people) {
      const label = document.createElement('label')
      label.className = 'check'
      const box = document.createElement('input')
      box.type = 'checkbox'
      box.value = person
      box.checked = item.people.includes(person)
      box.disabled = item.state !== 'waiting'
      box.addEventListener('change', () => void this.queue())
      const text = document.createElement('span')
      text.textContent = person
      label.append(box, text)
      host.append(label)
    }
    if (!this.people.length) {
      const hint = document.createElement('p')
      hint.className = 'hint'
      hint.textContent = 'No people are set up. Settings, Places.'
      host.append(hint)
    }
  }

  private get chosenPeople(): string[] {
    return Array.from(
      document.querySelectorAll<HTMLInputElement>('#pin-import-people input:checked'),
    ).map((box) => box.value)
  }

  private later(): void {
    if (this.settle !== undefined) window.clearTimeout(this.settle)
    this.settle = window.setTimeout(() => void this.queue(), SETTLE_MS)
  }

  /**
   * One save at a time, always carrying the whole pin.
   *
   * Same reason as the holding pen: the server merges a patch into what it
   * holds, so two requests in flight together both read the same starting
   * point and the second undoes the first. Sending everything and chaining
   * the sends makes ordering irrelevant.
   */
  private queue(): Promise<void> {
    this.chain = this.chain.then(() => this.saveEdits()).catch(() => {})
    return this.chain
  }

  private async flush(): Promise<void> {
    if (this.settle !== undefined) {
      window.clearTimeout(this.settle)
      this.settle = undefined
    }
    await this.queue()
  }

  private async saveEdits(): Promise<void> {
    const item = this.current
    if (!item || item.state !== 'waiting') return

    const label = element<HTMLSelectElement>('pin-import-label').value
    try {
      const updated = await apiSend<StagedPin>('PATCH', `/api/import/pins/${item.id}`, {
        name: element<HTMLInputElement>('pin-import-name').value,
        people: this.chosenPeople,
        label_id: label ? Number(label) : null,
        prominence: element<HTMLSelectElement>('pin-import-prominence').value,
      })
      this.replace(updated)
      element('pin-import-message').hidden = true
    } catch (error) {
      this.say(error)
    }
  }

  private replace(updated: StagedPin): void {
    const index = this.items.findIndex((item) => item.id === updated.id)
    if (index >= 0) this.items[index] = updated
    if (this.current?.id === updated.id) this.current = updated
  }

  // ----------------------------------------------------------------- deciding

  private async decide(what: 'save' | 'discard'): Promise<void> {
    const item = this.current
    if (!item || item.state !== 'waiting') return

    // Whatever was typed a moment ago has to be in before the pin is.
    if (what === 'save') await this.flush()

    const buttons = ['pin-import-save', 'pin-import-discard'] as const
    for (const id of buttons) element<HTMLButtonElement>(id).disabled = true

    try {
      await apiSend('POST', `/api/import/pins/${item.id}/${what}`)
      const following = this.next(item.id)
      await this.load()
      if (what === 'save') this.onCommitted()
      if (following !== null) this.select(following)
      else this.done()
    } catch (error) {
      this.say(error)
      for (const id of buttons) element<HTMLButtonElement>(id).disabled = false
    }
  }

  /** The next pin still waiting after this one, wrapping to the start. */
  private next(after: number): number | null {
    const order = this.items.map((item) => item.id)
    const from = order.indexOf(after)
    for (let step = 1; step <= order.length; step += 1) {
      const candidate = this.items[(from + step) % order.length]
      if (candidate && candidate.state === 'waiting' && candidate.id !== after) {
        return candidate.id
      }
    }
    return null
  }

  private done(): void {
    this.current = null
    element('pin-import-editor').hidden = true
    element('pin-import-message').textContent =
      'Every pin has been dealt with. Done closes the import.'
    element('pin-import-message').hidden = false
  }

  private async finish(): Promise<void> {
    try {
      const result = await apiSend<{ saved: number; discarded: number }>(
        'POST',
        '/api/import/pins/done',
      )
      this.items = []
      this.leave()
      this.onCommitted()
      window.alert(
        `Import finished. ${result.saved} pins kept, ${result.discarded} discarded.`,
      )
    } catch (error) {
      this.say(error)
    }
  }

  private say(error: unknown): void {
    const line = element('pin-import-message')
    line.textContent =
      error instanceof ApiError
        ? error.message
        : 'That did not work. The browser console has the detail.'
    line.hidden = false
    if (!(error instanceof ApiError)) console.error('Irfaran pin import', error)
  }
}
