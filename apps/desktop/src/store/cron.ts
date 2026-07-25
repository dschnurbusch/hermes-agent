import { atom } from 'nanostores'

import { cronJobVisibilityKey } from '@/lib/cron-session-visibility'
import { Codecs, persistentAtom } from '@/lib/persisted'
import type { CronJob } from '@/types/hermes'

const HIDDEN_CRON_JOBS_STORAGE_KEY = 'hermes.desktop.cronJobsHiddenFromSessions.v1'

// Cron *jobs* (not run sessions) power the sidebar "Cron jobs" section. Listing
// the job — schedule, state, live next-run countdown — makes the job the
// first-class entity; its runs (sessions) resolve under it in the cron detail.
export const $cronJobs = atom<CronJob[]>([])

export interface CronJobsRequest {
  generation: number
  scope: string
}

export interface CronJobsScopeToken {
  generation: number
  scope: string
}

let cronJobsRequestGeneration = 0
let cronJobsRequestScope = ''
let cronJobsScopeGeneration = 0

function activateCronJobsScope(scope: string): void {
  if (scope === cronJobsRequestScope) {
    return
  }

  cronJobsRequestScope = scope
  cronJobsRequestGeneration += 1
  cronJobsScopeGeneration += 1
}

export function beginCronJobsRequest(scope: string): CronJobsRequest {
  activateCronJobsScope(scope)
  cronJobsRequestGeneration += 1

  return { generation: cronJobsRequestGeneration, scope }
}

export function beginCronJobsAction(scope: string): CronJobsScopeToken {
  activateCronJobsScope(scope)

  return { generation: cronJobsScopeGeneration, scope }
}

export function isCronJobsScopeCurrent(token: CronJobsScopeToken): boolean {
  return token.scope === cronJobsRequestScope && token.generation === cronJobsScopeGeneration
}

export function isCronJobsRequestCurrent(request: CronJobsRequest): boolean {
  return request.scope === cronJobsRequestScope && request.generation === cronJobsRequestGeneration
}

export function invalidateCronJobsRequests(): void {
  cronJobsRequestGeneration += 1
  cronJobsScopeGeneration += 1
}

export function commitCronJobsRequest(request: CronJobsRequest, jobs: CronJob[]): boolean {
  if (!isCronJobsRequestCurrent(request)) {
    return false
  }

  // Consume the token so neither a duplicate completion nor any older request
  // can publish after this authoritative snapshot.
  cronJobsRequestGeneration += 1
  $cronJobs.set(jobs)

  return true
}

export const setCronJobs = (jobs: CronJob[]) => {
  cronJobsRequestGeneration += 1
  $cronJobs.set(jobs)
}

// In-place edit so the cron overlay's mutations (create/edit/delete/pause/…)
// land in the same atom the sidebar renders — no stale list until the next poll.
export const updateCronJobs = (fn: (jobs: CronJob[]) => CronJob[]) => {
  cronJobsRequestGeneration += 1
  $cronJobs.set(fn($cronJobs.get()))
}

// Presentation opt-outs: absence means every cron job is visible in Sessions.
// Each key is JSON [normalizedProfile, jobId], so profiles may reuse job ids.
export const $cronJobsHiddenFromSessions = persistentAtom<string[]>(
  HIDDEN_CRON_JOBS_STORAGE_KEY,
  [],
  Codecs.stringArray
)

export function setCronJobInSessions(jobId: string, profile: null | string | undefined, shown: boolean): void {
  const key = cronJobVisibilityKey(jobId, profile)
  const current = $cronJobsHiddenFromSessions.get()
  const hidden = current.includes(key)

  if (shown === !hidden) {
    return
  }

  $cronJobsHiddenFromSessions.set(shown ? current.filter(item => item !== key) : [...current, key])
}

// One-shot focus target: clicking "Manage" on a job sets this, then opens the
// cron overlay, which reads it once to select + scroll to that job. Cleared
// after consumption so re-opening cron normally doesn't re-focus a stale job.
export const $cronFocusJobId = atom<null | string>(null)
export const setCronFocusJobId = (id: null | string) => $cronFocusJobId.set(id)

// Shell-owned one-shot intent for stores without router context. Do not set a
// focus id here: the cron overlay's first fetch may not have loaded that row.
export const $cronReviewRequest = atom(0)
export const requestCronReview = () => $cronReviewRequest.set($cronReviewRequest.get() + 1)

export interface CronJobIdentity {
  id: string
  profile?: null | string
}

export const $cronFocusJob = atom<CronJobIdentity | null>(null)
export const setCronFocusJob = (job: CronJobIdentity | null) => $cronFocusJob.set(job)
