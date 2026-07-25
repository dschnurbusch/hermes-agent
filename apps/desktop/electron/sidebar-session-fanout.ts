interface SidebarSliceResponse {
  sessions?: unknown[]
  total?: number
  profile_totals?: Record<string, number>
}

interface SidebarSessionsResponse {
  capabilities: { cron_profile: true }
  recents: {
    sessions: unknown[]
    total: number
    profile_totals: Record<string, number>
  }
  cron: { sessions: unknown[] }
  messaging: { sessions: unknown[]; total: number }
  errors: unknown[]
}

type FetchSlice = (searchParams: URLSearchParams, remoteProfiles: string[]) => Promise<SidebarSliceResponse>

const rowsOf = (data: SidebarSliceResponse): unknown[] => (Array.isArray(data?.sessions) ? data.sessions : [])

/** Reassemble the batched endpoint through the remote-aware per-slice path. */
export async function fanOutSidebarSessions(
  searchParams: URLSearchParams,
  remoteProfiles: string[],
  fetchSlice: FetchSlice
): Promise<SidebarSessionsResponse> {
  const recentsProfile = (searchParams.get('recents_profile') || 'all').trim() || 'all'
  const cronProfile = (searchParams.get('cron_profile') || 'all').trim() || 'all'

  const sliceParams = (limitKey: string, defaultLimit: string, extra: Record<string, string>) =>
    new URLSearchParams({
      limit: searchParams.get(limitKey) || defaultLimit,
      offset: '0',
      min_messages: '1',
      archived: 'exclude',
      order: 'recent',
      ...extra
    })

  const recentsSp = sliceParams('recents_limit', '20', { profile: recentsProfile })
  const recentsExclude = searchParams.get('recents_exclude')

  if (recentsExclude) {
    recentsSp.set('exclude_sources', recentsExclude)
  }

  const cronSp = sliceParams('cron_limit', '50', { profile: cronProfile, source: 'cron' })
  const messagingSp = sliceParams('messaging_limit', '100', { profile: 'all' })
  const messagingExclude = searchParams.get('messaging_exclude')

  if (messagingExclude) {
    messagingSp.set('exclude_sources', messagingExclude)
  }

  const [recents, cron, messaging] = await Promise.all([
    fetchSlice(recentsSp, remoteProfiles),
    fetchSlice(cronSp, remoteProfiles),
    fetchSlice(messagingSp, remoteProfiles)
  ])

  return {
    capabilities: { cron_profile: true },
    recents: {
      sessions: rowsOf(recents),
      total: Number(recents?.total) || 0,
      profile_totals: recents?.profile_totals || {}
    },
    cron: { sessions: rowsOf(cron) },
    messaging: {
      sessions: rowsOf(messaging),
      total: Number(messaging?.total) || rowsOf(messaging).length
    },
    errors: []
  }
}
