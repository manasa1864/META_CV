/** The validated chart palette. The Prism UI is light-only, so charts always use the light set:
 * following the OS dark setting drew dark gridlines and tooltips on the white cards. */
const LIGHT = {
  s1: '#2a78d6', s2: '#eb6834', s3: '#1baf7a', muted: '#898781',
  grid: '#e1e0d9', axis: '#c3c2b7', ink: '#52514e', surface: '#ffffff',
}

export function usePalette() {
  return LIGHT
}
