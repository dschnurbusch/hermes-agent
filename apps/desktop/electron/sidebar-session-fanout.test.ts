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
    expect(calls[2].get('profile')).toBe('work')
    expect(result.capabilities).toEqual({ cron_profile: true })
  })

  it('retains errors only on the failed remote slice', async () => {
    const errors = [{ profile: 'work', error: 'offline' }]

    const result = await fanOutSidebarSessions(new URLSearchParams(), ['work'], async params =>
      params.get('source') === 'cron' ? { sessions: [], errors } : { sessions: [{ id: 'ok' }] }
    )

    expect(result.cron.errors).toEqual(errors)
    expect(result.recents.errors).toBeUndefined()
    expect(result.messaging.errors).toBeUndefined()
    expect(result.errors).toEqual([])
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
