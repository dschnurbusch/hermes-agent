// The desktop REST/WS client, split by domain under src/api/. This module is
// the compatibility barrel: every helper keeps its historical `@/hermes`
// import path while the implementations live in focused files.
// client is the one module with internals: profileScoped / connectionScoped /
// capabilityScoped are shared across api/ but must not reach call sites, or
// request scoping stops having a single owner.
import { isMissingRestEndpoint } from '@/lib/gateway-rpc'
import type { CronJob, CronJobUpdates, SessionInfo } from '@/types/hermes'

import { connectionScoped, hermesApi, profileScoped } from './api/client'
import { listAllProfileSessions as listAllProfileSessionsBase } from './api/sessions'

export {
  getApiRequestConnection,
  getApiRequestProfile,
  hermesApi,
  HermesGateway,
  profileScopeKey,
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS,
  setApiRequestConnection,
  setApiRequestProfile,
  STARTUP_REQUEST_TIMEOUT_MS
} from './api/client'
export type { ProfileScope } from './api/client'
export * from './api/config'
export * from './api/cron'
export * from './api/local-models'
export * from './api/mcp'
export * from './api/messaging'
export * from './api/models'
export * from './api/plugins'
export * from './api/profiles'
export * from './api/sessions'
export * from './api/skills'
export * from './api/system'
export * from './api/toolsets'

export type {
  ActionResponse,
  ActionStatusResponse,
  AnalyticsDailyEntry,
  AnalyticsModelEntry,
  AnalyticsResponse,
  AnalyticsSkillEntry,
  AnalyticsSkillsSummary,
  AnalyticsTotals,
  AudioSpeakResponse,
  AudioTranscriptionResponse,
  AudioTtsLeaseResponse,
  AutomationBlueprint,
  AutomationBlueprintField,
  AuxiliaryModelsResponse,
  BackendUpdateCheckResponse,
  ComputerUseCheck,
  ComputerUsePermissionSource,
  ComputerUseStatus,
  ConfigFieldSchema,
  ConfigSchemaResponse,
  CronDeliveryTarget,
  CronJob,
  CronJobCreatePayload,
  CronJobSchedule,
  CronJobUpdates,
  CuratorStatusResponse,
  CustomEndpoint,
  CustomEndpointsResponse,
  CustomEndpointUpdate,
  CustomEndpointValidationResponse,
  DebugShareResponse,
  ElevenLabsVoice,
  ElevenLabsVoicesResponse,
  EnvVarInfo,
  GatewayReadyPayload,
  HermesConfig,
  HermesConfigRecord,
  LogsResponse,
  McpCatalogEntry,
  McpCatalogResponse,
  McpServerSummary,
  McpServerTestResponse,
  MemoryProviderConfig,
  MemoryProviderOAuthStatus,
  MemoryStatusResponse,
  MessagingEnvVarInfo,
  MessagingHomeChannel,
  MessagingPlatformInfo,
  MessagingPlatformsResponse,
  MessagingPlatformTestResponse,
  MessagingPlatformUpdate,
  MoaConfigResponse,
  MoaModelSlot,
  ModelAssignmentRequest,
  ModelAssignmentResponse,
  ModelInfoResponse,
  ModelOptionProvider,
  ModelOptionsResponse,
  PaginatedSessions,
  PairingResponse,
  PairingUser,
  ProfileCreatePayload,
  ProfileDesktopOverlay,
  ProfileInfo,
  ProfileSetupCommand,
  ProfileSoul,
  ProfilesResponse,
  ProjectFolder,
  ProjectInfo,
  ProjectsPayload,
  RpcEvent,
  SessionCreateResponse,
  SessionInfo,
  SessionMessage,
  SessionMessagesResponse,
  SessionResumeResponse,
  SessionRuntimeInfo,
  SessionSearchResponse,
  SessionSearchResult,
  SkillHubInstalledEntry,
  SkillHubPreview,
  SkillHubResult,
  SkillHubScanResult,
  SkillHubSearchResponse,
  SkillHubSource,
  SkillHubSourcesResponse,
  SkillInfo,
  StaleAuxAssignment,
  StarmapGraph,
  StatusResponse,
  ToolsetConfig,
  ToolsetInfo,
  ToolsetModel,
  ToolsetModelsResponse,
  WebhookCreatePayload,
  WebhookCreateResponse,
  WebhookEnableResponse,
  WebhookRoute,
  WebhooksResponse
} from '@/types/hermes'

// This commit predates the api/ domain split. Keep the current barrel and host
// only its conflict-scoped compatibility delta here instead of restoring the
// obsolete monolith.
export interface SidebarSessionSlice {
  sessions: SessionInfo[]
  profiles_truncated?: Record<string, boolean>
  profiles_usage?: Record<string, { cost_usd: number; tokens: number }>
}

export interface SidebarSessionsResponse {
  capabilities?: { cron_profile?: boolean }
  recents: SidebarSessionSlice
  cron: SidebarSessionSlice
  messaging: SidebarSessionSlice
  errors?: Array<{ profile: string; error: string }>
}

export interface SidebarSessionsRequest {
  recentsProfile: 'all' | (string & {})
  recentsLimit: number
  recentsExclude: string[]
  cronProfile?: 'all' | (string & {})
  cronLimit: number
  messagingLimit: number
  messagingExclude: string[]
}

let sidebarBatchEndpointMissing = false

export function resetSidebarBatchCapability(): void {
  sidebarBatchEndpointMissing = false
}

function profilesTruncatedFrom(sessions: SessionInfo[], cap: number): Record<string, boolean> {
  const counts = new Map<string, number>()

  for (const session of sessions) {
    const key = session.profile || 'default'
    counts.set(key, (counts.get(key) ?? 0) + (session.pinned ? 0 : 1))
  }

  return Object.fromEntries([...counts].map(([name, count]) => [name, count >= cap]))
}

async function listSidebarSessionsLegacy(req: SidebarSessionsRequest): Promise<SidebarSessionsResponse> {
  const [recents, cron, messaging] = await Promise.all([
    listAllProfileSessionsBase(req.recentsLimit, 1, 'exclude', 'recent', req.recentsProfile, {
      excludeSources: req.recentsExclude
    }),
    listAllProfileSessionsBase(req.cronLimit, 1, 'exclude', 'recent', req.cronProfile ?? req.recentsProfile, {
      source: 'cron'
    }),
    listAllProfileSessionsBase(req.messagingLimit, 1, 'exclude', 'recent', req.recentsProfile, {
      excludeSources: req.messagingExclude
    })
  ])

  const errors = [...(recents.errors ?? []), ...(cron.errors ?? []), ...(messaging.errors ?? [])]

  return {
    capabilities: { cron_profile: true },
    recents: {
      profiles_truncated: profilesTruncatedFrom(recents.sessions, req.recentsLimit),
      sessions: recents.sessions
    },
    cron: { sessions: cron.sessions },
    messaging: { sessions: messaging.sessions },
    ...(errors.length ? { errors } : {})
  }
}

export async function listSidebarSessions(req: SidebarSessionsRequest): Promise<SidebarSessionsResponse> {
  if (sidebarBatchEndpointMissing) {
    return listSidebarSessionsLegacy(req)
  }

  const params = new URLSearchParams({
    recents_profile: req.recentsProfile,
    recents_limit: String(Math.max(1, req.recentsLimit)),
    cron_limit: String(Math.max(1, req.cronLimit)),
    messaging_limit: String(Math.max(1, req.messagingLimit))
  })

  if (req.cronProfile) {
    params.set('cron_profile', req.cronProfile)
  }

  if (req.recentsExclude.length) {
    params.set('recents_exclude', req.recentsExclude.join(','))
  }

  if (req.messagingExclude.length) {
    params.set('messaging_exclude', req.messagingExclude.join(','))
  }

  let result: SidebarSessionsResponse

  try {
    result = await hermesApi<SidebarSessionsResponse>({
      path: `/api/profiles/sessions/sidebar?${params.toString()}`,
      timeoutMs: 60_000
    })
  } catch (error) {
    if (!isMissingRestEndpoint(error)) {
      throw error
    }

    sidebarBatchEndpointMissing = true

    return listSidebarSessionsLegacy(req)
  }

  if (req.cronProfile && req.cronProfile !== 'all' && result.capabilities?.cron_profile !== true) {
    return listSidebarSessionsLegacy(req)
  }

  return {
    capabilities: result.capabilities,
    recents: { ...result.recents, sessions: result.recents?.sessions ?? [] },
    cron: { ...result.cron, sessions: result.cron?.sessions ?? [] },
    messaging: { ...result.messaging, sessions: result.messaging?.sessions ?? [] },
    errors: result.errors
  }
}

function cronJobPath(jobId: string, suffix = '', profile?: null | string): string {
  const query = new URLSearchParams()

  if (profile) {
    query.set('profile', profile)
  }

  const separator = suffix.includes('?') ? '&' : '?'

  return `/api/cron/jobs/${encodeURIComponent(jobId)}${suffix}${query.size ? `${separator}${query}` : ''}`
}

export async function getCronJobRuns(jobId: string, limit = 20, profile?: null | string): Promise<SessionInfo[]> {
  const { runs } = await hermesApi<{ runs: SessionInfo[] }>({
    ...profileScoped(profile),
    ...connectionScoped(),
    path: cronJobPath(jobId, `/runs?limit=${limit}`, profile)
  })

  return runs ?? []
}

export function updateCronJob(jobId: string, updates: CronJobUpdates, profile?: null | string): Promise<CronJob> {
  return hermesApi<CronJob>({
    ...profileScoped(profile),
    ...connectionScoped(),
    path: cronJobPath(jobId, '', profile),
    method: 'PUT',
    body: { updates }
  })
}

function cronMutation(jobId: string, suffix: string, profile?: null | string): Promise<CronJob> {
  return hermesApi<CronJob>({
    ...profileScoped(profile),
    ...connectionScoped(),
    path: cronJobPath(jobId, suffix, profile),
    method: 'POST',
    ...(suffix === '/trigger' ? { timeoutMs: 24 * 60 * 60 * 1000 } : {})
  })
}

export const pauseCronJob = (jobId: string, profile?: null | string): Promise<CronJob> =>
  cronMutation(jobId, '/pause', profile)
export const resumeCronJob = (jobId: string, profile?: null | string): Promise<CronJob> =>
  cronMutation(jobId, '/resume', profile)
export const triggerCronJob = (jobId: string, profile?: null | string): Promise<CronJob> =>
  cronMutation(jobId, '/trigger', profile)

export function deleteCronJob(jobId: string, profile?: null | string): Promise<{ ok: boolean }> {
  return hermesApi<{ ok: boolean }>({
    ...profileScoped(profile),
    ...connectionScoped(),
    path: cronJobPath(jobId, '', profile),
    method: 'DELETE'
  })
}
