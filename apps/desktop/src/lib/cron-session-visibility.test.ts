import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import {
  cronJobShownInSessions,
  cronJobVisibilityKey,
  mergeSessionsForPresentation,
  sessionsFeedShowsLoadMore,
  visibleCronSessions
} from './cron-session-visibility'

const row = (id: string, over: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    archived: false,
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: 'cron',
    started_at: 0,
    title: id,
    tool_call_count: 0,
    ...over
  }) as SessionInfo

describe('cron session visibility', () => {
  it('defaults to visible, filters archived rows, and restores a hidden job from the same cache', () => {
    const rows = [row('cron_daily_1'), row('cron_weekly_1'), row('cron_daily_old', { archived: true })]
    const hidden = [cronJobVisibilityKey('daily')]

    expect(visibleCronSessions(rows, []).map(session => session.id)).toEqual(['cron_daily_1', 'cron_weekly_1'])
    expect(visibleCronSessions(rows, hidden).map(session => session.id)).toEqual(['cron_weekly_1'])
    expect(visibleCronSessions(rows, []).map(session => session.id)).toContain('cron_daily_1')
  })

  it('profile-qualifies identical job ids and normalizes a missing profile to default', () => {
    const rows = [row('cron_daily_default', { profile: undefined }), row('cron_daily_work', { profile: 'work' })]
    const hidden = [cronJobVisibilityKey('daily', 'work')]

    expect(cronJobVisibilityKey('daily')).toBe(JSON.stringify(['default', 'daily']))
    expect(visibleCronSessions(rows, hidden).map(session => session.id)).toEqual(['cron_daily_default'])
    expect(cronJobShownInSessions(hidden, 'daily', 'default')).toBe(true)
    expect(cronJobShownInSessions(hidden, 'daily', 'work')).toBe(false)
  })

  it('ignores malformed keys and observes the exact cron job prefix boundary', () => {
    const rows = [row('cron_daily_1'), row('cron_daily'), row('prefix_cron_daily_2'), row('cron_dailylong_3')]
    const malformed = ['not-json', JSON.stringify(['default']), JSON.stringify(['default', 7])]

    expect(visibleCronSessions(rows, malformed)).toEqual(rows)
    expect(visibleCronSessions(rows, [cronJobVisibilityKey('daily')]).map(session => session.id)).toEqual([
      'cron_daily',
      'prefix_cron_daily_2',
      'cron_dailylong_3'
    ])
  })
})

describe('Sessions presentation merge', () => {
  it('deduplicates by normalized profile and durable lineage, then sorts by started_at', () => {
    const ordinary = [
      row('ordinary', { profile: undefined, source: 'desktop', started_at: 10 }),
      row('tip', { _lineage_root_id: 'root', profile: 'work', source: 'desktop', started_at: 20 })
    ]

    const cron = [
      row('cron_same_lineage', { _lineage_root_id: 'root', profile: 'work', started_at: 30 }),
      row('cron_other_profile', { _lineage_root_id: 'root', profile: 'default', started_at: 40 })
    ]

    expect(mergeSessionsForPresentation(ordinary, cron).map(session => session.id)).toEqual([
      'cron_other_profile',
      'tip',
      'ordinary'
    ])
  })

  it('preserves the shared cron Load more affordance in All Profiles when truncated', () => {
    expect(
      sessionsFeedShowsLoadMore({
        agentsGrouped: false,
        cronTruncated: true,
        ordinaryHasMore: false,
        sessionsLoading: false,
        showAllProfiles: true
      })
    ).toBe(true)
    expect(
      sessionsFeedShowsLoadMore({
        agentsGrouped: false,
        cronTruncated: false,
        ordinaryHasMore: true,
        sessionsLoading: false,
        showAllProfiles: true
      })
    ).toBe(false)
  })
})
