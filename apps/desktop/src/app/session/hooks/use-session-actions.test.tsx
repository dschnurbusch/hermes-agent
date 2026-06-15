import { cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setSessionArchived } from '@/hermes'
import { $pinnedSessionIds } from '@/store/layout'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import {
  $currentCwd,
  $messagingPlatformTotals,
  $messagingSessions,
  $sessions,
  $sessionsTotal
} from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import type { ClientSessionState } from '../../types'

import { useSessionActions } from './use-session-actions'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

const RUNTIME_SESSION_ID = 'rt-new-001'

type SessionActionsHandle = ReturnType<typeof useSessionActions>

function baseSession(overrides: Partial<SessionInfo>): SessionInfo {
  return {
    cwd: null,
    ended_at: null,
    id: 'session-1',
    input_tokens: 0,
    is_active: false,
    is_default_profile: true,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: '',
    profile: 'default',
    source: 'tui',
    started_at: 1,
    title: null,
    tool_call_count: 0,
    ...overrides
  }
}

function ActionsHarness({
  onReady,
  requestGateway = vi.fn(async () => ({} as never)),
  selectedStoredSessionId = null
}: {
  onReady: (actions: SessionActionsHandle) => void
  requestGateway?: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  selectedStoredSessionId?: null | string
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    navigate: vi.fn() as never,
    requestGateway,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId,
    selectedStoredSessionIdRef: ref<string | null>(selectedStoredSessionId),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions)
  }, [actions, onReady])

  return null
}

function Harness({
  onReady,
  requestGateway
}: {
  onReady: (create: (preview?: string | null) => Promise<string | null>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    navigate: vi.fn() as never,
    requestGateway,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions.createBackendSessionForSend)
  }, [actions.createBackendSessionForSend, onReady])

  return null
}

async function createWith(profileSetup: () => void): Promise<Record<string, unknown> | undefined> {
  let createParams: Record<string, unknown> | undefined

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.create') {
      createParams = params

      return { session_id: RUNTIME_SESSION_ID, stored_session_id: null } as never
    }

    return {} as never
  })

  $currentCwd.set('')
  profileSetup()

  let create: ((preview?: string | null) => Promise<string | null>) | null = null
  render(<Harness onReady={c => (create = c)} requestGateway={requestGateway} />)
  await waitFor(() => expect(create).not.toBeNull())
  await create!()

  return createParams
}

describe('createBackendSessionForSend profile routing', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    $sessions.set([])
    $sessionsTotal.set(0)
    $messagingSessions.set([])
    $messagingPlatformTotals.set({})
    $pinnedSessionIds.set([])
    vi.restoreAllMocks()
  })

  it('routes a plain new chat (no explicit profile) to the live gateway profile', async () => {
    // The "rubberband to default" bug: the top New Session button clears
    // $newChatProfile to null. In global-remote mode one backend serves every
    // profile, so an omitted `profile` lands the chat on the launch (default)
    // profile. The session must instead carry the active gateway profile.
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'coder' })
  })

  it('honours an explicit per-profile "+" selection', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set('analyst')
    })

    expect(params).toMatchObject({ profile: 'analyst' })
  })

  it('passes the default profile for single-profile users (backend resolves it to launch)', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('default')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'default' })
  })
})

describe('archiveSession sidebar stores', () => {
  afterEach(() => {
    cleanup()
    $sessions.set([])
    $sessionsTotal.set(0)
    $messagingSessions.set([])
    $messagingPlatformTotals.set({})
    $pinnedSessionIds.set([])
    vi.restoreAllMocks()
  })

  it('removes a Telegram bucket row from the messaging store and sends its profile', async () => {
    const telegram = baseSession({ id: 'tg-1', profile: 'slorg', source: 'telegram', title: 'Telegram chat' })
    $sessions.set([])
    $sessionsTotal.set(7)
    $messagingSessions.set([telegram])
    $messagingPlatformTotals.set({ telegram: 3 })
    vi.mocked(setSessionArchived).mockResolvedValue({ archived: true, ok: true, title: '' } as never)

    let actions: SessionActionsHandle | null = null
    render(<ActionsHarness onReady={next => (actions = next)} />)
    await waitFor(() => expect(actions).not.toBeNull())

    await actions!.archiveSession('tg-1')

    expect(setSessionArchived).toHaveBeenCalledWith('tg-1', true, 'slorg')
    expect($messagingSessions.get()).toEqual([])
    expect($messagingPlatformTotals.get()).toEqual({ telegram: 2 })
    // Archiving a platform bucket row must not perturb the main Sessions footer.
    expect($sessionsTotal.get()).toBe(7)
  })

  it('restores a messaging bucket row if the archive request fails', async () => {
    const discord = baseSession({ id: 'disc-1', source: 'discord', title: 'Discord chat' })
    $messagingSessions.set([discord])
    $messagingPlatformTotals.set({ discord: 4 })
    vi.mocked(setSessionArchived).mockRejectedValue(new Error('boom'))

    let actions: SessionActionsHandle | null = null
    render(<ActionsHarness onReady={next => (actions = next)} />)
    await waitFor(() => expect(actions).not.toBeNull())

    await actions!.archiveSession('disc-1')

    expect($messagingSessions.get()).toEqual([discord])
    expect($messagingPlatformTotals.get()).toEqual({ discord: 4 })
  })
})
