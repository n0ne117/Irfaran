// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Handing a pin's coordinates to somebody else's map.
//
// Irfaran knows where you were and nothing about where you are going. Routing,
// opening hours, street view, what a place is called this year - all of that
// lives in the big map services, and the cheapest possible bridge to them is a
// pair of coordinates in a URL.
//
// Coordinates and nothing else. Not the pin's name, not its label, not who was
// there: those are the parts worth keeping private, and a query string is the
// least private place in computing. The coordinates alone land you close enough
// to see what you came for.

/** Which service a pin opens in. */
export type MapsProvider = 'osm' | 'google' | 'apple'

export const PROVIDERS: MapsProvider[] = ['osm', 'google', 'apple']

const PROVIDER_KEY = 'irfaran.maps.provider'

/**
 * How close the other map should land.
 *
 * Seventeen is a street: close enough to see which building, wide enough to
 * recognise the surroundings. Every one of the three understands it.
 */
const ZOOM = 17

export const PROVIDER_NAMES: Record<MapsProvider, string> = {
  osm: 'OpenStreetMap',
  google: 'Google Maps',
  apple: 'Apple Maps',
}

/**
 * The chosen service, from this browser.
 *
 * A browser preference rather than a server setting, and deliberately: the
 * right answer differs per device. Apple Maps is the obvious choice on an
 * iPhone and the wrong one on the Linux box next to it, and a single
 * archive-wide setting could only ever be right on one of them.
 */
export function getMapsProvider(): MapsProvider {
  try {
    const stored = window.localStorage.getItem(PROVIDER_KEY)
    if (stored && (PROVIDERS as string[]).includes(stored)) {
      return stored as MapsProvider
    }
  } catch {
    /* storage disabled */
  }
  // OpenStreetMap first: it is where the basemap comes from, and it is the one
  // that asks least of whoever clicks.
  return 'osm'
}

export function setMapsProvider(provider: MapsProvider): void {
  try {
    window.localStorage.setItem(PROVIDER_KEY, provider)
  } catch {
    /* a preference that cannot be stored is still worth applying now */
  }
}

/**
 * Where to send a pair of coordinates.
 *
 * Six decimals, which is about a tenth of a metre - more than any of these
 * services will do anything with, and less than writing out a float's full
 * repr into a URL.
 */
export function externalUrl(
  provider: MapsProvider,
  lat: number,
  lon: number,
): string {
  const at = `${lat.toFixed(6)},${lon.toFixed(6)}`

  if (provider === 'google') {
    // The documented form rather than a scraped one: `api=1` is Google's
    // promise that the parameters keep working.
    return `https://www.google.com/maps/search/?api=1&query=${at}`
  }
  if (provider === 'apple') {
    // maps.apple.com opens the app on an Apple device and a web map anywhere
    // else, so one link covers both.
    return `https://maps.apple.com/?ll=${at}&z=${ZOOM}`
  }
  // OpenStreetMap wants the marker and the camera separately: mlat/mlon drops
  // the pin, and the fragment decides where to look.
  return (
    `https://www.openstreetmap.org/?mlat=${lat.toFixed(6)}` +
    `&mlon=${lon.toFixed(6)}#map=${ZOOM}/${lat.toFixed(6)}/${lon.toFixed(6)}`
  )
}

/**
 * A link that opens a pin somewhere else.
 *
 * `noreferrer` is the point of building this here rather than inline. Without
 * it the other map service is told which page sent you, and for a self-hosted
 * instance that is a private hostname - the one thing on this page nobody else
 * needs to learn.
 */
export function externalLink(lat: number, lon: number): HTMLAnchorElement {
  const provider = getMapsProvider()
  const link = document.createElement('a')
  link.className = 'popup-open-in'
  link.href = externalUrl(provider, lat, lon)
  link.target = '_blank'
  link.rel = 'noreferrer noopener'
  link.textContent = `Open in ${PROVIDER_NAMES[provider]}`
  link.title = `Open these coordinates in ${PROVIDER_NAMES[provider]}`
  return link
}
