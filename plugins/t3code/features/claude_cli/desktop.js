import React from 'react'
import * as sdk from '@hermes/plugin-sdk'
const {
  host, atom, useValue, useQuery, queryClient, surfaceModelSwitchConfirm,
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuSeparator
} = sdk

const h = React.createElement
const QUERY = 't3code-claude-context'
const ACCOUNTS_QUERY = 't3code-claude-accounts'
let featureCtx = null
// Status-bar visibility of the Claude accounts button (local Desktop pref, applies live).
let $showAccounts = atom(true)
const CLAUDE_PROVIDERS = ['claude-cli', 'claude-cli-2']

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
  if (!info || !CLAUDE_PROVIDERS.includes(info.provider) || !String(info.model || '').startsWith('claude-')) return null
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
    const params = { session_id: sessionId, key: 'model', value: `${target} --provider ${info.provider} --session` }
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

function AccountsPanel() {
  const owner = useValue(host.state.focusedSessionOwner)
  const { data, error, isFetching, refetch } = useQuery({
    queryKey: [ACCOUNTS_QUERY, owner?.connectionId, owner?.profile],
    queryFn: () => featureCtx.rest('/accounts', { timeoutMs: 25_000 }),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false
  })

  const accounts = data?.accounts || []
  const anyNeedsLogin = accounts.some(a => !a.logged_in)
  const triggerColor = anyNeedsLogin ? 'var(--ui-yellow)' : 'var(--ui-text-tertiary)'

  return h(DropdownMenu, null,
    h(DropdownMenuTrigger, { asChild: true },
      h('button', {
        title: 'Claude account status',
        style: { color: triggerColor, padding: '0 6px', fontSize: 'inherit', opacity: isFetching ? 0.6 : 1 }
      }, 'Claude')),
    h(DropdownMenuContent, { align: 'end' },
      h('div', { style: { padding: '6px 8px', fontWeight: 600 } }, 'Claude accounts'),
      accounts.length === 0
        ? h('div', { style: { padding: '6px 8px', fontSize: 12, color: 'var(--ui-text-tertiary)', maxWidth: 300 } }, error ? 'Account status unavailable. Restart Hermes after the backend update.' : 'Loading…')
        : accounts.map(acc =>
            h(React.Fragment, { key: acc.id },
              h('div', { style: { padding: '6px 8px 2px', display: 'flex', alignItems: 'center', gap: 8 } },
                h('span', { style: { fontWeight: 500 } }, acc.label),
                h('span', {
                  style: {
                    marginLeft: 'auto', fontSize: 11,
                    color: acc.logged_in ? 'var(--ui-green)' : 'var(--ui-yellow)'
                  }
                }, acc.logged_in ? '● signed in' : '○ not signed in')),
              acc.email ? h('div', { style: { padding: '0 8px 2px', fontSize: 11, color: 'var(--ui-text-tertiary)' } }, acc.email) : null,
              acc.plan ? h('div', { style: { padding: '0 8px 2px', fontSize: 11, color: 'var(--ui-text-tertiary)' } }, acc.plan) : null,
              !acc.logged_in && acc.login_command
                ? h(DropdownMenuItem, {
                    onSelect: async () => {
                      try { await navigator.clipboard.writeText(acc.login_command); host.notify({ kind: 'success', message: 'Native login command copied. Run it in your terminal.' }) }
                      catch { host.notify({ kind: 'error', message: 'Could not copy the login command.' }) }
                    },
                    style: { fontSize: 11, fontFamily: 'monospace', color: 'var(--ui-text-secondary)', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis' }
                  }, `Copy: ${acc.login_command}`)
                : null
            )
          ),
      h(DropdownMenuSeparator),
      h(DropdownMenuItem, { disabled: isFetching, onSelect: () => void refetch() }, 'Refresh accounts'),
      h('div', { style: { padding: '4px 8px 6px', fontSize: 11, color: 'var(--ui-text-tertiary)', whiteSpace: 'normal', maxWidth: 300 } },
        'Choose Claude CLI for account A or Claude CLI 2 for account B in a new chat’s model picker. Existing chats stay bound to their account.')))
}

// Unmounting the panel (not hiding it) also stops its account poll.
function AccountsSlot() {
  return useValue($showAccounts) ? h(AccountsPanel) : null
}

function SettingsPage() {
  const show = useValue($showAccounts)
  return h('label', { style: { display: 'flex', gap: 8, alignItems: 'flex-start', padding: 4 } },
    h('input', {
      type: 'checkbox', checked: show,
      onChange: e => { $showAccounts.set(e.target.checked); featureCtx.storage.set('showAccounts', e.target.checked) }
    }),
    h('span', null, 'Show Claude accounts in the status bar',
      h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } },
        'The "Claude" button listing account A and B sign-in status. The 200K/1M context picker stays either way.')))
}

export default {
  register(ctx) {
    featureCtx = ctx
    $showAccounts = atom(ctx.storage.get('showAccounts', true) !== false)
    ctx.register({ id: 'context-window', area: 'statusBar.right', order: -10, render: () => h(ContextPicker) })
    ctx.register({ id: 'claude-accounts', area: 'statusBar.right', order: -9, render: () => h(AccountsSlot) })
    ctx.registerSettingsPage?.({ icon: 'user', id: 'settings', render: () => h(SettingsPage), title: 'Claude CLI' })
    ctx.onEvent('session.info', () => {
      void queryClient.invalidateQueries({ queryKey: [QUERY] })
    })
  }
}
