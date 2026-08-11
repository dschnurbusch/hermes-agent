import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import type { VirtualSessionListProps } from './virtual-session-list'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        }
      }
    }
  })
}))

const mockVirtualListPropsHistory: VirtualSessionListProps[] = []
const renderedRowProps = vi.hoisted(() => new Map<string, Record<string, unknown>>())

vi.mock('./virtual-session-list', () => ({
  VirtualSessionList: (props: VirtualSessionListProps) => {
    mockVirtualListPropsHistory.push(props)

    return <div data-testid="virtual-session-list">Virtual List ({props.rows.length} rows)</div>
  }
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: (props: { onResume: () => void; session: SessionInfo }) => {
    renderedRowProps.set(`${props.session.profile}-${props.session.id}`, props as unknown as Record<string, unknown>)

    return (
      <button
        data-testid={`session-${props.session.profile}-${props.session.id}`}
        onClick={props.onResume}
        type="button"
      >
        {props.session.profile}/{props.session.id}
      </button>
    )
  }
}))

function makeSession(id: string, startedAt = 1000): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  } as unknown as SessionInfo
}

function generateSessions(count: number): SessionInfo[] {
  return Array.from({ length: count }, (_, i) => makeSession(`session-${i + 1}`, 10000 - i * 100))
}

const noop = () => {}

describe('SidebarSessionsSection memoization & virtualizer stability', () => {
  it('memoizes flatRows and passes the exact same rows array reference across parent re-renders', () => {
    mockVirtualListPropsHistory.length = 0

    const sessions = generateSessions(VIRTUALIZE_THRESHOLD + 5)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(1)
    const initialRowsRef = mockVirtualListPropsHistory[0].rows
    expect(initialRowsRef.length).toBeGreaterThan(VIRTUALIZE_THRESHOLD)

    // Re-render parent with the exact same sessions array and props
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(2)
    const nextRowsRef = mockVirtualListPropsHistory[1].rows

    // Confirm that the flatRows array reference remains strictly identical across renders (useMemo proof)
    expect(nextRowsRef).toBe(initialRowsRef)
  })

  it('re-computes flatRows reference when grouping or sessions change', () => {
    mockVirtualListPropsHistory.length = 0

    const initialSessions = generateSessions(VIRTUALIZE_THRESHOLD + 2)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="none"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const firstRowsRef = mockVirtualListPropsHistory[0].rows

    // Switch on date dividers
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const secondRowsRef = mockVirtualListPropsHistory[1].rows
    expect(secondRowsRef).not.toBe(firstRowsRef)

    // Change sessions array identity
    const updatedSessions = generateSessions(VIRTUALIZE_THRESHOLD + 4)
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={updatedSessions}
      />
    )

    const thirdRowsRef = mockVirtualListPropsHistory[2].rows
    expect(thirdRowsRef).not.toBe(secondRowsRef)
  })
})

const ownedSession = (profile: string, source = 'desktop'): SessionInfo =>
  ({
    archived: false,
    cwd: null,
    ended_at: null,
    id: 'same',
    input_tokens: 0,
    is_active: false,
    last_active: 100,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile,
    source,
    started_at: 100,
    title: `${profile} same`,
    tool_call_count: 0
  }) as SessionInfo

describe('Sessions cron rows', () => {
  it('opens a cron row with its actual owner', () => {
    const onResumeSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onResumeSession={onResumeSession}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleUnread={vi.fn()}
        open
        pinned={false}
        sessions={[ownedSession('work', 'cron')]}
      />
    )

    fireEvent.click(screen.getByTestId('session-work-same'))

    expect(onResumeSession).toHaveBeenCalledWith('same', 'work')
  })

  it('leaves ordinary row resume behavior profile-agnostic', () => {
    const onResumeSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onResumeSession={onResumeSession}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleUnread={vi.fn()}
        open
        pinned={false}
        sessions={[ownedSession('work')]}
      />
    )

    fireEvent.click(screen.getByTestId('session-work-same'))

    expect(onResumeSession).toHaveBeenCalledWith('same', undefined)
  })

  it('provides archive for a cron row and preserves its owner', () => {
    const onArchiveSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={onArchiveSession}
        onDeleteSession={vi.fn()}
        onResumeSession={vi.fn()}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleUnread={vi.fn()}
        open
        pinned={false}
        sessions={[ownedSession('work', 'cron')]}
      />
    )

    const props = renderedRowProps.get('work-same')

    expect(props?.onArchive).toBeTypeOf('function')
    expect(props?.onDelete).toBeUndefined()
    expect(props?.onResume).toBeTypeOf('function')
    ;(props?.onArchive as (() => void) | undefined)?.()
    expect(onArchiveSession).toHaveBeenCalledWith('same', 'work')
  })
})
