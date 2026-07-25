import type { SessionInfo } from '@/types/hermes'

export type CronJobVisibilityIdentity = [profile: string, jobId: string]

function normalizeVisibilityProfile(profile?: null | string): string {
  return profile?.trim() || 'default'
}

export function cronJobVisibilityKey(jobId: string, profile?: null | string): string {
  return JSON.stringify([normalizeVisibilityProfile(profile), jobId])
}

export function parseCronJobVisibilityKey(key: string): CronJobVisibilityIdentity | null {
  try {
    const parsed: unknown = JSON.parse(key)

    if (
      Array.isArray(parsed) &&
      parsed.length === 2 &&
      typeof parsed[0] === 'string' &&
      typeof parsed[1] === 'string' &&
      parsed[1].length > 0
    ) {
      return [normalizeVisibilityProfile(parsed[0]), parsed[1]]
    }
  } catch {
    // Presentation preferences are best-effort; corrupt entries hide nothing.
  }

  return null
}

export function hasValidCronJobVisibilityKeys(keys: readonly string[]): boolean {
  return keys.some(key => parseCronJobVisibilityKey(key) !== null)
}

export function cronJobShownInSessions(
  hiddenJobKeys: readonly string[],
  jobId: string,
  profile?: null | string
): boolean {
  return !hiddenJobKeys.includes(cronJobVisibilityKey(jobId, profile))
}

/** Apply per-job opt-outs to an already-acquired cron slice. */
export function visibleCronSessions(rows: readonly SessionInfo[], hiddenJobKeys: readonly string[]): SessionInfo[] {
  const hiddenJobs = hiddenJobKeys.flatMap(key => {
    const parsed = parseCronJobVisibilityKey(key)

    return parsed ? [parsed] : []
  })

  return rows.filter(session => {
    if (session.archived) {
      return false
    }

    const profile = normalizeVisibilityProfile(session.profile)

    return !hiddenJobs.some(([jobProfile, jobId]) => jobProfile === profile && session.id.startsWith(`cron_${jobId}_`))
  })
}

export function sessionPresentationKey(session: Pick<SessionInfo, 'id' | 'profile'>): string {
  return JSON.stringify([normalizeVisibilityProfile(session.profile), session.id])
}

/** Merge only for Sessions presentation; acquisition stays independently paged. */
export function mergeSessionsForPresentation(
  ordinary: readonly SessionInfo[],
  cron: readonly SessionInfo[]
): SessionInfo[] {
  const seen = new Set<string>()

  return [...ordinary, ...cron]
    .filter(session => {
      const key = JSON.stringify([normalizeVisibilityProfile(session.profile), session._lineage_root_id ?? session.id])

      if (seen.has(key)) {
        return false
      }

      seen.add(key)

      return true
    })
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0))
}

export function sessionsFeedShowsLoadMore({
  agentsGrouped,
  cronTruncated,
  ordinaryHasMore,
  sessionsLoading,
  showAllProfiles
}: {
  agentsGrouped: boolean
  cronTruncated: boolean
  ordinaryHasMore: boolean
  sessionsLoading: boolean
  showAllProfiles: boolean
}): boolean {
  if (sessionsLoading) {
    return false
  }

  return showAllProfiles ? cronTruncated : !agentsGrouped && (ordinaryHasMore || cronTruncated)
}
