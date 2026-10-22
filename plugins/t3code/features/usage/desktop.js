/**
 * T3 Code · Usage — T3 Code's Usage page, rebuilt for Hermes as a first-class plugin page.
 *
 * A T3 Code feature module (see shared/desktop/shell.js); bundled into desktop/plugin.js by
 * scripts/build.mjs — edit this file, then rebuild.
 *
 * A port of T3 Code's usage interface (apps/web/src/components/usage/UsagePage.tsx).
 * Mounted in the existing Command Center → Usage tab; a titlebar button left of
 * the Settings gear opens it directly.
 *
 * Data comes from this feature's backend (features/usage/api.py, under /api/plugins/t3code/usage):
 *   GET /usage?days=N  Claude Code + Codex tokens and API-equivalent cost, by
 *                      provider / model / day (hour for N=1), from the CLIs' local
 *                      transcripts plus Hermes' own Codex sessions
 *   GET /limits        live rate-limit windows: Claude get_usage, Codex
 *                      app-server, Z.ai Coding Plan quota
 */
import * as sdk from '@hermes/plugin-sdk'
import React from 'react'

const {
  host, atom, useValue, useQuery, queryClient, cn, Button, Tip,
  ROUTES_AREA, PALETTE_AREA, STATUSBAR_AREAS, TITLEBAR_AREAS
} = sdk

const h = React.createElement
const { useLayoutEffect, useRef, useState } = React

// T3 Code feature identity: NS keys palette commands and queries.
const NS = 't3code.usage'
const API_BASE = '/api/plugins/t3code/usage'
const ROUTE = '/t3code-usage'
// Where the user wants it: inside Command Center, on the Usage section that
// already exists there (Sessions · System · Usage · Maintenance).
const CC_USAGE = '/command-center?section=usage'

let ctxRef = null
let $usagePrefs = null
let $tick = null

// ─── usage page (T3 Code's Usage page, profiles as providers) ───────────────
const USAGE_WINDOWS = [
  { days: 1, label: '24H' },
  { days: 7, label: '7D' },
  { days: 30, label: '30D' },
  { days: 90, label: '90D' }
]
const USAGE_METRICS = [
  { id: 'tokens', label: 'Tokens' },
  { id: 'cost', label: 'Cost' },
  { id: 'limits', label: 'Limits' }
]
// T3's PROVIDER_PRESENTATION, in its reading order.
const PROVIDERS = {
  codex: { color: 'var(--ui-text-primary)', label: 'Codex' },
  claude: { color: '#d97757', label: 'Claude Code' },
  zai: { color: '#3d6df2', label: 'Z.ai' },
  antigravity: { color: '#8c7bd1', label: 'Antigravity' },
  muse: { color: '#7d8590', label: 'Muse Code' }
}
const WINDOW_KIND_ORDER = { session: 0, weekly: 1, monthly: 2 }
const PROVIDER_ORDER = ['codex', 'claude', 'antigravity', 'muse']

function setUsagePrefs(patch) {
  const next = { ...$usagePrefs.get(), ...patch }
  $usagePrefs.set(next)
  ctxRef?.storage.set('usagePrefs', next)
}

const fmtTokens = n => {
  const v = Number(n) || 0
  const unit = (d, s) => `${(v / d).toFixed(v / d >= 100 ? 0 : 1).replace(/\.0$/, '')}${s}`

  return v >= 1e9 ? unit(1e9, 'B') : v >= 1e6 ? unit(1e6, 'M') : v >= 1e3 ? unit(1e3, 'K') : String(Math.round(v))
}

const fmtUsd = n => {
  const v = Number(n) || 0

  return `$${v > 0 && v < 0.01 ? v.toFixed(4) : v.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`
}

const fmtPct = v => (v > 0 && v < 0.01 ? '<1%' : `${Math.round(v * 100)}%`)
const fmtCount = n => (Number(n) || 0).toLocaleString()

function fmtPeriod(period, hourly) {
  const d = new Date(hourly ? `${period}:00` : `${period}T12:00:00`)

  if (Number.isNaN(d.getTime())) {
    return period
  }

  return hourly
    ? d.toLocaleTimeString(undefined, { hour: 'numeric' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function fmtWait(ms) {
  const mins = Math.max(0, Math.ceil(ms / 60_000))
  const d = Math.floor(mins / 1440)
  const hh = Math.floor((mins % 1440) / 60)
  const m = mins % 60

  if (d > 0) {
    return hh ? `${d}d ${hh}h` : `${d}d`
  }

  if (hh > 0) {
    return m ? `${hh}h ${m}m` : `${hh}h`
  }

  return `${mins}m`
}

// ─── data ───────────────────────────────────────────────────────────────────
async function api(req) {
  const bridge = window.hermesDesktop?.api

  if (typeof bridge !== 'function') {
    throw new Error('Hermes desktop bridge unavailable')
  }

  return bridge(req)
}

function usageRest(path, opts = {}) {
  return ctxRef?.rest
    ? ctxRef.rest(path, { timeoutMs: 90_000, ...opts })
    : api({ path: `${API_BASE}${path}`, timeoutMs: 90_000, ...opts })
}

// ─── icons (lucide glyphs, same as T3 Code) ─────────────────────────────────
const ICONS = {
  refresh: ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
  // T3's /usage-limits pace glyphs (lucide).
  gauge: ['M3.34 19a10 10 0 1 1 17.32 0', 'm12 14 4-4'],
  trendUp: ['M22 7l-8.5 8.5-5-5L2 17', 'M16 7h6v6'],
  trendDown: ['M22 17l-8.5-8.5-5 5L2 7', 'M16 17h6v-6']
}

function Icon({ name, size = 14, className, style }) {
  const parts = ICONS[name] || []

  return h(
    'svg',
    {
      'aria-hidden': true,
      className: cn('t3u-icon', className),
      fill: 'none',
      height: size,
      stroke: 'currentColor',
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      strokeWidth: 2,
      style,
      viewBox: '0 0 24 24',
      width: size
    },
    parts.map((d, i) => h('path', { d, key: i }))
  )
}

// T3 Code's provider marks (apps/web Icons.tsx, provider-muse client glyph).
const ANTIGRAVITY_ICON = 'data:image/png;base64,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'
const MUSE_GLYPH = 'M107.9999847,0.0000013c-12.3391113,0-21.984787,9.2935991-30.7164001,21.0993118 C65.2847366,5.821527,55.2499847,0,43.2413788,0C18.7586193,0,0,31.8620682,0,65.5862122 C0,86.6896515,10.2095623,100,27.3103466,100c12.3080502,0,21.1600361-5.8025894,36.8965454-33.3103409 c0,0,6.5597382-11.5841255,11.0725021-19.563858c1.5813522,2.5531845,3.2431107,5.3001404,4.9964676,8.2535095 l7.379303,12.4137955C102.0298843,91.8478851,110.039032,100,124.5517273,100 c16.6595764,0,25.9310303-13.4923325,25.9310303-35.0344849 C150.4827576,29.6551723,131.301178,0.0000013,107.9999847,0.0000013z M52.2068977,59.2413788 c-12.7586212,20-17.1724167,24.4827576-24.2758656,24.4827576c-7.3103428,0-11.6551704-6.4178314-11.6551704-17.8620682 c0-24.4827576,12.2068977-49.5172424,26.7586231-49.5172424c7.880127,0,14.4653816,4.5510426,24.5522308,18.9913158 C58.0087967,50.0272293,52.2068977,59.2413788,52.2068977,59.2413788z M100.3597336,56.7235756L91.5367355,42.00877 c-2.3876724-3.8832092-4.6757278-7.451458-6.8838348-10.7230263 c7.951973-12.273531,14.5113449-18.3891907,22.3126144-18.3891907c16.2069016,0,29.1724243,23.8620682,29.1724243,53.1724091 c0,11.1724167-3.6593475,17.6551743-11.2413864,17.6551743 C117.6294937,83.7241364,114.1578674,78.924736,100.3597336,56.7235756z'

function ProviderMark({ provider, size = 16 }) {
  if (provider === 'antigravity') {
    return h('svg', { 'aria-hidden': true, className: 't3u-icon', height: size, viewBox: '0 0 128 128', width: size },
      h('image', { height: 128, href: ANTIGRAVITY_ICON, width: 128 }))
  }

  if (provider === 'muse') {
    return h('svg', { 'aria-hidden': true, className: 't3u-icon', height: size, viewBox: '0 0 150.4828 100', width: size },
      h('path', { d: MUSE_GLYPH, fill: 'currentColor', fillRule: 'evenodd' }))
  }

  if (provider === 'zai') {
    return h(
      'svg',
      { 'aria-hidden': true, className: 't3u-icon', height: size, viewBox: '0 0 24 24', width: size },
      h('rect', { fill: '#3d6df2', height: 20, rx: 5, width: 20, x: 2, y: 2 }),
      h('path', { d: 'M7.5 8h9l-9 8h9', fill: 'none', stroke: 'white', strokeLinecap: 'round', strokeLinejoin: 'round', strokeWidth: 2.2 })
    )
  }

  if (provider === 'claude') {
    // Claude's burst mark.
    return h(
      'svg',
      { 'aria-hidden': true, className: 't3u-icon', height: size, viewBox: '0 0 24 24', width: size },
      h(
        'g',
        { stroke: '#d97757', strokeLinecap: 'round', strokeWidth: 2.4 },
        [0, 30, 60, 90, 120, 150].map(a =>
          h('line', { key: a, transform: `rotate(${a} 12 12)`, x1: 12, x2: 12, y1: 2.5, y2: 21.5 })
        )
      )
    )
  }

  // Codex: OpenAI-style knot, drawn as three rotated rounded links.
  return h(
    'svg',
    { 'aria-hidden': true, className: 't3u-icon', height: size, viewBox: '0 0 24 24', width: size },
    h(
      'g',
      { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8 },
      [0, 60, 120].map(a => h('rect', { height: 16, key: a, rx: 6, transform: `rotate(${a} 12 12)`, width: 8, x: 8, y: 4 }))
    )
  )
}

function UsageSegmented({ value, options, onChange, label, disabled }) {
  return h(
    'div',
    { 'aria-disabled': disabled || undefined, 'aria-label': label, className: 't3u-seg', role: 'group' },
    options.map(o =>
      h(
        'button',
        {
          'aria-pressed': value === o.id,
          className: 't3u-seg-btn',
          'data-on': value === o.id ? '' : undefined,
          disabled,
          key: o.id,
          onClick: () => onChange(o.id),
          type: 'button'
        },
        o.label
      )
    )
  )
}

function UsageChart({ series, providers, metric, hourly }) {
  const [hover, setHover] = useState(null)
  const values = series.map(d => (metric === 'cost' ? d.cost : d.tokens))
  const peak = Math.max(...values, 0)
  const fmt = metric === 'cost' ? fmtUsd : fmtTokens
  const hovered = hover === null ? null : series[hover]

  return h(
    'div',
    { className: 't3u-chart' },
    h('div', { className: 't3u-chart-axis' }, (peak > 0 ? [peak, peak / 2, 0] : [0]).map((t, i) => h('span', { key: i }, fmt(t)))),
    h(
      'div',
      { className: 't3u-chart-plot', onMouseLeave: () => setHover(null) },
      h('div', { className: 't3u-chart-grid' }, h('span'), h('span'), h('span')),
      h(
        'div',
        { className: 't3u-chart-bars' },
        series.map((d, i) =>
          h(
            'div',
            { className: 't3u-bar', 'data-hover': hover === i ? '' : undefined, key: d.period, onMouseEnter: () => setHover(i) },
            h(
              'div',
              { className: 't3u-bar-stack', style: { height: peak > 0 ? `${(values[i] / peak) * 100}%` : 0 } },
              providers.map(p => {
                const v = d.byProvider[p]
                const part = v ? (metric === 'cost' ? v.cost : v.tokens) : 0

                return part > 0 ? h('div', { key: p, style: { background: PROVIDERS[p].color, flexGrow: part } }) : null
              })
            )
          )
        )
      ),
      hovered
        ? h(
            'div',
            {
              className: 't3u-tip',
              style:
                hover > series.length / 2
                  ? { right: `${((series.length - hover) / series.length) * 100}%` }
                  : { left: `${((hover + 1) / series.length) * 100}%` }
            },
            h('div', { className: 't3u-tip-head' }, h('span', null, fmtPeriod(hovered.period, hourly)), h('b', null, fmt(metric === 'cost' ? hovered.cost : hovered.tokens))),
            providers
              .filter(p => hovered.byProvider[p])
              .map(p =>
                h(
                  'div',
                  { className: 't3u-tip-row', key: p },
                  h('span', { className: 't3u-dot', style: { background: PROVIDERS[p].color } }),
                  h('span', { style: { flex: 1 } }, PROVIDERS[p].label),
                  h('span', null, fmt(metric === 'cost' ? hovered.byProvider[p].cost : hovered.byProvider[p].tokens))
                )
              )
          )
        : null,
      h(
        'div',
        { className: 't3u-chart-x' },
        h('span', null, series[0] ? fmtPeriod(series[0].period, hourly) : ''),
        h('span', null, series.length ? fmtPeriod(series[series.length - 1].period, hourly) : '')
      )
    )
  )
}

function ShareBar({ label, segments, format, aside }) {
  const total = segments.reduce((a, s) => a + s.value, 0)

  return h(
    'div',
    { className: 't3u-share' },
    h('div', { className: 't3u-share-head' }, h('span', null, label), h('span', null, aside || format(total))),
    h('div', { className: 't3u-share-bar' }, segments.map(s => (s.value > 0 ? h('div', { key: s.label, style: { background: s.color, flexGrow: s.value } }) : null))),
    h(
      'div',
      { className: 't3u-share-legend' },
      segments.map(s =>
        h(
          'span',
          { key: s.label },
          h('span', { className: 't3u-dot', style: { background: s.color } }),
          s.label,
          h('b', null, format(s.value)),
          h('i', null, total > 0 ? fmtPct(s.value / total) : '0%')
        )
      )
    )
  )
}

function Metric({ label, value }) {
  return h('div', { className: 't3u-metric' }, h('span', null, label), h('b', null, value))
}

// ─── T3 Code's /usage-limits (packages/shared usageLimits maths, verbatim) ──
// Subscription windows read as "% left"; the bar's fill is quota left and the
// hairline is how far into the window the clock is — where even spending would
// have put the fill. Pace compares the two.
function limitRemaining(w) {
  return Math.round(100 - Math.max(0, Math.min(100, w.usedPercent ?? 0)))
}

function limitElapsed(w, now) {
  const at = w.resetsAt ? Date.parse(w.resetsAt) : NaN
  const length = (w.windowDurationMins || 0) * 60_000
  if (!Number.isFinite(at) || length <= 0) return null
  return Math.max(0, Math.min(1, (length - (at - now)) / length))
}

function limitPace(w, now) {
  const elapsed = limitElapsed(w, now)
  if (elapsed === null) return null
  const gap = (w.usedPercent ?? 0) - elapsed * 100
  return gap > 5 ? 'ahead' : gap < -5 ? 'under' : 'on'
}

function limitResetsIn(w, now) {
  const at = w.resetsAt ? Date.parse(w.resetsAt) : NaN
  if (!Number.isFinite(at)) return w.idle ? 'starts with your next request' : null
  if (at <= now) return 'resets now'
  return `resets in ${fmtWait(at - now)}`
}

const PACE_ICON = { ahead: 'trendUp', on: 'gauge', under: 'trendDown' }
const PACE_TITLE = {
  ahead: 'Ahead of pace: spending faster than the window elapses',
  on: 'On pace with the window',
  under: 'Under pace: headroom left for the rest of the window'
}

function WindowBar({ color, w, now }) {
  const remaining = limitRemaining(w)
  const elapsed = limitElapsed(w, now)
  const timeLeft = elapsed === null ? null : Math.round((1 - elapsed) * 100)
  const resetsAt = w.resetsAt ? new Date(Date.parse(w.resetsAt)).toLocaleString(undefined, { day: 'numeric', hour: 'numeric', minute: '2-digit' }) : null

  return h(
    Tip,
    {
      label: `${remaining}% left${timeLeft !== null ? ` · ${timeLeft}% of the window left` : ''}${resetsAt ? ` · resets ${resetsAt}` : ''} — the line is where even spending would be`
    },
    h(
      'div',
      { className: 't3u-wbar' },
      remaining > 0 ? h('div', { style: { background: color, width: `${remaining}%` } }) : null,
      timeLeft !== null ? h('span', { className: 't3u-wbar-mark', style: { left: `${timeLeft}%` } }) : null
    )
  )
}

// One account's windows as rows: label and % left, bar, pace and countdown.
function LimitRows({ color, windows, now }) {
  return h(
    'div',
    { className: 't3u-lw' },
    windows.map(w => {
      const pace = limitPace(w, now)
      const resetsIn = limitResetsIn(w, now)

      return h(
        React.Fragment,
        { key: w.id },
        h(
          'span',
          { className: 't3u-lw-label' },
          h('span', { className: 't3u-lw-name' }, w.label),
          h('b', null, `${limitRemaining(w)}% left`)
        ),
        h(WindowBar, { color, now, w }),
        h(
          'span',
          { className: 't3u-lw-side' },
          pace ? h(Tip, { label: PACE_TITLE[pace] }, h(Icon, { className: `t3u-pace t3u-pace-${pace}`, name: PACE_ICON[pace], size: 13 })) : null,
          resetsIn ? h('span', null, resetsIn) : null
        )
      )
    })
  )
}

function LimitsView({ data, error, loading }) {
  const now = useValue($tick)

  if (loading && !data) {
    return h('div', { className: 't3u-empty' }, 'Asking Claude Code, Codex, Z.ai, Antigravity and Muse Code for their limits…')
  }

  if (!data) {
    return h(
      'div',
      { className: 't3u-empty' },
      /404|not found/i.test(error?.message || '')
        ? 'The usage backend is not loaded yet — switch to Tokens to restart it.'
        : `Could not read limits${error?.message ? `: ${error.message}` : ''}`
    )
  }

  return h(
    'div',
    { className: 't3u-limits' },
    data.providers.map(p => {
      const meta = PROVIDERS[p.provider] || { label: p.provider }
      const title = [meta.label, p.plan ? p.plan.charAt(0).toUpperCase() + p.plan.slice(1) : null].filter(Boolean).join(' · ')

      return h(
        'section',
        { className: 't3u-limit-card', key: p.account_id || p.provider },
        h(
          'div',
          { className: 't3u-limit-head' },
          h(ProviderMark, { provider: p.provider }),
          h('h2', null, p.account_label ? `${title} · ${p.account_label}` : title),
          p.account ? h('span', { className: 't3u-muted t3u-trunc' }, p.account) : null
        ),
        p.observedAt ? h('p', { className: 't3u-muted', style: { margin: 0 } }, `As of ${new Date(p.observedAt).toLocaleString(undefined, { day: 'numeric', hour: 'numeric', minute: '2-digit', month: 'short' })} (reported during Muse Code turns)`) : null,
        p.status !== 'ok'
          ? h('p', { className: 't3u-muted', style: { margin: 0 } }, p.message || 'Limits unavailable.')
          : h(
              React.Fragment,
              null,
              p.windows.length === 0 ? h('p', { className: 't3u-muted', style: { margin: 0 } }, 'No limit windows reported.') : null,
              h(LimitRows, {
                color: PROVIDERS[p.provider]?.color || 'var(--ui-accent)',
                now,
                windows: [...p.windows].sort((x, y) => (WINDOW_KIND_ORDER[x.kind] ?? 9) - (WINDOW_KIND_ORDER[y.kind] ?? 9))
              }),
              p.limitReached ? h('p', { className: 't3u-note' }, `Limit reached: ${String(p.limitReached).replace(/_/g, ' ')}`) : null,
              p.resetCredits && p.resetCredits.availableCount > 0
                ? h(
                    'p',
                    { className: 't3u-muted', style: { margin: 0 } },
                    `${p.resetCredits.availableCount} free limit ${p.resetCredits.availableCount === 1 ? 'reset' : 'resets'} available`,
                    p.resetCredits.nextExpiresAt ? ` · next expires ${new Date(p.resetCredits.nextExpiresAt).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}` : ''
                  )
                : null,
              p.extraUsage
                ? h(
                    'p',
                    { className: 't3u-muted', style: { margin: 0 } },
                    p.extraUsage.enabled
                      ? `Extra usage on · ${fmtUsd((p.extraUsage.usedCredits || 0) / 100)} of ${fmtUsd((p.extraUsage.monthlyLimit || 0) / 100)} this month`
                      : 'Extra usage off'
                  )
                : null
            )
      )
    }),
    h('p', { className: 't3u-muted', style: { gridColumn: '1 / -1', margin: 0 } }, `Checked ${new Date(data.checkedAt).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} · Claude Code and Codex from their CLIs, Z.ai from its Coding Plan quota API, Antigravity from Google's quota API, Muse Code from its last turn`)
  )
}

function UsagePage({ grafted = false }) {
  const prefs = useValue($usagePrefs)
  const { metric, days, breakdown } = prefs
  const showingLimits = metric === 'limits'
  const hourly = days === 1

  const usage = useQuery({
    enabled: !showingLimits,
    queryFn: () => usageRest(`/usage?days=${days}`),
    queryKey: [NS, 'cli-usage', days],
    refetchInterval: 60_000,
    staleTime: 20_000
  })

  const limits = useQuery({
    enabled: showingLimits,
    queryFn: () => usageRest('/limits'),
    queryKey: [NS, 'cli-limits'],
    refetchInterval: 120_000,
    staleTime: 30_000
  })

  const refreshing = showingLimits ? limits.isFetching : usage.isFetching
  const refresh = () => (showingLimits ? usageRest('/limits?refresh=true').then(d => queryClient.setQueryData([NS, 'cli-limits'], d)) : usage.refetch())
  const data = usage.data
  const fmt = metric === 'cost' ? fmtUsd : fmtTokens

  const windowLabel =
    data && !showingLimits
      ? hourly
        ? `${new Date(data.since).toLocaleString(undefined, { day: 'numeric', hour: 'numeric', month: 'short' })} to ${new Date(data.until).toLocaleString(undefined, { day: 'numeric', hour: 'numeric', month: 'short' })}`
        : `${fmtPeriod(data.series[0]?.period, false)} to ${fmtPeriod(data.series[data.series.length - 1]?.period, false)}`
      : null

  const controls = h(
    'div',
    { className: 't3u-controls' },
    h('span', { className: 't3u-window' }, windowLabel),
    h(UsageSegmented, { label: 'Usage metric', onChange: v => setUsagePrefs({ metric: v }), options: USAGE_METRICS, value: metric }),
    h(UsageSegmented, {
      disabled: showingLimits,
      label: 'Usage period',
      onChange: v => setUsagePrefs({ days: Number(v) }),
      options: USAGE_WINDOWS.map(w => ({ id: String(w.days), label: w.label })),
      value: String(days)
    }),
    h(
      Tip,
      { label: showingLimits ? 'Refresh limits' : 'Refresh usage' },
      h(
        'button',
        { 'aria-busy': refreshing, 'aria-label': 'Refresh', className: 't3u-iconbtn', disabled: refreshing, onClick: () => void refresh(), type: 'button' },
        h(Icon, { className: refreshing ? 't3u-spin' : undefined, name: 'refresh', size: 15 })
      )
    )
  )

  let body

  if (showingLimits) {
    body = h(LimitsView, { data: limits.data, error: limits.error, loading: limits.isLoading })
  } else if (usage.isLoading || (!data && !usage.error)) {
    body = h('div', { className: 't3u-empty' }, 'Reading Claude Code, Codex, Antigravity and Muse Code history…')
  } else if (!data) {
    const msg = usage.error?.message || ''
    const missing = /404|not found/i.test(msg)
    body = h(
      'div',
      { className: 't3u-empty', style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 } },
      missing
        ? 'The usage backend is installed but not loaded yet. Hermes loads it on its next backend restart.'
        : `Could not read usage${msg ? `: ${msg}` : ''}`,
      missing
        ? h(
            Button,
            {
              onClick: () =>
                host
                  .restartGateway()
                  .then(() => ctxRef?.setTimeout(() => queryClient.invalidateQueries({ queryKey: [NS] }), 4000))
                  .catch(err => host.notifyError(err, 'Could not restart the backend')),
              size: 'sm',
              variant: 'outline'
            },
            'Restart backend'
          )
        : h(Button, { onClick: () => usage.refetch(), size: 'sm', variant: 'outline' }, 'Retry')
    )
  } else {
    const { totals, models, series } = data
    const active = PROVIDER_ORDER.filter(p => (data.providers[p]?.tokens || 0) > 0 || (data.providers[p]?.cost || 0) > 0)
    const modelPeak = Math.max(0, ...models.map(m => (metric === 'cost' ? m.cost : m.tokens)))
    const modelTotal = models.reduce((a, m) => a + (metric === 'cost' ? m.cost : m.tokens), 0)
    const ranked = [...models].sort((a, b) => (metric === 'cost' ? b.cost - a.cost : b.tokens - a.tokens))
    const periods = [...series].reverse().filter(d => d.tokens > 0 || d.cost > 0)
    const unpricedShare = totals.tokens > 0 ? totals.unpricedTokens / totals.tokens : 0

    body = h(
      React.Fragment,
      null,
      h(
        'section',
        { className: 't3u-top' },
        h(
          'div',
          { className: 't3u-summary' },
          h(
            'div',
            { className: 't3u-big' },
            h('span', null, metric === 'cost' ? fmtUsd(totals.cost) : fmtTokens(totals.tokens)),
            h(
              'small',
              null,
              `${fmtCount(totals.sessions)} sessions`,
              metric === 'cost' ? ' · API estimate' : '',
              metric === 'cost' && unpricedShare > 0
                ? h(Tip, { label: `API estimate excludes ${fmtPct(unpricedShare)} unpriced records.` }, h('span', { className: 't3u-info' }, ' ⓘ'))
                : null
            )
          ),
          PROVIDER_ORDER.map(pv => {
            const p = data.providers[pv]

            if (!p) {
              return null
            }

            const share = metric === 'cost' ? (totals.cost ? p.cost / totals.cost : 0) : totals.tokens ? p.tokens / totals.tokens : 0
            const via = p.via || {}

            return h(
              'div',
              { className: 't3u-prow', key: pv },
              h(
                'div',
                { className: 't3u-prow-head' },
                h(
                  'span',
                  { className: 't3u-prow-name' },
                  h('span', { className: 't3u-dot', style: { background: PROVIDERS[pv].color } }),
                  h(ProviderMark, { provider: pv }),
                  h('span', null, PROVIDERS[pv].label),
                  h('small', null, `${fmtCount(p.sessions)} ${p.sessions === 1 ? 'session' : 'sessions'}`)
                ),
                h('b', null, fmt(metric === 'cost' ? p.cost : p.tokens))
              ),
              h(
                'small',
                { className: 't3u-muted' },
                metric === 'cost' ? `${fmtPct(share)} of cost · ${fmtTokens(p.tokens)} tokens` : `${fmtPct(share)} of tokens · ${fmtUsd(p.cost)}`
              ),
              via.hermes
                ? h('small', { className: 't3u-muted' }, `${fmtTokens(via.hermes)} via Hermes · ${fmtTokens(via.cli || 0)} via the CLI`)
                : null,
              p.accounts && p.accounts.length > 0
                ? h('div', { className: 't3u-muted', style: { display: 'flex', gap: 16, marginTop: 2 } },
                    p.accounts.map(acc =>
                      h('span', { key: acc.id },
                        `${acc.label}: `,
                        h('b', { style: { fontVariantNumeric: 'tabular-nums', color: 'var(--ui-text-secondary)' } },
                          fmt(metric === 'cost' ? acc.cost : acc.tokens)))))
                : null
            )
          })
        ),
        h(
          'div',
          { className: 't3u-chartcol' },
          h('h2', null, `${hourly ? 'Hourly' : 'Daily'} ${metric === 'tokens' ? 'processed tokens' : 'cost'}`),
          h(UsageChart, { hourly, metric, providers: active, series })
        )
      ),
      h(
        'section',
        { className: 't3u-section' },
        h('h2', null, 'Totals'),
        h(
          'div',
          { className: 't3u-metrics t3u-metrics-5' },
          h(Metric, { label: 'Processed tokens', value: fmtTokens(totals.tokens) }),
          h(Metric, { label: 'Cached input', value: fmtTokens(totals.cached) }),
          h(Metric, { label: 'Uncached input', value: fmtTokens(totals.uncached + totals.cacheWrite) }),
          h(Metric, { label: 'Output', value: fmtTokens(totals.output) }),
          h(Metric, { label: 'Cache savings', value: fmtUsd(totals.cacheSavings) })
        )
      ),
      totals.tokens > 0
        ? h(
            'section',
            { className: 't3u-shares' },
            metric === 'tokens'
              ? h(ShareBar, {
                  format: fmtTokens,
                  label: 'Tokens by type',
                  segments: [
                    { color: 'var(--ui-cyan)', label: 'Cached input', value: totals.cached },
                    { color: 'var(--ui-blue)', label: 'Uncached input', value: totals.uncached },
                    { color: 'var(--ui-purple)', label: 'Cache write', value: totals.cacheWrite },
                    { color: 'var(--ui-green)', label: 'Output', value: totals.output - totals.reasoning },
                    { color: 'var(--ui-yellow)', label: 'Reasoning', value: totals.reasoning }
                  ].filter(s => s.value > 0)
                })
              : h(ShareBar, {
                  format: fmtUsd,
                  label: 'Cost by type',
                  segments: [
                    { color: 'var(--ui-cyan)', label: 'Cached input', value: totals.categoryCost.cached },
                    { color: 'var(--ui-blue)', label: 'Uncached input', value: totals.categoryCost.uncached },
                    { color: 'var(--ui-purple)', label: 'Cache write', value: totals.categoryCost.cacheWrite },
                    { color: 'var(--ui-green)', label: 'Output', value: totals.categoryCost.output }
                  ].filter(s => s.value > 0)
                }),
            metric === 'cost' && totals.speedCost.fast + totals.speedCost.ultrafast > 0
              ? h(ShareBar, {
                  aside: `${fmtUsd(totals.speedPremium)} premium`,
                  format: fmtUsd,
                  label: 'Cost by speed',
                  segments: [
                    { color: 'var(--ui-text-tertiary)', label: 'Standard', value: totals.speedCost.standard },
                    { color: 'var(--ui-orange)', label: 'Fast', value: totals.speedCost.fast },
                    { color: 'var(--ui-red)', label: 'Ultrafast', value: totals.speedCost.ultrafast }
                  ].filter(s => s.value > 0)
                })
              : h(ShareBar, {
                  format: metric === 'cost' ? fmtUsd : fmtTokens,
                  label: metric === 'cost' ? 'Cost by provider' : 'Tokens by provider',
                  segments: active.map(p => ({ color: PROVIDERS[p].color, label: PROVIDERS[p].label, value: metric === 'cost' ? data.providers[p].cost : data.providers[p].tokens }))
                })
          )
        : null,
      h(
        'section',
        { className: 't3u-section' },
        h(
          'div',
          { className: 't3u-section-head' },
          h('h2', null, 'Breakdown'),
          h(UsageSegmented, {
            label: 'Usage breakdown',
            onChange: v => setUsagePrefs({ breakdown: v }),
            options: [
              { id: 'model', label: 'Model' },
              { id: 'time', label: hourly ? 'Hour' : 'Day' }
            ],
            value: breakdown === 'model' ? 'model' : 'time'
          })
        ),
        breakdown === 'model'
          ? h(
              'table',
              { className: 't3u-table' },
              h('thead', null, h('tr', null, h('th', { className: 'l' }, '#'), h('th', { className: 'l w' }, 'Model'), h('th', null, 'Cost'), h('th', null, 'Share'), h('th', null, 'Tokens'))),
              h(
                'tbody',
                null,
                ranked.length === 0
                  ? h('tr', null, h('td', { className: 'empty', colSpan: 5 }, 'No activity in this window.'))
                  : ranked.map((m, i) => {
                      const v = metric === 'cost' ? m.cost : m.tokens

                      return h(
                        'tr',
                        { key: `${m.provider}:${m.model}` },
                        h('td', { className: 'l dim' }, i + 1),
                        h(
                          'td',
                          { className: 'l' },
                          h('div', { className: 't3u-model' }, h(ProviderMark, { provider: m.provider, size: 14 }), m.model),
                          h('div', { className: 't3u-model-bar' }, h('div', { style: { background: PROVIDERS[m.provider]?.color, width: v > 0 && modelPeak > 0 ? `max(0.5rem, ${(v / modelPeak) * 100}%)` : 0 } }))
                        ),
                        h('td', { className: 'fg' }, m.priced ? fmtUsd(m.cost) : h('span', { className: 'dim' }, 'Unpriced')),
                        h('td', null, modelTotal > 0 && (metric !== 'cost' || m.priced) ? fmtPct(v / modelTotal) : ''),
                        h('td', null, fmtTokens(m.tokens))
                      )
                    })
              )
            )
          : h(
              'table',
              { className: 't3u-table' },
              h(
                'thead',
                null,
                h('tr', null, h('th', { className: 'l' }, hourly ? 'Hour' : 'Day'), active.map(p => h('th', { key: p }, PROVIDERS[p].label)), h('th', null, 'Total'), h('th', null, 'Tokens'))
              ),
              h(
                'tbody',
                null,
                periods.length === 0
                  ? h('tr', null, h('td', { className: 'empty', colSpan: active.length + 3 }, 'No activity in this window.'))
                  : periods.map(d =>
                      h(
                        'tr',
                        { key: d.period },
                        h('td', { className: 'l fg' }, fmtPeriod(d.period, hourly)),
                        active.map(p => h('td', { key: p }, fmtUsd(d.byProvider[p]?.cost || 0))),
                        h('td', { className: 'fg' }, fmtUsd(d.cost)),
                        h('td', null, fmtTokens(d.tokens))
                      )
                    )
              )
            )
      )
    )
  }

  return h('div', { className: grafted ? 't3u-root' : 't3u-page t3u-root' }, h('style', null, CSS), controls, h('div', { className: 't3u-body' }, body))
}

// ─── styles (scoped; theme variables only) ──────────────────────────────────
const CSS = `
.t3u-page{width:100%;box-sizing:border-box;max-width:1360px;margin:0 auto;padding:18px 28px 12px}
.t3u-root{height:100%;min-height:0;display:flex;flex-direction:column;font-size:13px;color:var(--ui-text-primary)}
.t3u-icon{flex-shrink:0;display:block}
.t3u-iconbtn{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;color:var(--ui-text-tertiary);cursor:pointer;flex-shrink:0}
.t3u-iconbtn:hover{background:var(--ui-row-hover-background);color:var(--ui-text-primary)}
.t3u-iconbtn:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.t3u-trunc{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@keyframes t3u-rotate{to{transform:rotate(360deg)}}
.t3u-spin{animation:t3u-rotate 3s linear infinite}
@media (prefers-reduced-motion:reduce){.t3u-spin{animation:none}}
.t3u-controls{display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:2px 0 12px;flex-wrap:wrap}
.t3u-window{margin-right:auto;font-size:12px;color:var(--ui-text-tertiary)}
.t3u-seg{display:inline-grid;grid-auto-flow:column;gap:2px;padding:2px;border-radius:6px;background:var(--ui-bg-tertiary,var(--ui-row-hover-background))}
.t3u-seg-btn{all:unset;padding:3px 10px;border-radius:4px;font-size:12px;color:var(--ui-text-tertiary);cursor:pointer;text-align:center}
.t3u-seg-btn:hover{color:var(--ui-text-primary)}
.t3u-seg-btn[data-on]{background:var(--ui-row-active-background);color:var(--ui-text-primary);font-weight:500;box-shadow:0 1px 2px color-mix(in srgb,black 12%,transparent)}
.t3u-body{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:32px;padding-bottom:24px}
.t3u-empty{padding:48px 0;text-align:center;color:var(--ui-text-tertiary)}
.t3u-note{margin:0;font-size:12px;color:var(--ui-yellow)}
.t3u-muted{color:var(--ui-text-tertiary);font-size:12px}
.t3u-top{display:grid;gap:24px;grid-template-columns:minmax(0,18rem) minmax(0,1fr)}
@container (max-width:760px){.t3u-top{grid-template-columns:1fr}}
@media (max-width:1000px){.t3u-top{grid-template-columns:1fr}}
.t3u-summary{display:flex;flex-direction:column;gap:18px;min-width:0}
.t3u-big{display:flex;flex-direction:column;gap:4px}
.t3u-big span{font-size:36px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.t3u-big small{font-size:12px;color:var(--ui-text-tertiary)}
.t3u-prow{display:flex;flex-direction:column;gap:3px}
.t3u-prow-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px}
.t3u-prow-head b{font-weight:500;font-variant-numeric:tabular-nums}
.t3u-prow-name{display:flex;align-items:center;gap:8px;min-width:0}
.t3u-prow-name small{font-size:11px;color:var(--ui-text-tertiary);white-space:nowrap}
.t3u-dot{display:inline-block;width:8px;height:8px;border-radius:999px;flex-shrink:0}
.t3u-chartcol{display:flex;flex-direction:column;gap:12px;min-width:0}
.t3u-root h2{margin:0;font-size:13px;font-weight:500;color:var(--ui-text-primary)}
.t3u-chart{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;height:220px}
.t3u-chart-axis{display:flex;flex-direction:column;justify-content:space-between;padding-bottom:20px;font-size:10px;color:var(--ui-text-quaternary);text-align:right;font-variant-numeric:tabular-nums}
.t3u-chart-plot{position:relative;min-width:0;display:flex;flex-direction:column}
.t3u-chart-grid{position:absolute;inset:0 0 20px 0;display:flex;flex-direction:column;justify-content:space-between;pointer-events:none}
.t3u-chart-grid span{height:1px;background:var(--ui-stroke-tertiary)}
.t3u-chart-bars{position:relative;flex:1;display:flex;align-items:flex-end;gap:2px}
.t3u-bar{flex:1;min-width:0;height:100%;display:flex;align-items:flex-end;border-radius:2px}
.t3u-bar[data-hover]{background:var(--ui-row-hover-background)}
.t3u-bar-stack{width:100%;display:flex;flex-direction:column-reverse;border-radius:2px 2px 0 0;overflow:hidden;min-height:0;transition:height .4s ease-out}
.t3u-bar-stack div{min-height:1px;opacity:.85}
.t3u-bar[data-hover] .t3u-bar-stack div{opacity:1}
.t3u-chart-x{display:flex;justify-content:space-between;height:20px;align-items:flex-end;font-size:10px;color:var(--ui-text-quaternary)}
.t3u-tip{position:absolute;top:0;z-index:5;min-width:160px;max-width:240px;padding:8px 10px;border-radius:8px;border:1px solid var(--ui-stroke-secondary);background:var(--dt-popover,var(--ui-sidebar-surface-background));box-shadow:0 6px 24px color-mix(in srgb,black 18%,transparent);pointer-events:none;display:flex;flex-direction:column;gap:4px;font-size:12px}
.t3u-tip-head{display:flex;justify-content:space-between;gap:12px;color:var(--ui-text-secondary)}
.t3u-tip-head b{color:var(--ui-text-primary);font-weight:600;font-variant-numeric:tabular-nums}
.t3u-tip-row{display:flex;align-items:center;gap:6px;color:var(--ui-text-secondary);font-variant-numeric:tabular-nums}
.t3u-section{display:flex;flex-direction:column;gap:10px}
.t3u-section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.t3u-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px 24px}
.t3u-metric{display:flex;flex-direction:column;gap:2px;min-width:0}
.t3u-metric span{font-size:12px;color:var(--ui-text-tertiary)}
.t3u-metric b{font-size:16px;font-weight:500;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.t3u-shares{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:32px 48px}
.t3u-share{display:flex;flex-direction:column;gap:8px;min-width:0}
.t3u-share-head{display:flex;justify-content:space-between;font-size:13px;font-weight:500}
.t3u-share-head span:last-child{font-variant-numeric:tabular-nums;color:var(--ui-text-secondary);font-weight:400}
.t3u-share-bar{display:flex;gap:2px;height:8px;border-radius:999px;overflow:hidden;background:var(--ui-row-hover-background)}
.t3u-share-bar div{min-width:2px}
.t3u-share-legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--ui-text-secondary)}
.t3u-share-legend > span{display:inline-flex;align-items:center;gap:6px}
.t3u-share-legend b{font-weight:500;color:var(--ui-text-primary);font-variant-numeric:tabular-nums}
.t3u-share-legend i{font-style:normal;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.t3u-table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
.t3u-table th{padding:8px 0 8px 24px;font-size:12px;font-weight:400;color:var(--ui-text-tertiary);text-align:right;border-bottom:1px solid var(--ui-stroke-secondary);white-space:nowrap}
.t3u-table td{padding:9px 0 9px 24px;text-align:right;color:var(--ui-text-tertiary);border-bottom:1px solid var(--ui-stroke-tertiary);white-space:nowrap}
.t3u-table .l{text-align:left;padding-left:0;padding-right:12px}
.t3u-table .w{width:100%}
.t3u-table td.l:nth-child(2){white-space:normal}
.t3u-table .fg{color:var(--ui-text-primary)}
.t3u-table .dim{color:var(--ui-text-quaternary)}
.t3u-table td.empty{text-align:center;padding:24px 0}
.t3u-table tbody tr:hover td{background:var(--ui-row-hover-background)}
.t3u-model{display:flex;align-items:baseline;gap:8px;color:var(--ui-text-primary);font-family:var(--font-mono,ui-monospace),monospace;font-size:12px}
.t3u-model small{font-family:var(--font-sans,inherit);font-size:11px;color:var(--ui-text-quaternary)}
.t3u-model-bar{margin-top:6px;height:2px;max-width:12rem}
.t3u-model-bar div{height:100%;border-radius:999px;background:var(--ui-accent)}
.t3u-info{cursor:help;color:var(--ui-text-tertiary)}
.t3u-seg[aria-disabled]{opacity:.45;pointer-events:none}
.t3u-metrics-5{grid-template-columns:repeat(5,minmax(0,1fr))}
.t3u-model{display:flex;align-items:center;gap:8px}
.t3u-limits{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:24px}
.t3u-limit-card{display:flex;flex-direction:column;gap:16px;padding:16px;border:1px solid var(--ui-stroke-tertiary);border-radius:10px}
.t3u-limit-head{display:flex;align-items:center;gap:8px;min-width:0}
.t3u-limit-head .t3u-muted{margin-left:auto}
.t3u-window-row{display:flex;flex-direction:column;gap:6px}
.t3u-window-head,.t3u-window-foot{display:flex;justify-content:space-between;gap:12px;font-size:13px}
.t3u-window-foot b{font-weight:500;font-variant-numeric:tabular-nums}
.t3u-window-details{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--ui-text-tertiary)}
.t3u-window-details span{display:inline-flex;gap:6px}
.t3u-window-details b{font-weight:500;color:var(--ui-text-secondary);font-variant-numeric:tabular-nums}
.t3u-window-bar{height:6px;border-radius:999px;overflow:hidden;background:var(--ui-row-hover-background)}
.t3u-window-bar div{height:100%;border-radius:999px;transition:width .5s ease-out}
.t3u-lw{display:grid;grid-template-columns:minmax(0,10rem) minmax(3rem,1fr) auto;gap:4px 14px;align-items:center}
.t3u-lw-label{display:flex;align-items:baseline;gap:8px;min-width:0;font-size:12px}
.t3u-lw-name{color:var(--ui-text-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t3u-lw-label b{margin-left:auto;font-weight:500;color:var(--ui-text-primary);font-variant-numeric:tabular-nums;white-space:nowrap}
.t3u-wbar{position:relative;display:block;width:100%;height:14px;border-radius:999px;background:var(--ui-row-hover-background);overflow:hidden;cursor:default}
.t3u-wbar > div{position:absolute;top:2px;bottom:2px;left:0;border-radius:999px;transition:width .5s ease-out}
.t3u-wbar-mark{position:absolute;top:1px;bottom:1px;width:1px;background:color-mix(in srgb,var(--ui-text-primary) 60%,transparent)}
.t3u-lw-side{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--ui-text-tertiary);white-space:nowrap;font-variant-numeric:tabular-nums}
.t3u-pace{color:var(--ui-text-tertiary)}
.t3u-pace-ahead{color:var(--ui-red)}
.t3u-pace-under{color:var(--ui-green)}
`

// Available before UsagePage mounts so the stock panel never flashes.
const GRAFT_CSS = `[data-t3u-active] > :not([data-t3u-host]){display:none !important}
[data-t3u-hide]{display:none !important}
[data-t3u-host]{display:flex;flex-direction:column;flex:1;min-height:0}
`

// ─── Command Center graft ───────────────────────────────────────────────────
// Command Center → Usage is open: hash route /command-center with ?section=usage
// and the overlay nav carrying the usage row. The SDK has no slot inside the
// overlay, so a hidden "home" in the status bar portals the page into that tab.
//
// The status bar is the only plugin area that stays mounted under an overlay:
// the titlebar unmounts its slots while ANY overlay is open, and opening one
// unmounts the chat composer too. The status bar itself unmounts when hidden
// (Simple mode rests it hidden); with no home, Command Center → Usage falls back
// to this feature's own page instead of leaving the stock panel up.
const isCommandCenterUsage = () => {
  const hash = window.location.hash || ''

  return hash.startsWith('#/command-center') && /[?&]section=usage\b/.test(hash)
}

function findUsageTarget() {
  if (!isCommandCenterUsage()) {
    return null
  }

  const nav = document.querySelector('[data-tour="overlay-nav"]')
  const layout = nav?.parentElement

  if (!layout) {
    return null
  }

  const main = layout.querySelector(':scope > main') || layout.querySelector('main')

  if (!main || main.children.length < 2) {
    return null
  }

  return { content: main.lastElementChild, header: main.firstElementChild }
}

// What ReactDOM.createPortal returns, built by hand: plugins only get `react`,
// and the host's own reconciler mounts this like any portal.
const REACT_PORTAL = Symbol.for('react.portal')
const portalKeys = new WeakMap()
let portalSeq = 0

function portal(children, container) {
  if (!portalKeys.has(container)) {
    portalKeys.set(container, `t3u-${++portalSeq}`)
  }

  return { $$typeof: REACT_PORTAL, children, containerInfo: container, implementation: null, key: portalKeys.get(container) }
}

// Renders graft failures into the tab instead of dying silently.
class GraftBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return h(
        'div',
        { style: { color: 'var(--ui-red)', fontSize: 12, padding: 16 } },
        `T3 Code · Usage: ${String(this.state.error?.message || this.state.error)}`
      )
    }

    return this.props.children
  }
}

function UsageGraft() {
  const [target, setTarget] = useState(null)

  useLayoutEffect(() => {
    let current = null
    let hidden = null

    const release = () => {
      current?.content.removeAttribute('data-t3u-active')
      hidden?.removeAttribute('data-t3u-hide')
      current = null
      hidden = null
    }

    const tick = () => {
      const next = findUsageTarget()

      if (next?.content !== current?.content) {
        release()
        current = next
        setTarget(next)
      }

      if (!current) {
        return
      }

      // Hides the stock panel before paint; the portal lands right after.
      current.content.setAttribute('data-t3u-active', '')

      // The stock 7/30/90 switch in the breadcrumb row: ours replaces it.
      const seg = current.header?.querySelector('button[aria-pressed]')?.parentElement

      if (seg && seg !== hidden) {
        hidden?.removeAttribute('data-t3u-hide')
        hidden = seg
        seg.setAttribute('data-t3u-hide', '')
      }
    }

    // React's overlay commit is observed in a microtask, before browser paint.
    // Battery: a subtree observer on <body> fires on every streamed token, so it
    // only runs while the Usage page is open. The Navigation API reports
    // pushState navigation too (no hashchange); without it, observe always.
    const observer = new MutationObserver(tick)
    const nav = window.navigation
    let observing = false

    const sync = () => {
      const want = !nav || isCommandCenterUsage()

      if (want && !observing) {
        observer.observe(document.body, { childList: true, subtree: true })
      } else if (!want && observing) {
        observer.disconnect()
      }

      observing = want
      tick()
    }

    sync()
    nav?.addEventListener('currententrychange', sync)
    window.addEventListener('hashchange', sync)
    window.addEventListener('popstate', sync)

    return () => {
      observer.disconnect()
      nav?.removeEventListener('currententrychange', sync)
      window.removeEventListener('hashchange', sync)
      window.removeEventListener('popstate', sync)
      release()
    }
  }, [])

  return target
    ? portal(h('div', { 'data-t3u-host': '' }, h(GraftBoundary, null, h(UsagePage, { grafted: true }))), target.content)
    : null
}

// Every mounted home registers; the first one is the leader and grafts (two
// can overlap briefly while a hot reload swaps the old mount for the new).
let graftHomes = []
let $graftLeader = null
let fallbackPending = false

const syncGraftLeader = () => $graftLeader?.set(graftHomes[0] ?? null)

// Command Center → Usage with no home to graft from: open our own page instead
// of leaving the stock panel up. `wait` gives homes still mounting (app boot,
// the status bar toggling) a grace period.
function scheduleUsageFallback(wait = 300) {
  if (fallbackPending || !ctxRef || !isCommandCenterUsage()) {
    return
  }

  const fallback = () => {
    fallbackPending = false

    if (!$graftLeader?.get() && isCommandCenterUsage()) {
      window.location.replace(`#${ROUTE}`)
    }
  }

  if (!wait) {
    return fallback()
  }

  fallbackPending = true
  ctxRef.setTimeout(fallback, wait)
}

const openUsage = () => host.navigate($graftLeader?.get() ? CC_USAGE : ROUTE)

function UsageGraftHome() {
  const [id] = useState(() => ({}))
  const leader = useValue($graftLeader)

  useLayoutEffect(() => {
    graftHomes.push(id)
    syncGraftLeader()

    return () => {
      graftHomes = graftHomes.filter(home => home !== id)
      syncGraftLeader()

      if (!graftHomes.length) {
        scheduleUsageFallback()
      }
    }
  }, [id])

  // <style> is display:none, so the home adds nothing to its host row.
  return h(React.Fragment, null, h('style', null, GRAFT_CSS), leader === id ? h(UsageGraft, null) : null)
}

// ─── registration ───────────────────────────────────────────────────────────
export default {
  id: 'usage',
  name: 'Usage',
  register(ctx) {
    ctxRef = ctx
    $usagePrefs = atom({ metric: 'tokens', days: 30, breakdown: 'model', ...ctx.storage.get('usagePrefs', {}) })
    if (![1, 7, 30, 90].includes($usagePrefs.get().days)) $usagePrefs.set({ ...$usagePrefs.get(), days: 30 })
    // Coarse clock for the limits view's "resets in …" counters.
    $tick = atom(Date.now())
    ctx.setInterval(() => $tick.set(Date.now()), 15_000)
    graftHomes = []
    $graftLeader = atom(null)
    ctx.addEventListener(window, 'hashchange', () => !$graftLeader.get() && scheduleUsageFallback(0))
    scheduleUsageFallback()

    ctx.registerMany([
      {
        // Command Center → Usage graft homes (see the graft note above).
        id: 'cc-graft',
        area: (STATUSBAR_AREAS && STATUSBAR_AREAS.right) || 'statusBar.right',
        order: 9999,
        render: () => h(UsageGraftHome, null)
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => h(UsagePage, null)
      },
      {
        // Titlebar shortcut left of the Settings gear (T3 Code's usage button spot).
        // Rendered INSIDE the right cluster via its slot, not `titleBar.tools.right`:
        // the pane header only reserves window-drag-free space for that measured
        // cluster, so a separate tools cluster sits on a drag region and eats clicks.
        // `order: -1` moves it before the gear in the cluster's flex row.
        id: 'titlebar',
        area: (TITLEBAR_AREAS && TITLEBAR_AREAS.right) || 'titleBar.right',
        render: () =>
          h(
            Tip,
            { label: 'Usage', placement: 'toolbar' },
            h(
              Button,
              {
                'aria-label': 'Usage',
                className: 'bg-transparent select-none text-muted-foreground/85 hover:bg-(--ui-control-hover-background) hover:text-foreground',
                onClick: openUsage,
                onPointerDown: e => e.stopPropagation(),
                size: 'icon-titlebar',
                style: { order: -1 },
                type: 'button',
                variant: 'ghost'
              },
              h('i', { 'aria-hidden': true, className: 'codicon codicon-graph leading-none', style: { fontSize: '13.9px' } })
            )
          )
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${NS}.open`,
          label: 'Open Usage',
          keywords: ['usage', 'tokens', 'cost', 'spend', 'limits', 'analytics'],
          run: openUsage
        }
      }
    ])
  }
}
