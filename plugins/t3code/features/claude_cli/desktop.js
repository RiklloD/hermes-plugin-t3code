import React from 'react'
import * as sdk from '@hermes/plugin-sdk'
const {
  host, useValue, useQuery, queryClient, surfaceModelSwitchConfirm,
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuSeparator
} = sdk

const h = React.createElement
const QUERY = 't3code-claude-context'

function hasNativeMillion(model) {
  const match = model.match(/^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?$/)
  if (!match) return false
  const [, family, majorText, minorText] = match
  const major = Number(majorText), minor = Number(minorText || 0)
  return (family === 'fable' && major >= 5) ||
    (family === 'opus' && (major > 4 || (major === 4 && minor >= 7))) ||
    (family === 'sonnet' && major >= 5) ||
    (family === 'haiku' && (major > 5 || (major === 5 && minor >= 5)))
}

function ContextPicker() {
  const sessionId = useValue(host.state.focusedSessionId)
  const owner = useValue(host.state.focusedSessionOwner)
  const busy = useValue(host.state.busy)
  const modelHint = useValue(host.state.model)
  const [saving, setSaving] = React.useState(false)
  const route = owner && { ...owner, targetProfile: owner.profile, mode: owner.connectionId === 'local' ? 'local' : 'remote' }
  const { data: info } = useQuery({
    queryKey: [QUERY, owner?.connectionId, owner?.profile, sessionId, modelHint],
    enabled: Boolean(sessionId && route),
    queryFn: () => host.requestProfile(route, 'session.info', { session_id: sessionId }),
    staleTime: 30_000,
    retry: false
  })
  if (!info || info.provider !== 'claude-cli' || !String(info.model || '').startsWith('claude-')) return null
  const model = info.model || ''
  const base = model.replace(/\[(200k|1m)\]$/i, '')
  // Older 4.6 models need account-entitled [1m] variants. Do not invent access.
  const million = hasNativeMillion(base) || /\[1m\]$/i.test(model)
  const value = /\[200k\]$/i.test(model) || !million ? '200K' : '1M'

  async function select(context) {
    if (saving || busy) return
    const target = context === '200k' ? `${base}[200k]` : `${base}[1m]`
    if (target === model) return
    setSaving(true)
    const params = { session_id: sessionId, key: 'model', value: `${target} --provider claude-cli --session` }
    const stale = () => host.state.focusedSessionId.get() !== sessionId ||
      host.state.focusedSessionOwner.get()?.connectionId !== owner.connectionId
    try {
      const result = await host.requestProfile(route, 'config.set', params)
      if (result?.confirm_required) {
        await surfaceModelSwitchConfirm({
          confirmMessage: result.confirm_message,
          failureMessage: 'Could not change Claude context window.',
          model: target, isStale: stale,
          requestConfirmed: () => host.requestProfile(route, 'config.set', { ...params, confirm_expensive_model: true })
        })
      }
      await queryClient.invalidateQueries({ queryKey: [QUERY] })
    } catch (error) {
      host.notify({ kind: 'error', message: `Could not change Claude context: ${error.message || error}` })
    } finally { setSaving(false) }
  }

  return h(DropdownMenu, null,
    h(DropdownMenuTrigger, { asChild: true },
      h('button', { disabled: busy || saving, title: 'Claude CLI context window',
        style: { color: 'var(--ui-text-secondary)', padding: '0 6px', fontSize: 'inherit' } },
      `Context ${value}`)),
    h(DropdownMenuContent, { align: 'end' },
      h('div', { style: { padding: '6px 8px', fontWeight: 600 } }, 'Claude context window'),
      h(DropdownMenuItem, { onSelect: () => select('200k') }, `${value === '200K' ? '✓ ' : ''}200K · compact earlier`),
      h(DropdownMenuItem, { disabled: !million, onSelect: () => select('1m') }, `${value === '1M' ? '✓ ' : ''}1M · full context`),
      h(DropdownMenuSeparator),
      h('div', { style: { maxWidth: '270px', padding: '6px 8px', fontSize: 11, whiteSpace: 'normal', color: 'var(--ui-text-tertiary)' } },
        'Applies to this chat. Native conversation history is preserved.')))
}

export default {
  register(ctx) {
    ctx.register({ id: 'context-window', area: 'statusBar.right', order: -10, render: () => h(ContextPicker) })
    ctx.onEvent('session.info', () => {
      void queryClient.invalidateQueries({ queryKey: [QUERY] })
    })
  }
}
