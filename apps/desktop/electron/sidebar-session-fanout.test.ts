import { describe, expect, it, vi } from 'vitest'

import { fanOutSidebarSessions } from './sidebar-session-fanout'

describe('remote sidebar fan-out', () => {
  it('honors a concrete cron selector and advertises the capability', async () => {
    const calls: URLSearchParams[] = []

    const fetchSlice = vi.fn(async (params: URLSearchParams) => {
      calls.push(new URLSearchParams(params))

      return { sessions: [{ id: params.get('source') === 'cron' ? 'cron' : 'other' }], total: 1 }
    })

    const result = await fanOutSidebarSessions(
      new URLSearchParams({
        recents_profile: 'work',
        cron_profile: 'work',
        cron_limit: '500'
      }),
      ['work'],
      fetchSlice
    )

    expect(calls).toHaveLength(3)
    expect(calls[0].get('profile')).toBe('work')
    expect(calls[1].get('profile')).toBe('work')
    expect(calls[1].get('source')).toBe('cron')
    expect(calls[1].get('limit')).toBe('500')
    expect(calls[2].get('profile')).toBe('all')
    expect(result.capabilities).toEqual({ cron_profile: true })
  })

  it('preserves All Profiles cron acquisition', async () => {
    const calls: URLSearchParams[] = []

    await fanOutSidebarSessions(new URLSearchParams({ cron_profile: 'all' }), ['work'], async params => {
      calls.push(new URLSearchParams(params))

      return { sessions: [] }
    })

    expect(calls[1].get('profile')).toBe('all')
  })
})
