import { atom } from 'nanostores'

export const SIDEBAR_SESSIONS_PAGE_SIZE = 50
export const $sessionsLimit = atom(SIDEBAR_SESSIONS_PAGE_SIZE)

export function bumpSessionsLimit(step: number = SIDEBAR_SESSIONS_PAGE_SIZE): void {
  const safeStep = Math.max(1, Math.floor(step))

  $sessionsLimit.set($sessionsLimit.get() + safeStep)
}

/** Raise the window to at least `floor`, never shrinking it. Returns true when
 * it moved, so the caller knows a refetch is worth it. */
export function raiseSessionsLimit(floor: number): boolean {
  if ($sessionsLimit.get() >= floor) {
    return false
  }

  $sessionsLimit.set(floor)

  return true
}

export function resetSessionsLimit(): void {
  if ($sessionsLimit.get() !== SIDEBAR_SESSIONS_PAGE_SIZE) {
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE)
  }
}
