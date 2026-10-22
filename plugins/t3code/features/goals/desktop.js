/** T3's persistent goal callout, backed by the thread rather than its provider. */
import React from 'react'
import { host, useValue, useQuery, queryClient, COMPOSER_AREAS, Tip } from '@hermes/plugin-sdk'

import { legacyGoal } from './legacy.js'

const legacyBackends = new Set()
const h = React.createElement
const QUERY = 't3code-goals'
const LABELS = { active: 'Pursuing goal', waiting: 'Waiting on goal', paused: 'Goal paused', done: 'Goal complete', complete: 'Goal complete' }

export async function fetchGoal(owner, sessionId, runtimeId = null) {
  if (!owner?.connectionId || !owner?.profile || !sessionId) return null
  const bridge = window.hermesDesktop?.api
  if (typeof bridge !== 'function') throw new Error('Hermes desktop bridge unavailable')
  const backend = `${owner.connectionId}/${owner.profile}`
  if (legacyBackends.has(backend)) return legacyGoal(owner, sessionId, runtimeId, bridge)
  let result
  try { result = await bridge({
    connectionId: owner.connectionId,
    profile: owner.profile,
    path: `/api/plugins/t3code/goals/sessions/${encodeURIComponent(sessionId)}?profile=${encodeURIComponent(owner.profile)}`,
    timeoutMs: 10_000
  }) } catch (error) {
    if (error?.status !== 404 && error?.statusCode !== 404 && !/\b404\b/.test(String(error?.message || error))) throw error
    if (legacyBackends.size >= 32) legacyBackends.delete(legacyBackends.values().next().value)
    legacyBackends.add(backend)
    return legacyGoal(owner, sessionId, runtimeId, bridge)
  }
  if (result?.session_id !== sessionId) throw new Error('Goal response belongs to another thread')
  return result.goal || null
}

// lucide `target`, the icon T3 Code's goal banner uses.
const TargetIcon = () => h('svg', { width: 12, height: 12, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true },
  h('circle', { cx: 12, cy: 12, r: 10 }), h('circle', { cx: 12, cy: 12, r: 6 }), h('circle', { cx: 12, cy: 12, r: 2 }))

const MUTED = 'var(--ui-text-secondary, var(--muted-foreground))'

/** T3 Code's goal row (ComposerBannerStack): muted target, medium title, muted one-line objective. */
export function GoalCallout({ goal, stale = false, retry }) {
  if (!goal?.objective || goal.status === 'cleared') return null
  const title = LABELS[goal.status] || 'Goal'
  const objective = h('span', { style: { minWidth: 0, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
    whiteSpace: 'nowrap', color: MUTED } }, goal.objective)
  return h('div', {
    'data-t3-goal': goal.status, role: 'status', 'aria-live': 'polite',
    style: { display: 'flex', alignItems: 'center', gap: 4, minWidth: 0, height: 28, fontSize: 12, lineHeight: '16px' }
  },
  h('span', { style: { display: 'flex', width: 24, flex: 'none', justifyContent: 'center', color: MUTED } }, h(TargetIcon)),
  h('span', { style: { flex: 'none', fontWeight: 500, color: 'var(--ui-text-primary, var(--foreground))' } }, title),
  Tip ? h(Tip, { label: goal.objective }, objective) : objective,
  stale && h('button', { type: 'button', onClick: retry, title: 'Goal status could not refresh',
    style: { flex: 'none', background: 'none', border: 0, padding: '0 6px', color: MUTED, font: 'inherit', cursor: 'pointer' } }, 'Retry'))
}

export function GoalStrip() {
  const sessionId = useValue(host.state.focusedStoredSessionId)
  const owner = useValue(host.state.focusedSessionOwner)
  const runtimeId = useValue(host.state.focusedSessionId)
  const query = useQuery({
    queryKey: [QUERY, owner?.connectionId, owner?.profile, sessionId, runtimeId],
    queryFn: () => fetchGoal(owner, sessionId, runtimeId),
    enabled: Boolean(sessionId && owner?.connectionId && owner?.profile),
    // Goal edits arrive through the events below; this poll is only a backstop (each fetch scans
    // the thread's messages in state.db, so 2.5 s kept a gateway worker busy). Back off on errors.
    refetchInterval: q => (q.state.status === 'error' ? 60_000 : 15_000), staleTime: 1000, retry: 1
  })
  // No provider/model/busy check: keep the objective visible between turns,
  // while native background tasks run, and when the model/harness changes.
  return h(GoalCallout, { key: `${owner?.connectionId}/${owner?.profile}/${sessionId}`,
    goal: query.data, stale: query.isError, retry: () => void query.refetch() })
}

export default {
  register(ctx) {
    ctx.register({ id: 'pursuing-goal', area: COMPOSER_AREAS.top, order: -100,
      render: () => h(GoalStrip) })
    for (const event of ['session.control.update', 'session.resumed', 'session.info', 'turn.completed', 'message.final']) {
      ctx.onEvent(event, () => void queryClient.invalidateQueries({ queryKey: [QUERY] }))
    }
  }
}
