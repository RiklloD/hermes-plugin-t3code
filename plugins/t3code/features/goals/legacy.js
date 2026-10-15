/** Read compatibility for backends started before the goals REST route existed.
 * No slash dispatch, extra agent turn, or restart of a live native agent.
 */
import { host } from '@hermes/plugin-sdk'

const cache = new Map()
export function clearLegacyGoals() { cache.clear() }

export function confirmedNativeGoal(rows, initial = null) {
  let goal = initial, command = null
  for (const row of rows) {
    if (row.display_kind === 'hidden') continue
    const text = String(row.content || '').trim()
    if (row.role === 'user') {
      const match = text.match(/^\/(?:claude:)?goal(?:\s+([\s\S]*))?$/)
      command = match ? String(match[1] || '').trim() : null
    } else if (row.role === 'assistant' && command !== null) {
      const objective = command.startsWith('set ') ? command.slice(4).trim() : command
      if (text.startsWith('Goal set: ') && text.slice(10).trim() === objective && objective) {
        goal = { objective, status: 'active', source: 'native', updated_at: row.timestamp }
      } else if (['clear', 'off'].includes(command) && text.startsWith('Goal cleared')) {
        goal = { objective: '', status: 'cleared', updated_at: row.timestamp }
      } else if (['', 'status'].includes(command)) {
        const active = text.match(/^Goal active: ([\s\S]+?) \((?:not yet evaluated|(\d+) turns?)\)/)
        if (active) goal = { objective: active[1], status: 'active', checks: Number(active[2] || 0), updated_at: row.timestamp }
        else if (text.startsWith('No goal set')) goal = { objective: '', status: 'cleared', updated_at: row.timestamp }
      }
      command = null
    }
  }
  return goal
}

async function readNativeGoal(owner, sessionId, bridge) {
  const key = JSON.stringify([owner.connectionId, owner.profile, sessionId])
  const previous = cache.get(key)
  const limit = previous ? 50 : 500
  let rows = [], offset = 0
  for (;;) {
    const query = new URLSearchParams({ profile: owner.profile, limit: String(limit), offset: String(offset), order: 'latest', include_compacted: 'true', inline_images: 'false' })
    const page = await bridge({ connectionId: owner.connectionId, profile: owner.profile,
      path: `/api/sessions/${encodeURIComponent(sessionId)}/messages?${query}`, timeoutMs: 10_000 })
    if (page.session_id !== sessionId) throw new Error('Goal history belongs to another thread')
    const messages = [...(page.messages || [])].sort((a, b) => a.id - b.id)
    rows = [...messages, ...rows]
    if (messages.length < limit || (previous && messages.some(row => row.id <= previous.lastId)) ||
        (!previous && confirmedNativeGoal(rows))) break
    offset += messages.length
  }
  const newRows = previous ? rows.filter(row => row.id > previous.lastId) : rows
  if (previous?.lastUser && !newRows.some(row => row.id === previous.lastUser.id)) newRows.unshift(previous.lastUser)
  const goal = confirmedNativeGoal(newRows, previous?.goal)
  const lastUser = [...newRows].reverse().find(row => row.role === 'user' && row.display_kind !== 'hidden') || previous?.lastUser
  if (!cache.has(key) && cache.size >= 32) cache.delete(cache.keys().next().value)
  cache.set(key, { goal, lastUser, lastId: Math.max(previous?.lastId || 0, ...rows.map(row => row.id)) })
  return goal
}

export async function legacyGoal(owner, sessionId, runtimeId, bridge) {
  const route = { ...owner, targetProfile: owner.profile, mode: owner.connectionId === 'local' ? 'local' : 'remote' }
  let core = null
  if (runtimeId) {
    const snapshot = await host.requestProfile(route, 'session.control.read', { session_id: runtimeId })
    const state = snapshot?.control?.goal
    if (state) core = { ...state, objective: state.goal, source: 'hermes',
      updated_at: Math.max(state.created_at || 0, state.last_turn_at || 0) }
  }
  const native = await readNativeGoal(owner, sessionId, bridge)
  const goal = !native || (core && core.updated_at >= native.updated_at) ? core : native
  return goal?.status === 'cleared' ? null : goal
}
