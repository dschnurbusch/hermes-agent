import { describe, expect, it } from 'vitest'

import { contrastRatio } from './color'
import { getBaseColors, unreadContrastColor } from './context'
import { BUILTIN_THEME_LIST } from './presets'

function sidebarBackground(colors: ReturnType<typeof getBaseColors>): string {
  return colors.sidebarBackground ?? colors.background
}

describe('unreadContrastColor', () => {
  it('keeps the preferred sky accent when it contrasts with the active theme', () => {
    const colors = getBaseColors('sunset', 'dark')

    expect(unreadContrastColor(colors)).toBe('#38bdf8')
  })

  it.each(BUILTIN_THEME_LIST.flatMap(theme => [
    [`${theme.name} light`, getBaseColors(theme.name, 'light')] as const,
    [`${theme.name} dark`, getBaseColors(theme.name, 'dark')] as const
  ]))('chooses a visible contrast color for %s', (_label, colors) => {
    const unread = unreadContrastColor(colors)

    expect(contrastRatio(unread, sidebarBackground(colors))).toBeGreaterThanOrEqual(3)
  })
})
