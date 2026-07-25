import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $cronJobsHiddenFromSessions, setCronJobInSessions } from '@/store/cron'
import type { CronJob } from '@/types/hermes'

import { SidebarCronJobsSection } from './cron-jobs-section'

const getCronJobRuns = vi.hoisted(() => vi.fn())

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobRuns
}))

const job: CronJob = {
  enabled: true,
  id: 'daily',
  name: 'Daily review',
  profile: 'work'
}

describe('Sidebar Cron Jobs Sessions action', () => {
  beforeEach(() => {
    $cronJobsHiddenFromSessions.set([])
    getCronJobRuns.mockReset()
    getCronJobRuns.mockResolvedValue([])
  })

  afterEach(() => {
    cleanup()
    $cronJobsHiddenFromSessions.set([])
  })

  it('labels the eye by its next action and forwards the owning profile', () => {
    const onSetSessionsVisibility = vi.fn((jobId: string, profile: null | string | undefined, shown: boolean) =>
      setCronJobInSessions(jobId, profile, shown)
    )

    render(
      <SidebarCronJobsSection
        jobs={[job]}
        label="Cron Jobs"
        onManageJob={vi.fn()}
        onOpenRun={vi.fn()}
        onSetSessionsVisibility={onSetSessionsVisibility}
        onToggle={vi.fn()}
        onTriggerJob={vi.fn()}
        open
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Hide from Sessions' }))

    expect(screen.getByRole('button', { name: 'Show in Sessions' })).toBeTruthy()
    expect(onSetSessionsVisibility).toHaveBeenCalledWith('daily', 'work', false)
  })

  it('fetches and opens run history with the owning profile', async () => {
    const onOpenRun = vi.fn()

    getCronJobRuns.mockResolvedValue([
      {
        id: 'cron_daily_1',
        last_active: 0,
        profile: 'work',
        source: 'cron',
        started_at: 0
      }
    ])

    render(
      <SidebarCronJobsSection
        jobs={[job]}
        label="Cron Jobs"
        onManageJob={vi.fn()}
        onOpenRun={onOpenRun}
        onSetSessionsVisibility={vi.fn()}
        onToggle={vi.fn()}
        onTriggerJob={vi.fn()}
        open
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Show runs' }))
    fireEvent.click(await screen.findByRole('button', { name: '—' }))

    expect(getCronJobRuns).toHaveBeenCalledWith('daily', 5, 'work')
    expect(onOpenRun).toHaveBeenCalledWith('cron_daily_1', 'work')
  })
})
