/** The validated chart palette (light and dark), following the colour scheme. */
import { useEffect, useState } from 'react'

const LIGHT = {
  s1: '#2a78d6', s2: '#eb6834', s3: '#1baf7a', muted: '#898781',
  grid: '#e1e0d9', axis: '#c3c2b7', ink: '#52514e', surface: '#ffffff',
}
const DARK = {
  s1: '#3987e5', s2: '#d95926', s3: '#199e70', muted: '#898781',
  grid: '#2c2c2a', axis: '#383835', ink: '#c3c2b7', surface: '#1a1d1f',
}

export function usePalette() {
  const query = '(prefers-color-scheme: dark)'
  const [dark, setDark] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = (e: MediaQueryListEvent) => setDark(e.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return dark ? DARK : LIGHT
}
