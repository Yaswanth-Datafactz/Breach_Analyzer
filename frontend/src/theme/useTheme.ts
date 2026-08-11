import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'dark' | 'light'

const STORAGE_KEY = 'breach-analytics-theme'

function getInitialTheme(): ThemeMode {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  // Dark is the DataFactZ default (Handbook §7); light is opt-in.
  return stored === 'light' ? 'light' : 'dark'
}

export function useTheme() {
  const [theme, setTheme] = useState<ThemeMode>(getInitialTheme)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    window.localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme((current) => (current === 'dark' ? 'light' : 'dark'))
  }, [])

  return { theme, toggleTheme }
}
