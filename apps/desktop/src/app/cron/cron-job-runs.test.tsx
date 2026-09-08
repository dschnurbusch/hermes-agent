// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { getCronJobRuns } from '@/hermes'
import type { Translations } from '@/i18n/types'
import { makeSessionInfo } from '@/test/session-info'

import { CronJobRuns } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getCronJobRuns: vi.fn()
}))
afterEach(cleanup)

it('Master Cron opens the fetched row with its connection and profile owner', async () => {
  const row = makeSessionInfo({
    id: 'cron_daily_1',
    title: 'Remote run',
    profile: 'work',
    connection_id: 'remote-1',
    source: 'cron'
  })

  vi.mocked(getCronJobRuns).mockResolvedValue([row])
  const open = vi.fn()
  render(
    <CronJobRuns
      c={{ runHistory: 'History', noRuns: 'No runs' } as Translations['cron']}
      jobId="daily"
      onOpenSession={open}
      profile="work"
    />
  )
  fireEvent.click(await screen.findByText('Remote run'))
  expect(getCronJobRuns).toHaveBeenCalledWith('daily', 20, 'work')
  expect(open).toHaveBeenCalledWith(row.id, row)
})
