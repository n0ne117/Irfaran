// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pins and popups that know the world is round.
//
// MapLibre already works out that a location is on the far side of the globe.
// It just does not hide it: a covered marker is drawn at 20% opacity, which on
// a globe is a column of ghost pins hanging over the Pacific where Austria is
// on the other side of the planet. A popup is not faded at all unless asked.
//
// Not offered as a setting. "Show me things that are behind the earth" is not
// a preference anybody holds - it is what a flat map does because it has no
// way to know, and the globe does know.
//
// Everything Irfaran puts on the map goes through here, so that the answer is
// in one place rather than at five call sites that will not all be remembered
// next time.

import { Marker, Popup } from 'maplibre-gl'
import type { MarkerOptions, PopupOptions } from 'maplibre-gl'

/** Gone, rather than MapLibre's default ghost at 0.2. */
const COVERED = 0

export function mapMarker(options: MarkerOptions = {}): Marker {
  return new Marker({ opacityWhenCovered: COVERED, ...options })
}

export function mapPopup(options: PopupOptions = {}): Popup {
  return new Popup({ locationOccludedOpacity: COVERED, ...options })
}
