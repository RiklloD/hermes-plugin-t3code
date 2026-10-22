/**
 * T3 Code · Inbox — the T3 Code thread sidebar, rebuilt for Hermes sessions.
 *
 * A T3 Code feature module: the shell (shared/desktop/shell.js) hands register() a ctx scoped
 * to this feature (ids, storage, REST under /inbox, error isolation). Bundled into
 * desktop/plugin.js by scripts/build.mjs — edit this file, then rebuild.
 *
 * Mirrors T3 Code's sidebar (apps/web/src/components/Sidebar.tsx):
 *   - header row: Search · new session; a selection bar while rows are selected
 *   - Pinned cards (no header), then the Inbox (cards, latest return first)
 *   - shelves pinned to the bottom: Working (cards, by your last send),
 *     Snoozed (slim, by wake time), Settled (slim, paged into older history)
 *   - card rows: profile · status/time (hover → Snooze ⏰ / Settle ✓), title, meta line
 *   - slim rows: dimmed profile mark · title · time (hover → Un-settle / Wake)
 *   - snooze presets with "raise hand" early wakes (a question, an approval, a
 *     fresh failure or a finished run) and a Woke pill
 *   - ⌘/Ctrl-click selects, ⇧-click selects a range; bulk settle / snooze /
 *     pin / read; dragging a selected row drags the whole selection
 *   - search: titles and metadata locally, transcript text through Hermes'
 *     full-text index, ↑/↓ + Enter without leaving the input
 *   - context menu, double-click rename, middle-click opens in a new tab
 *   - drag a row (or the whole selection) onto the chat composer to mention
 *     the thread there (`@session:` chip); typing @ in the composer also
 *     offers threads from the roster
 *
 * Recovery: threads cut off by a crash resume on launch, and usage-limited
 * threads resume as soon as the limit lifts (see "recovery" below).
 *
 * Status follows T3's priority: Approval / Input → Working → Background
 * (delegated children still running) → Failed / Limited → Woke → Done.
 *
 * Settle / snooze / Woke acknowledgement are lifecycle records keyed by the
 * full owner (connection · profile · durable id), cached in this feature's
 * storage and shared through this feature's backend (features/inbox/api.py) (newest write wins), so
 * every window on the same Hermes agrees. Settle refuses live work, like T3's
 * server. Pins, unread, rename and archive write the same stores/endpoints
 * the core sidebar writes.
 */
import * as sdk from '@hermes/plugin-sdk'
import React from 'react'

const {
  host,
  atom,
  useValue,
  useQuery,
  queryClient,
  cn,
  profileColor,
  profileColorSoft,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Button,
  Input,
  Tip,
  ToggleRow,
  ListRow,
  SegmentedControl,
  COMPOSER_AREAS
} = sdk

const h = React.createElement
const { memo, useCallback, useEffect, useMemo, useRef, useState } = React

// T3 Code feature identity. NS keys palette commands and queries; PANE_ID is the id the
// T3 Code shell gives the pane contribution ('<plugin>:<feature>-<id>').
const NS = 't3code.inbox'
const API_BASE = '/api/plugins/t3code/inbox'
const PANE_ID = 't3code:inbox-pane'
const DRAG_MIME = 'application/x-t3-inbox-sessions'
const SETTLED_TAIL_PAGE_COUNT = 25
// Roster page size; "Show more" past the last fetched settled row loads the next page.
const HISTORY_PAGE = 400
const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR
// A source whose live poll has failed for this long shows its last known
// status as stale instead of pretending it is still current (or idle).
const LIVE_STALE_MS = 12_000
// A pin we just wrote wins over a roster page fetched before the write landed.
const PIN_OVERLAY_MS = 30_000
const CORE_PINS_KEY = 'hermes.desktop.pinnedSessions'
const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '')

// ─── lucide icons (same glyphs T3 Code uses) ────────────────────────────────
const ICONS = {
  search: ['circle:11,11,8', 'M21 21l-4.3-4.3'],
  squarePen: [
    'M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7',
    'M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z'
  ],
  x: ['M18 6 6 18', 'm6 6 12 12'],
  check: ['M20 6 9 17l-5-5'],
  clock: ['circle:12,12,10', 'M12 6v6l4 2'],
  alarm: ['circle:12,13,8', 'M12 9v4l2 2', 'M5 3 2 6', 'm22 6-3-3', 'M6.38 18.7 4 21', 'M17.64 18.67 20 21'],
  alarmOff: [
    'M6.87 6.87a8 8 0 1 0 11.26 11.26',
    'M19.9 14.25a8 8 0 0 0-9.15-9.15',
    'm22 6-3-3',
    'M6.26 18.67 4 21',
    'm2 2 20 20',
    'M4 4 2 6'
  ],
  undo: ['M9 14 4 9l5-5', 'M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11'],
  pin: [
    'M12 17v5',
    'M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z'
  ],
  pinOff: [
    'M12 17v5',
    'M15 9.34V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H7.89',
    'm2 2 20 20',
    'M9 9v1.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h11'
  ],
  circleDashed: [
    'M10.1 2.182a10 10 0 0 1 3.8 0',
    'M13.9 21.818a10 10 0 0 1-3.8 0',
    'M17.609 3.721a10 10 0 0 1 2.69 2.7',
    'M2.182 13.9a10 10 0 0 1 0-3.8',
    'M20.279 17.609a10 10 0 0 1-2.7 2.69',
    'M21.818 10.1a10 10 0 0 1 0 3.8',
    'M3.721 6.391a10 10 0 0 1 2.7-2.69',
    'M6.391 20.279a10 10 0 0 1-2.69-2.7'
  ],
  circleCheck: ['circle:12,12,10', 'm9 12 2 2 4-4'],
  circleAlert: ['circle:12,12,10', 'M12 8v4', 'M12 16h.01'],
  circleX: ['circle:12,12,10', 'm15 9-6 6', 'm9 9 6 6'],
  gauge: ['m12 14 4-4', 'M3.34 19a10 10 0 1 1 17.32 0'],
  square: ['M6 6h12v12H6z'],
  shield: [
    'M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z',
    'M12 8v4',
    'M12 16h.01'
  ],
  messageQuestion: [
    'M7.9 20A9 9 0 1 0 4 16.1L2 22Z',
    'M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3',
    'M12 17h.01'
  ],
  cloudOff: [
    'm2 2 20 20',
    'M5.782 5.782A7 7 0 0 0 9 19h8.5a4.5 4.5 0 0 0 1.307-.193',
    'M21.532 16.5A4.5 4.5 0 0 0 17.5 10h-1.79A7.008 7.008 0 0 0 10 5.07'
  ],
  chevronDown: ['m6 9 6 6 6-6'],
  plus: ['M5 12h14', 'M12 5v14'],
  users: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', 'circle:9,7,4', 'M22 21v-2a4 4 0 0 0-3-3.87', 'M16 3.13a4 4 0 0 1 0 7.75'],
  branch: ['M6 3v12', 'circle:18,6,3', 'circle:6,18,3', 'M18 9a9 9 0 0 1-9 9'],
  refresh: ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
  folder: ['M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z']
}

function Icon({ name, size = 14, className, style }) {
  const parts = ICONS[name] || []

  return h(
    'svg',
    {
      'aria-hidden': true,
      className: cn('t3i-icon', className),
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
    parts.map((d, i) => {
      if (d.startsWith('circle:')) {
        const [cx, cy, r] = d.slice(7).split(',')

        return h('circle', { cx, cy, key: i, r })
      }

      return h('path', { d, key: i })
    })
  )
}

// ─── styles (scoped; theme variables only) ──────────────────────────────────
const CSS = `
.t3i-root{display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px;color:var(--ui-text-primary);
  --t3i-hover:var(--ui-row-hover-background);--t3i-active:var(--ui-row-active-background);
  --t3i-sel:var(--ui-control-active-background,var(--ui-row-active-background));
  --t3i-secondary:var(--ui-text-secondary);--t3i-tertiary:var(--ui-text-tertiary);--t3i-quaternary:var(--ui-text-quaternary);
  --t3i-info:var(--ui-blue);--t3i-warn:var(--ui-yellow);--t3i-ok:var(--ui-green);--t3i-err:var(--ui-red);--t3i-input:var(--ui-purple)}
.t3i-icon{flex-shrink:0;display:block}
.t3i-header{display:flex;align-items:center;gap:4px;padding:8px 8px 4px}
.t3i-search{display:flex;align-items:center;gap:8px;height:32px;min-width:0;flex:1;padding:0 8px;border-radius:6px;color:var(--t3i-tertiary)}
.t3i-search:hover,.t3i-search:focus-within{background:var(--t3i-hover);color:var(--ui-text-primary)}
.t3i-search input{all:unset;flex:1;min-width:0;font-size:13px;font-weight:500;color:var(--ui-text-primary)}
.t3i-search input::placeholder{color:var(--t3i-tertiary)}
.t3i-iconbtn{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;color:var(--t3i-tertiary);cursor:pointer;flex-shrink:0}
.t3i-iconbtn:hover{background:var(--t3i-hover);color:var(--ui-text-primary)}
.t3i-iconbtn:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.t3i-iconbtn[data-active]{color:var(--ui-text-primary)}
.t3i-micro{all:unset;display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:4px;color:var(--t3i-tertiary);cursor:pointer}
.t3i-micro:hover{color:var(--ui-text-primary)}
.t3i-scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:2px 8px 8px}
.t3i-list{display:flex;flex-direction:column;min-height:100%;margin:0;padding:0;list-style:none}
.t3i-li{list-style:none}
.t3i-card-li{padding:2px 0}
.t3i-row{position:relative;width:100%;box-sizing:border-box;border-radius:6px;cursor:pointer;user-select:none;outline:none;overflow:hidden;color:var(--ui-text-primary)}
.t3i-row:hover{background:var(--t3i-hover)}
.t3i-row:focus-visible{box-shadow:inset 0 0 0 2px var(--ui-accent)}
.t3i-row[data-active]{background:var(--t3i-active)}
.t3i-row[data-selected]{background:var(--t3i-sel);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-accent) 45%,transparent)}
.t3i-row[data-highlight]{background:var(--t3i-hover);box-shadow:inset 0 0 0 1px var(--ui-stroke-secondary)}
.t3i-row[data-recede]{color:var(--t3i-secondary)}
.t3i-row[data-recede][data-working]{opacity:.7;transition:opacity .15s}
.t3i-row[data-recede][data-working]:hover{opacity:1}
.t3i-row[data-dragging]{opacity:.45}
.t3i-row[data-drop-before]::before{content:'';position:absolute;left:4px;right:4px;top:0;height:2px;border-radius:2px;background:var(--ui-accent)}
.t3i-card{height:78px;box-sizing:border-box;padding:8px 10px}
.t3i-line1{display:flex;align-items:center;gap:6px;height:20px;min-width:0}
.t3i-proj{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:500;color:var(--t3i-secondary)}
.t3i-row[data-recede] .t3i-proj{font-weight:400}
.t3i-title{display:block;min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:500;color:color-mix(in srgb,var(--ui-text-primary) 92%,transparent)}
.t3i-row[data-recede] .t3i-title{font-weight:400;color:var(--t3i-secondary)}
.t3i-row[data-attn] .t3i-title{color:var(--ui-text-primary)}
.t3i-line2{display:flex;margin-top:4px;min-width:0}
.t3i-line3{display:flex;align-items:center;gap:6px;margin-top:2px;min-width:0;font-size:12px;color:var(--t3i-secondary)}
.t3i-dim{color:var(--t3i-quaternary)}
.t3i-trunc{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t3i-avatar{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:4px;font-size:9px;font-weight:700;flex-shrink:0;text-transform:uppercase;line-height:1}
.t3i-slot{position:relative;display:flex;align-items:stretch;justify-content:flex-end;height:20px;min-width:32px;margin-left:auto;flex-shrink:0;font-size:12px}
.t3i-status{display:flex;align-items:center;gap:4px;font-variant-numeric:tabular-nums;color:var(--t3i-secondary);transition:opacity .12s;white-space:nowrap}
.t3i-status b{font-weight:500}
.t3i-status[data-stale]{opacity:.55}
.t3i-actions{position:absolute;inset:0 0 0 auto;display:flex;align-items:stretch;opacity:0;pointer-events:none;transition:opacity .12s}
.t3i-row:hover .t3i-actions,.t3i-row:focus-within .t3i-actions,.t3i-actions[data-open]{position:static;opacity:1;pointer-events:auto}
.t3i-row:hover .t3i-status:not([data-keep]),.t3i-row:focus-within .t3i-status:not([data-keep]),.t3i-slot[data-menu] .t3i-status:not([data-keep]){position:absolute;right:0;opacity:0;pointer-events:none}
.t3i-act{all:unset;display:inline-flex;align-items:center;gap:4px;padding:0 6px;border-radius:6px;font-size:12px;color:var(--t3i-tertiary);cursor:pointer;white-space:nowrap}
.t3i-act:hover{color:var(--ui-text-primary)}
.t3i-act:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.t3i-act[aria-disabled=true]{opacity:.45;cursor:default}
.t3i-c-info{color:var(--t3i-info)}.t3i-c-warn{color:var(--t3i-warn)}.t3i-c-ok{color:var(--t3i-ok)}.t3i-c-err{color:var(--t3i-err)}.t3i-c-input{color:var(--t3i-input)}
.t3i-woke{all:unset;display:inline-flex;align-items:center;gap:4px;font-weight:500;color:var(--t3i-warn);cursor:pointer}
.t3i-woke:hover{text-decoration:underline}
.t3i-slim{display:flex;align-items:center;gap:10px;height:36px;padding:0 10px}
.t3i-slim .t3i-title{font-weight:500;color:color-mix(in srgb,var(--t3i-secondary) 75%,transparent)}
.t3i-slim[data-unread] .t3i-title{color:var(--t3i-secondary)}
.t3i-slim:hover .t3i-title,.t3i-slim[data-active] .t3i-title,.t3i-slim[data-attn] .t3i-title,.t3i-slim[data-highlight] .t3i-title{color:var(--ui-text-primary)}
.t3i-slim .t3i-avatar{opacity:.4;filter:grayscale(1);transition:opacity .12s,filter .12s}
.t3i-slim:hover .t3i-avatar,.t3i-slim[data-active] .t3i-avatar,.t3i-slim[data-highlight] .t3i-avatar{opacity:1;filter:none}
.t3i-slim-slot{position:relative;display:flex;align-items:center;justify-content:flex-end;height:24px;min-width:32px;margin-left:auto;flex-shrink:0}
.t3i-slim-time{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--t3i-secondary);font-variant-numeric:tabular-nums;transition:opacity .12s}
.t3i-slim:hover .t3i-slim-time:not([data-keep]){opacity:0}
.t3i-slim-act{position:absolute;inset:0 -4px 0 auto;opacity:0;pointer-events:none}
.t3i-slim:hover .t3i-slim-act,.t3i-slim:focus-within .t3i-slim-act{opacity:1;pointer-events:auto}
.t3i-slim-act[data-static]{position:static}
.t3i-hit{display:flex;flex-direction:column;justify-content:center;gap:2px;min-height:44px;padding:5px 10px;box-sizing:border-box}
.t3i-hit-top{display:flex;align-items:center;gap:10px;min-width:0}
.t3i-snippet{padding-left:26px;font-size:12px;color:var(--t3i-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t3i-snippet mark{background:color-mix(in srgb,var(--t3i-warn) 28%,transparent);color:var(--ui-text-primary);border-radius:2px;padding:0 1px}
.t3i-search-note{display:flex;align-items:center;gap:6px;height:24px;padding:0 10px;font-size:11px;color:var(--t3i-quaternary)}
.t3i-shelf{display:flex;align-items:center;gap:8px;width:100%;height:32px;padding:0 8px;box-sizing:border-box;border:0;border-radius:6px;background:none;cursor:pointer;font:inherit;font-size:12px;font-weight:500;color:var(--t3i-tertiary);text-align:left}
.t3i-shelf:hover{color:var(--t3i-secondary)}
.t3i-shelf[data-tone=info]{color:color-mix(in srgb,var(--t3i-info) 80%,var(--t3i-tertiary))}
.t3i-shelf[data-tone=accent]{color:var(--ui-accent)}
.t3i-draft-rule{height:1px;margin:8px 10px;background:color-mix(in srgb,var(--t3i-secondary) 35%,transparent)}
.t3i-shelf[data-tone=emph]{color:var(--t3i-secondary)}
.t3i-shelf-line{flex:1;min-width:8px;height:1px;background:var(--ui-stroke-tertiary)}
.t3i-shelf[data-tone=accent] .t3i-shelf-line{background:color-mix(in srgb,var(--ui-accent) 50%,transparent)}
.t3i-shelf .t3i-caret{transition:transform .15s}
.t3i-shelf[aria-expanded=true] .t3i-caret{transform:rotate(180deg)}
.t3i-push{margin-top:auto}
.t3i-boundary{display:flex;align-items:center;gap:8px;height:16px;margin:4px 8px;font-size:12px;font-weight:500;color:var(--t3i-secondary)}
.t3i-boundary span:last-child{flex:1;height:1px;background:var(--ui-stroke-tertiary)}
.t3i-boundary[data-target]{color:var(--ui-accent)}
.t3i-boundary[data-target] span:last-child{background:color-mix(in srgb,var(--ui-accent) 50%,transparent)}
.t3i-hint{display:flex;align-items:center;justify-content:center;height:36px;margin:2px;border:1px dashed var(--ui-stroke-secondary);border-radius:6px;font-size:12px;color:var(--t3i-secondary)}
.t3i-hint[data-target]{border-color:color-mix(in srgb,var(--ui-accent) 50%,transparent);background:color-mix(in srgb,var(--ui-accent) 6%,transparent);color:var(--ui-accent)}
.t3i-more{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:10px;width:100%;height:36px;padding:0 10px;border-radius:6px;font-size:13px;color:var(--t3i-quaternary);cursor:pointer}
.t3i-more:hover{background:var(--t3i-hover);color:var(--ui-text-primary)}
.t3i-more[aria-busy=true]{cursor:progress}
.t3i-empty{display:flex;flex-direction:column;align-items:center;gap:8px;padding:24px 8px;text-align:center;font-size:12px;color:var(--t3i-quaternary)}
.t3i-rename{flex:1;min-width:0;font:inherit;font-size:13px;font-weight:500;padding:0 4px;border-radius:4px;border:1px solid var(--ui-stroke-primary);background:var(--ui-sidebar-surface-background,transparent);color:var(--ui-text-primary);outline:none}
.t3i-scope-row{display:flex;align-items:center;gap:8px;min-width:0}
.t3i-shortcut{margin-left:auto;padding-left:16px;font-size:11px;color:var(--t3i-tertiary);font-variant-numeric:tabular-nums}
.t3i-src{font-size:10px;padding:0 4px;border-radius:3px;background:var(--ui-row-hover-background);color:var(--t3i-tertiary);text-transform:lowercase;flex-shrink:0}
.t3i-bulk{display:flex;align-items:center;gap:2px;height:30px;margin:0 8px 4px;padding:0 4px 0 10px;border-radius:6px;background:var(--t3i-sel);font-size:12px}
.t3i-bulk-count{flex:1;min-width:0;font-weight:500;color:var(--ui-text-primary);white-space:nowrap}
.t3i-offline{display:flex;align-items:center;gap:6px;margin:0 8px 4px;padding:4px 10px;border-radius:6px;font-size:11px;color:var(--t3i-warn);background:color-mix(in srgb,var(--t3i-warn) 8%,transparent)}
@keyframes t3i-spin{to{transform:rotate(360deg)}}
.t3i-spin{animation:t3i-spin 3s linear infinite}
@media (prefers-reduced-motion:reduce){.t3i-spin{animation:none}}
`

// ─── time helpers ───────────────────────────────────────────────────────────
function compactAgo(ms, now) {
  const d = Math.max(0, now - ms)

  if (d < MINUTE) {
    return 'now'
  }

  if (d < HOUR) {
    return `${Math.floor(d / MINUTE)}m`
  }

  if (d < DAY) {
    return `${Math.floor(d / HOUR)}h`
  }

  if (d < 7 * DAY) {
    return `${Math.floor(d / DAY)}d`
  }

  if (d < 30 * DAY) {
    return `${Math.floor(d / (7 * DAY))}w`
  }

  if (d < 365 * DAY) {
    return `${Math.floor(d / (30 * DAY))}mo`
  }

  return `${Math.floor(d / (365 * DAY))}y`
}

function workingDuration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000))

  if (s < 60) {
    return `${s}s`
  }

  const m = Math.floor(s / 60)

  if (m < 60) {
    return `${m}m ${String(s % 60).padStart(2, '0')}s`
  }

  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

function wakeLabel(until, now) {
  const r = until - now

  if (r <= 0) {
    return 'now'
  }

  if (r < HOUR) {
    return `${Math.max(1, Math.ceil(r / MINUTE))}m`
  }

  if (r < DAY) {
    return `${Math.ceil(r / HOUR)}h`
  }

  return `${Math.ceil(r / DAY)}d`
}

const timeOfDay = d => d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function atHour(base, hour, addDays = 0) {
  const d = new Date(base)
  d.setDate(d.getDate() + addDays)
  d.setHours(hour, 0, 0, 0)

  return d
}

// Same presets and collapsing rules as T3's resolveSnoozePresets.
function snoozePresets(now = new Date()) {
  const inHour = new Date(now.getTime() + HOUR)
  const in3 = new Date(now.getTime() + 3 * HOUR)
  const out = [
    { id: 'hour', label: 'In 1 hour', when: timeOfDay(inHour), until: inHour.getTime() },
    { id: 'three-hours', label: 'In 3 hours', when: timeOfDay(in3), until: in3.getTime() }
  ]
  const evening = atHour(now, 18)

  if (evening.getTime() - now.getTime() > HOUR) {
    out.push({ id: 'evening', label: 'This evening', when: timeOfDay(evening), until: evening.getTime() })
  }

  const tomorrow = atHour(now, 9, 1)
  out.push({ id: 'tomorrow', label: 'Tomorrow', when: timeOfDay(tomorrow), until: tomorrow.getTime() })
  const toMonday = (1 - now.getDay() + 7) % 7 || 7
  const nextWeek = atHour(now, 9, toMonday)

  if (nextWeek.getTime() !== tomorrow.getTime()) {
    out.push({
      id: 'next-week',
      label: 'Next week',
      when: `${nextWeek.toLocaleDateString(undefined, { weekday: 'short' })} ${timeOfDay(nextWeek)}`,
      until: nextWeek.getTime()
    })
  }

  return out
}

function wakeDescription(until, now = Date.now()) {
  const wake = new Date(until)
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const days = Math.floor((until - start.getTime()) / DAY)
  const t = timeOfDay(wake)

  if (days === 0) {
    return t
  }

  if (days === 1) {
    return `tomorrow ${t}`
  }

  if (days < 7) {
    return `${wake.toLocaleDateString(undefined, { weekday: 'short' })} ${t}`
  }

  return `${wake.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${t}`
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

// ─── plugin state (reset on every register) ─────────────────────────────────
const DEFAULT_PREFS = {
  workingShelf: true,
  autoSettleDays: 3,
  includeCron: false,
  syncLifecycle: true,
  resumeAfterCrash: true,
  autoResumeLimited: true,
  snoozeLimited: false,
  expanded: { working: true, snoozed: false, settled: false },
  revealed: false
}

let ctxRef = null
let $prefs = atom(DEFAULT_PREFS)
// Lifecycle records, keyed by row key (`<connection>|<profile>|<durable id>`):
//   t            ms of the last write (newest write wins when syncing)
//   settledAt    settled by hand; settledCount / settledLast = message count
//                and activity at that moment — new messages un-settle it
//   unsettledAt  explicitly kept in the inbox (beats auto-settle)
//   snoozeUntil / snoozeAt
//   visitedAt    Woke / failure acknowledgement (set once an open succeeds)
//   returnedAt   last observed return to you (work finished, a question
//                arrived, woken, un-settled) — the Inbox sort key
//   sentAt       last observed send (fallback when the backend has none)
// `legacy` holds v1 bare-id state until rows arrive to give it an owner.
let $life = atom({ v: 2, records: {}, legacy: null })
// Live turn state per source owner (`<connection>|<profile>`), see pollLive.
let $live = atom({ owners: {} })
// Coarse clock: relative ages, snooze expiry, auto-settle. Working timers tick on their own.
let $tick = atom(Date.now())
let $pinsVersion = atom(0)
let $customSnooze = atom(null) // { rows } while the custom dialog is open
let $newPicker = atom(false)
let $drafts = atom({}) // fresh-chat draft key → { text, profile, at }
let $freshKey = atom(null) // the composer's current new-chat draft key
let $sessionDrafts = atom('') // '\n'-joined session ids with unsent composer text
let $selection = atom({ keys: [], anchor: null })
let $historyLimit = atom(HISTORY_PAGE)
let $sync = atom({ state: 'local', at: 0, error: null }) // 'local' | 'synced' | 'unavailable' | 'error'
let workingSince = new Map() // key → ms the row was first seen working
let pinOverlay = new Map() // key → { pinned, at } for pins written but not yet in a roster page
let rowCache = new Map() // key → last derived row, reused while unchanged (memoized rows)
let dirtyKeys = new Set() // lifecycle records not yet pushed to the backend
let openedProfiles = new Set() // profiles opened from here: their sockets exist, so they can be polled
let pendingLookups = new Set()
let lastPinsRaw = ''
let connGeneration = 0
let lastGroups = null // latest classification the pane rendered (card order for park navigation)
let lastVisibleKeys = [] // row keys in on-screen order (range selection)
let syncTimer = null

function setPrefs(patch) {
  const next = { ...$prefs.get(), ...patch }
  $prefs.set(next)
  ctxRef?.storage.set('prefs', next)
}

// ─── identity ───────────────────────────────────────────────────────────────
// A session is only unique within its owner: two profiles (or two
// connections) can hold the same stored id, so every map is keyed by the
// full owner, never the bare id.
const activeConn = () => host.state.connectionId?.get?.() || 'local'
const activeProfile = () => host.state.profile.get() || 'default'
const profileOf = s => s.profile || 'default'
const durableOf = s => s._lineage_root_id || s.id
const connOf = s => s._conn || 'local'
const ownerOf = s => `${connOf(s)}|${profileOf(s)}`
const keyOf = s => `${ownerOf(s)}|${durableOf(s)}`
const sameSession = (a, b) => a.id === b.id && profileOf(a) === profileOf(b)

function sessionIds(s) {
  return [s.id, s._lineage_root_id, ...(s._lineage_ids || [])].filter(Boolean)
}

// ─── lifecycle records ──────────────────────────────────────────────────────
const NO_REC = Object.freeze({})
const recOf = (life, key) => life.records[key] || NO_REC

function setLife(next) {
  $life.set(next)
  ctxRef?.storage.set('life', next)
}

// patches: key → { field: value | null }; null deletes the field.
// Returns key → the record as written (its `t` fences a later Undo).
function writeRecs(patches) {
  const life = $life.get()
  const records = { ...life.records }
  const written = {}
  const now = Date.now()

  for (const [key, patch] of Object.entries(patches)) {
    const prev = records[key] || NO_REC
    const next = { ...prev }

    for (const [field, value] of Object.entries(patch)) {
      if (value === null || value === undefined) {
        delete next[field]
      } else {
        next[field] = value
      }
    }

    next.t = Math.max(now, (prev.t || 0) + 1)
    records[key] = next
    written[key] = next
    dirtyKeys.add(key)
  }

  setLife({ ...life, records })
  scheduleSync()

  return written
}

// Undo: put a record back exactly as it was (a fresh `t` so it syncs).
function restoreRec(key, before) {
  const life = $life.get()
  const prevT = life.records[key]?.t || 0
  const next = { ...before, t: Math.max(Date.now(), prevT + 1) }
  setLife({ ...life, records: { ...life.records, [key]: next } })
  dirtyKeys.add(key)
  scheduleSync()
}

function loadLife(raw) {
  if (raw && raw.v === 2) {
    return { v: 2, records: raw.records || {}, legacy: raw.legacy || null }
  }

  const legacy =
    raw && (raw.settled || raw.snoozed || raw.visited)
      ? { settled: raw.settled || {}, snoozed: raw.snoozed || {}, visited: raw.visited || {} }
      : null

  return { v: 2, records: {}, legacy }
}

// v1 keyed lifecycle by the bare durable id. Give each entry to the rows that
// carry that id (the owner it actually applied to), once.
function migrateLegacy(rows) {
  const life = $life.get()
  const legacy = life.legacy

  if (!legacy) {
    return
  }

  const patches = {}
  const used = new Set()

  for (const s of rows) {
    const bare = durableOf(s)
    const st = legacy.settled[bare]
    const sn = legacy.snoozed[bare]
    const vi = legacy.visited[bare]

    if (!st && !sn && !vi) {
      continue
    }

    used.add(bare)
    const key = keyOf(s)

    if (life.records[key] || patches[key]) {
      continue
    }

    patches[key] = {
      ...(st?.state === 'settled' ? { settledAt: st.at, settledLast: st.at + 15_000 } : {}),
      ...(st?.state === 'active' ? { unsettledAt: st.at } : {}),
      ...(sn ? { snoozeUntil: sn.until, snoozeAt: sn.at } : {}),
      ...(vi ? { visitedAt: vi } : {})
    }
  }

  if (used.size === 0) {
    return
  }

  const prune = map => Object.fromEntries(Object.entries(map).filter(([k]) => !used.has(k)))
  const rest = { settled: prune(legacy.settled), snoozed: prune(legacy.snoozed), visited: prune(legacy.visited) }
  const empty = !Object.keys(rest.settled).length && !Object.keys(rest.snoozed).length && !Object.keys(rest.visited).length

  if (Object.keys(patches).length) {
    writeRecs(patches)
  }

  setLife({ ...$life.get(), legacy: empty ? null : rest })
}

// ─── lifecycle sync (this plugin's backend) ─────────────────────────────────
// Records travel without the connection prefix: the backend they are posted
// to IS that connection. Responses for a connection we have since left are
// dropped (generation fence).
function remoteKeyOf(key, conn) {
  const prefix = `${conn}|`

  return key.startsWith(prefix) ? key.slice(prefix.length) : null
}

function scheduleSync() {
  if (!$prefs.get().syncLifecycle || $sync.get().state === 'unavailable') {
    return
  }

  clearTimeout(syncTimer)
  syncTimer = setTimeout(() => void pushLifecycle(), 800)
}

function adoptRemote(remote, conn, gen) {
  if (gen !== connGeneration || conn !== activeConn() || !remote) {
    return
  }

  const life = $life.get()
  const records = { ...life.records }
  let changed = false

  for (const [rk, rec] of Object.entries(remote)) {
    const key = `${conn}|${rk}`
    const cur = records[key]

    if (!cur || (cur.t || 0) < (rec.t || 0)) {
      records[key] = rec
      changed = true
    } else if ((cur.t || 0) > (rec.t || 0)) {
      dirtyKeys.add(key)
    }
  }

  // Local-only records (first sync after the move to shared storage) go up once.
  for (const key of Object.keys(records)) {
    const rk = remoteKeyOf(key, conn)

    if (rk && !(rk in remote)) {
      dirtyKeys.add(key)
    }
  }

  if (changed) {
    setLife({ ...life, records })
  }
}

function syncFailed(err) {
  const missing = /404|not found/i.test(err?.message || '')
  $sync.set({ at: Date.now(), error: missing ? null : err?.message || 'failed', state: missing ? 'unavailable' : 'error' })
}

async function pullLifecycle() {
  if (!$prefs.get().syncLifecycle) {
    return
  }

  const conn = activeConn()
  const gen = connGeneration

  try {
    const res = await inboxRest('/lifecycle')
    adoptRemote(res?.records || {}, conn, gen)
    $sync.set({ at: Date.now(), error: null, state: 'synced' })

    if (dirtyKeys.size) {
      scheduleSync()
    }
  } catch (err) {
    syncFailed(err)
  }
}

async function pushLifecycle() {
  const conn = activeConn()
  const gen = connGeneration
  const life = $life.get()
  const records = {}
  const sent = new Map()

  for (const key of dirtyKeys) {
    const rk = remoteKeyOf(key, conn)
    const rec = life.records[key]

    if (rk && rec) {
      records[rk] = rec
      sent.set(key, rec.t)
    }
  }

  if (!sent.size) {
    return
  }

  try {
    const res = await inboxRest('/lifecycle', { body: { records }, method: 'POST' })

    for (const [key, t] of sent) {
      if ($life.get().records[key]?.t === t) {
        dirtyKeys.delete(key)
      }
    }

    adoptRemote(res?.records || {}, conn, gen)
    $sync.set({ at: Date.now(), error: null, state: 'synced' })
  } catch (err) {
    syncFailed(err)
  }
}

// ─── pins ───────────────────────────────────────────────────────────────────
// Membership comes from the backend's `pinned` flag (gateway-authoritative,
// what core's pin sync writes), overlaid with our own unconfirmed writes.
// Only the ORDER comes from core's connection-scoped localStorage list; the
// SDK has no accessor for it, so pick the scope whose ids match the backend.
function readPinOrder(pinnedIds) {
  let best = []
  let bestScore = -1
  let raw = ''

  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i)

      if (!k || (k !== CORE_PINS_KEY && !k.startsWith(`${CORE_PINS_KEY}.remote.`))) {
        continue
      }

      const value = window.localStorage.getItem(k) || '[]'
      const list = JSON.parse(value)

      if (!Array.isArray(list)) {
        continue
      }

      const ids = list.filter(x => typeof x === 'string')
      const local = k === CORE_PINS_KEY && activeConn() === 'local'
      const score = ids.filter(id => pinnedIds.has(id)).length + (local ? 0.5 : 0)

      if (score > bestScore) {
        best = ids
        bestScore = score
        raw = value
      }
    }
  } catch {
    // unreadable storage: order falls back to activity
  }

  lastPinsRaw = raw

  return best
}

function pinsChangedElsewhere() {
  try {
    const raw = window.localStorage.getItem(CORE_PINS_KEY) || ''

    if (raw !== lastPinsRaw && activeConn() === 'local') {
      lastPinsRaw = raw
      $pinsVersion.set($pinsVersion.get() + 1)
    }
  } catch {
    // ignore
  }
}

function pinnedFlag(s, key, orderIndex, now) {
  const overlay = pinOverlay.get(key)

  if (overlay && now - overlay.at < PIN_OVERLAY_MS) {
    return overlay.pinned
  }

  if (typeof s.pinned === 'boolean') {
    return s.pinned
  }

  return orderIndex !== undefined
}

// ─── data ───────────────────────────────────────────────────────────────────
// This feature's REST namespace: /api/plugins/t3code/inbox (ctx.rest is already scoped to it).
function inboxRest(path, opts = {}) {
  return ctxRef?.rest
    ? ctxRef.rest(path, { timeoutMs: 90_000, ...opts })
    : api({ path: `${API_BASE}${path}`, timeoutMs: 90_000, ...opts })
}

async function api(req) {
  const bridge = window.hermesDesktop?.api

  if (typeof bridge !== 'function') {
    throw new Error('Hermes desktop bridge unavailable')
  }

  return bridge(req)
}

const sessionsKey = () => [NS, 'sessions', activeConn(), Boolean($prefs.get().includeCron), $historyLimit.get()]

function cachedRows() {
  return queryClient.getQueryData(sessionsKey())?.sessions || []
}

function patchCachedRows(fn) {
  queryClient.setQueryData(sessionsKey(), old => (old?.sessions ? { ...old, sessions: fn(old.sessions) } : old))
}

async function fetchSessionRows(limit, includeCron) {
  const out = []
  const seen = new Set()
  let total = null

  for (let offset = 0; offset < limit; offset += HISTORY_PAGE) {
    const size = Math.min(HISTORY_PAGE, limit - offset)
    const params = new URLSearchParams({
      archived: 'exclude',
      limit: String(size),
      min_messages: '0',
      offset: String(offset),
      order: 'recent',
      profile: 'all'
    })

    if (!includeCron) {
      params.set('exclude_sources', 'cron')
    }

    let res

    try {
      res = await api({ path: `/api/profiles/sessions?${params}`, timeoutMs: 60_000 })
    } catch (err) {
      if (offset === 0 && typeof host.listPersistedSessions === 'function') {
        res = await host.listPersistedSessions(null, { limit: Math.min(500, limit), profile: 'all' })
      } else {
        throw err
      }
    }

    const page = res?.sessions || []
    total = typeof res?.total === 'number' ? res.total : total

    for (const s of page) {
      // Pinned rows are back-filled onto every page: keep the first copy.
      const id = `${profileOf(s)}|${s.id}`

      if (!seen.has(id) && !s.hidden && !s.is_internal_child) {
        seen.add(id)
        out.push(s)
      }
    }

    if (page.length < size) {
      break
    }
  }

  return { sessions: out, total }
}

// Merge the backend's last-turn outcome: { outcome, at, sent_at } (an older
// backend answers a bare 'ok' | 'failed' string).
function withOutcome(s, entry) {
  if (!entry) {
    return s
  }

  if (typeof entry === 'string') {
    return { ...s, outcome: entry }
  }

  return {
    ...s,
    limit_reset_at: entry.limit_reset_at || null,
    outcome: entry.outcome || null,
    outcome_at: entry.at || null,
    sent_at: entry.sent_at || null
  }
}

async function fetchSessions(conn, includeCron, limit) {
  const [roster, outcomes] = await Promise.all([
    fetchSessionRows(limit, includeCron),
    inboxRest(`/outcomes?limit=${Math.min(10_000, limit)}`).catch(() => null)
  ])

  const sessions = roster.sessions.map(s => ({ ...withOutcome(s, outcomes?.[profileOf(s)]?.[s.id]), _conn: conn }))

  // Pins this page confirms no longer need our overlay.
  for (const s of sessions) {
    const o = pinOverlay.get(keyOf(s))

    if (o && typeof s.pinned === 'boolean' && s.pinned === o.pinned) {
      pinOverlay.delete(keyOf(s))
    }
  }

  migrateLegacy(sessions)

  return { sessions, total: roster.total }
}

const refresh = () => queryClient.invalidateQueries({ queryKey: [NS, 'sessions'] })

// Full-text search through Hermes' FTS index, one profile DB at a time
// (`?profile=` makes the primary open that profile's store; no backend spawn).
async function searchTranscripts(q, profiles, includeCron) {
  const settled = await Promise.allSettled(
    profiles.map(profile => {
      const params = new URLSearchParams({ limit: '20', profile, q })

      if (!includeCron) {
        params.set('exclude_sources', 'cron')
      }

      return api({ path: `/api/sessions/search?${params}`, timeoutMs: 20_000 }).then(res =>
        (res?.results || []).map(hit => ({ ...hit, profile: hit.profile || profile }))
      )
    })
  )

  const hits = settled.flatMap(r => (r.status === 'fulfilled' ? r.value : []))
  const failed = settled.filter(r => r.status === 'rejected').length

  return { failed, hits }
}

// ─── live status ────────────────────────────────────────────────────────────
// Per source owner: { statuses: storedId → 'working'|'starting'|'waiting'|'idle',
// runtime: storedId → runtime id, pending: storedId → 'approval'|'input',
// waitingSince: storedId → ms, background: storedId → running children,
// stale: bool }. Only sources we can actually reach are polled: the active
// gateway and the local profiles opened from here (their sockets exist).
const lastLiveOk = new Map()

function rowKeyForLive(owner, storedId) {
  const row = cachedRows().find(s => ownerOf(s) === owner && sessionIds(s).includes(storedId))

  return row ? keyOf(row) : `${owner}|${storedId}`
}

// Working → back to you, and idle → working, observed against a baseline so
// mounting never reshuffles the list.
function recordEdges(owner, prev, next) {
  const now = Date.now()
  const patches = {}
  const ids = new Set([...Object.keys(prev.statuses), ...Object.keys(next.statuses), ...Object.keys(prev.background), ...Object.keys(next.background)])

  for (const id of ids) {
    const was = prev.statuses[id]
    const is = next.statuses[id]
    const wasBusy = BUSY.has(was) || (prev.background[id] || 0) > 0
    const isBusy = BUSY.has(is) || (next.background[id] || 0) > 0

    if ((wasBusy && !isBusy) || (is === 'waiting' && was !== 'waiting')) {
      const row = cachedRows().find(s => ownerOf(s) === owner && sessionIds(s).includes(id))
      patches[rowKeyForLive(owner, id)] = {
        returnedAt: now,
        ...(row && isFocused({ s: row }) ? { visitedAt: now } : {})
      }
    } else if (!wasBusy && was !== 'waiting' && BUSY.has(is)) {
      patches[rowKeyForLive(owner, id)] = { sentAt: now }
    }
  }

  if (Object.keys(patches).length) {
    writeRecs(patches)
  }
}

function setOwnerLive(owner, entry) {
  const cur = $live.get()
  $live.set({ ...cur, owners: { ...cur.owners, [owner]: entry } })
}

function sameMap(a = {}, b = {}) {
  const ka = Object.keys(a)

  return ka.length === Object.keys(b).length && ka.every(k => a[k] === b[k])
}

async function resolvePendingKind(owner, storedId, runtimeId, request) {
  const tag = `${owner}|${storedId}`

  if (pendingLookups.has(tag)) {
    return
  }

  pendingLookups.add(tag)
  let kind = 'input'

  try {
    const res = await request('approval.pending', { session_id: runtimeId })

    if (Array.isArray(res?.approvals) && res.approvals.length > 0) {
      kind = 'approval'
    }
  } catch {
    // unknown: say Input, never invent an approval
  } finally {
    pendingLookups.delete(tag)
  }

  const entry = $live.get().owners[owner]

  if (entry?.statuses[storedId] === 'waiting' && entry.pending[storedId] !== kind) {
    setOwnerLive(owner, { ...entry, pending: { ...entry.pending, [storedId]: kind } })
  }
}

function markOwnerUnreachable(owner) {
  const entry = $live.get().owners[owner]
  const lastOk = lastLiveOk.get(owner) || 0

  if (entry && !entry.stale && Date.now() - lastOk > LIVE_STALE_MS) {
    setOwnerLive(owner, { ...entry, stale: true })
  }
}

async function pollOwner(owner, unscoped) {
  const gen = connGeneration
  // Name the profile on every call: Desktop now serves secondary profiles over
  // the default profile's shared socket, which answers for its own home unless
  // told otherwise (the default profile's turns showed up as this one's).
  const profile = owner.slice(owner.indexOf('|') + 1)
  const request = (m, p) => unscoped(m, { ...p, profile })
  let res
  let delegation

  try {
    ;[res, delegation] = await Promise.all([
      request('session.active_list', {}),
      request('delegation.status', {}).catch(() => null)
    ])
  } catch {
    if (gen === connGeneration) {
      markOwnerUnreachable(owner)
    }

    return
  }

  if (gen !== connGeneration) {
    return
  }

  lastLiveOk.set(owner, Date.now())
  const prev = $live.get().owners[owner]
  const statuses = {}
  const runtime = {}
  // Delegated children and native background work (Claude Code shells, monitors,
  // subagents) that outlive their parent's turn keep it working.
  const background = {}

  for (const row of res?.sessions || []) {
    const stored = row.session_key || row.id

    if (stored) {
      // A sent turn reports 'starting' while its agent is still being built: that is work.
      statuses[stored] = row.status === 'starting' && row.turn_started_at ? 'working' : row.status
      runtime[stored] = row.id

      if (row.background_tasks > 0) {
        background[stored] = (background[stored] || 0) + row.background_tasks
      }
    }
  }

  for (const child of delegation?.active || []) {
    const parent = child.owner_agent_session_id

    if (parent) {
      background[parent] = (background[parent] || 0) + 1
    }
  }

  const now = Date.now()
  const pending = {}
  const waitingSince = {}

  for (const [id, status] of Object.entries(statuses)) {
    if (status === 'waiting') {
      waitingSince[id] = prev?.statuses[id] === 'waiting' ? prev.waitingSince[id] || now : now
      pending[id] = prev?.statuses[id] === 'waiting' ? prev.pending[id] || null : null

      if (!pending[id]) {
        void resolvePendingKind(owner, id, runtime[id], request)
      }
    }
  }

  const next = { background, pending, runtime, stale: false, statuses, waitingSince }

  if (prev) {
    recordEdges(owner, prev, next)
  }

  const changed =
    !prev ||
    prev.stale ||
    !sameMap(prev.statuses, statuses) ||
    !sameMap(prev.background, background) ||
    !sameMap(prev.pending, pending)

  if (changed) {
    // A row that just stopped working has new content: refetch so the
    // outcome, unread and activity land without waiting for the poll.
    if (prev && Object.keys(prev.statuses).some(k => BUSY.has(prev.statuses[k]) && statuses[k] !== prev.statuses[k])) {
      refresh()
    }

    setOwnerLive(owner, next)
  }
}

async function pollLive() {
  if (document.hidden) {
    return
  }

  pinsChangedElsewhere()
  const conn = activeConn()
  const profile = activeProfile()
  const owner = `${conn}|${profile}`
  const polls = []

  if (host.state.gateway.get() === 'open') {
    polls.push(pollOwner(owner, (m, p) => host.request(m, p)))
  } else {
    markOwnerUnreachable(owner)
  }

  // Other local profiles opened from here (pooled sockets): bare-profile routes are local-only.
  if (conn === 'local' && typeof host.requestProfile === 'function') {
    for (const other of openedProfiles) {
      if (other !== profile) {
        polls.push(pollOwner(`${conn}|${other}`, (m, p) => host.requestProfile(other, m, p, 8_000)))
      }
    }
  }

  await Promise.all(polls)
}

// A new connection or gateway profile: nothing we knew about the old source applies.
function resetSourceState() {
  connGeneration += 1
  $live.set({ owners: {} })
  lastLiveOk.clear()
  workingSince = new Map()
  rowCache = new Map()
  $selection.set({ anchor: null, keys: [] })
  void pullLifecycle()
  void pollLive()
}

// ─── recovery (T3's restart continuation + usage-limit recovery) ────────────
// Crash: Hermes journals every running turn in `<home>/desktop/interrupted_turns.json`
// and clears it on any conclusion, so a marker left by a dead process is proof the
// app or backend died mid-turn. Core re-submits a fresh one only when the session
// is opened (and never for native-runtime sessions or past its freshness window), so
// on launch we resume every such thread ourselves: `session.resume` lets core
// continue it, and when core declines we send the continuation note. Like T3 this is
// blocked for settled / snoozed / archived threads and capped per run chain.
// Limit: a thread whose last turn hit a usage limit is resumed as soon as the limit
// lifts (reset time parsed from the reply, else a fixed fallback), once per failure,
// capped; optionally snoozed until then so it stays out of the way meanwhile.
const CRASH_NOTE =
  '[The app stopped while you were working on this thread. Some of the work may already be done: check the current state before redoing anything, then finish the task.]'
const LIMIT_NOTE = '[The usage limit that stopped this thread has reset. Carry on where you left off.]'
const CRASH_CHAIN_MAX = 3
const LIMIT_CHAIN_MAX = 8
const LIMIT_FALLBACK_MS = 20 * MINUTE
const LIMIT_SLACK_MS = 5_000
const INTERRUPTED_POLL_MS = 60_000
const RETRY_MS = 60_000

let recoveryBusy = false
let lastInterruptedAt = 0
let crashTried = new Set() // `<profile>|<session key>|<marker start>`: handled this run
let recoveryInflight = new Set()

const readRecovery = () => {
  const raw = ctxRef?.storage.get('recovery', null)

  return { crash: { ...(raw?.crash || {}) }, limit: { ...(raw?.limit || {}) } }
}

const writeRecovery = rec => ctxRef?.storage.set('recovery', rec)

function requestFor(profile) {
  if (profile === activeProfile()) {
    return host.state.gateway.get() === 'open' ? (m, p) => host.request(m, p) : null
  }

  return activeConn() === 'local' && typeof host.requestProfile === 'function'
    ? (m, p) => host.requestProfile(profile, m, p, 60_000)
    : null
}

// Resume `sessionId` and make sure it keeps going. Returns false when the source is unreachable.
async function continueSession(profile, sessionId, note) {
  const request = requestFor(profile)

  if (!request) {
    return false
  }

  const res = await request('session.resume', { session_id: sessionId })

  if (!res?.session_id) {
    throw new Error('session.resume returned no session')
  }

  // Core is re-submitting the interrupted turn itself, or the session is already mid-turn.
  if (res.auto_continue || (res.status && res.status !== 'idle')) {
    return true
  }

  await request('prompt.submit', { session_id: res.session_id, text: note })

  return true
}

const isSettledRec = r => Boolean(r.settledAt) && !(r.unsettledAt > r.settledAt)

async function recoverInterrupted() {
  const rows = cachedRows()

  if (!rows.length) {
    return false
  }

  let data

  try {
    data = await inboxRest('/interrupted')
  } catch {
    return true
  }

  const rec = readRecovery()
  const now = Date.now()

  for (const [profile, marks] of Object.entries(data || {})) {
    for (const [sessionKey, mark] of Object.entries(marks || {})) {
      const tag = `${profile}|${sessionKey}|${mark.started_at}`
      const row = rows.find(r => profileOf(r) === profile && sessionIds(r).includes(sessionKey))

      if (!mark.auto_continue || crashTried.has(tag) || recoveryInflight.has(tag) || !row) {
        continue
      }

      const key = keyOf(row)
      const life = recOf($life.get(), key)
      const chain = rec.crash[key]
      const n = chain && now - chain.at < DAY ? chain.n : 0

      // Blocked like T3: settled, snoozed, or restarted too many times in a row.
      if (isSettledRec(life) || (life.snoozeUntil && life.snoozeUntil > now) || n >= CRASH_CHAIN_MAX) {
        crashTried.add(tag)
        continue
      }

      recoveryInflight.add(tag)

      try {
        if (await continueSession(profile, sessionKey, CRASH_NOTE)) {
          crashTried.add(tag)
          rec.crash[key] = { at: now, n: n + 1 }
          writeRecovery(rec)
          host.notify({ kind: 'info', message: `Resumed “${rowTitle(row)}” after the app stopped` })
        }
      } catch {
        // retried on the next marker poll
      } finally {
        recoveryInflight.delete(tag)
      }
    }
  }

  return true
}

async function recoverLimited() {
  const prefs = $prefs.get()
  const rec = readRecovery()
  const rows = cachedRows()
  const live = $live.get()
  const now = Date.now()
  let dirty = false

  // A thread that moved past its limit starts a fresh chain.
  for (const key of Object.keys(rec.limit)) {
    const row = rows.find(r => keyOf(r) === key)

    if (row && row.outcome !== 'limited') {
      delete rec.limit[key]
      dirty = true
    }
  }

  for (const key of Object.keys(rec.crash)) {
    const row = rows.find(r => keyOf(r) === key)

    if (row?.outcome === 'ok' && row.outcome_at * 1000 > rec.crash[key].at + MINUTE) {
      delete rec.crash[key]
      dirty = true
    }
  }

  if (prefs.autoResumeLimited || prefs.snoozeLimited) {
    for (const s of rows) {
      if (s.outcome !== 'limited' || !s.outcome_at) {
        continue
      }

      const key = keyOf(s)
      const life = recOf($life.get(), key)
      const src = live.owners[ownerOf(s)]

      if (isSettledRec(life) || (src && sessionIds(s).some(id => src.statuses[id]))) {
        continue
      }

      const outcomeAt = s.outcome_at * 1000
      const known = s.limit_reset_at && s.limit_reset_at > outcomeAt
      const resetAt = known ? s.limit_reset_at : outcomeAt + LIMIT_FALLBACK_MS
      const lr = { ...rec.limit[key] }

      if (prefs.snoozeLimited && known && resetAt > now && lr.snoozedFor !== outcomeAt) {
        writeRecs({ [key]: { snoozeAt: now, snoozeUntil: resetAt } })
        rec.limit[key] = { ...lr, snoozedFor: outcomeAt }
        dirty = true
      }

      const due =
        prefs.autoResumeLimited &&
        now >= resetAt + LIMIT_SLACK_MS &&
        !(life.snoozeUntil && life.snoozeUntil > now) &&
        (lr.n || 0) < LIMIT_CHAIN_MAX &&
        !(lr.outcomeAt === outcomeAt && lr.tried) &&
        !(lr.retryAt && lr.retryAt > now)

      if (!due || recoveryInflight.has(key)) {
        continue
      }

      recoveryInflight.add(key)

      try {
        if (await continueSession(profileOf(s), s.id, LIMIT_NOTE)) {
          rec.limit[key] = { ...rec.limit[key], n: (lr.n || 0) + 1, outcomeAt, tried: true }
          host.notify({ kind: 'info', message: `Usage limit reset — resumed “${rowTitle(s)}”` })
        }
      } catch {
        rec.limit[key] = { ...rec.limit[key], retryAt: now + RETRY_MS }
      } finally {
        recoveryInflight.delete(key)
        dirty = true
      }
    }
  }

  if (dirty) {
    writeRecovery(rec)
  }
}

// The Orchestrator owns restart continuation in the backend while its switch is on (T3's
// server-side RestartContinuation: shutdown capture, crash snapshot, chain cap, background note).
// The Inbox only covers crashes itself when that feature is off, so no thread is continued twice.
let orchestratorChecked = 0
let orchestratorOwns = false

async function orchestratorOwnsRestarts() {
  if (Date.now() - orchestratorChecked < INTERRUPTED_POLL_MS) {
    return orchestratorOwns
  }

  orchestratorChecked = Date.now()

  try {
    const settings = await api({ path: '/api/plugins/t3code/orchestrator/settings', timeoutMs: 15_000 })
    orchestratorOwns = Boolean(settings?.continue_after_restart)
  } catch {
    orchestratorOwns = false
  }

  return orchestratorOwns
}

async function runRecovery() {
  if (recoveryBusy) {
    return
  }

  recoveryBusy = true

  try {
    await recoverLimited()

    if (
      $prefs.get().resumeAfterCrash &&
      Date.now() - lastInterruptedAt > INTERRUPTED_POLL_MS &&
      !(await orchestratorOwnsRestarts())
    ) {
      if (await recoverInterrupted()) {
        lastInterruptedAt = Date.now()
      }
    }
  } catch {
    // best effort: the next tick tries again
  } finally {
    recoveryBusy = false
  }
}

// ─── classification (T3's section rules on Hermes data) ─────────────────────
const isFailure = outcome => outcome === 'failed' || outcome === 'limited'
// Hermes also reports 'starting' for read-only resume/prewarm. Only an actual
// turn is work: opening a thread must not fake a send, completion or shelf move.
const BUSY = new Set(['working'])
// Sources started on this machine get no badge; only messaging platforms do.
// Hermes 0.22 renamed 'desktop' to 'gui' and added 'local' for Desktop chats.
const LOCAL_SOURCES = new Set(['tui', 'desktop', 'gui', 'local', 'cli', 'api'])

function deriveRow(s, cx) {
  const { life, live, prefs, now, focusedId, pinOrder } = cx
  const key = keyOf(s)
  const ids = sessionIds(s)
  const lastActive = (s.last_active || s.started_at || 0) * 1000
  const rec = recOf(life, key)
  const src = live.owners[ownerOf(s)] || null
  const find = map => (map ? ids.map(id => map[id]).find(v => v !== undefined && v !== null) : undefined)
  const liveStatus = find(src?.statuses) || null
  const stale = Boolean(src?.stale)
  const busy = BUSY.has(liveStatus)
  const waiting = liveStatus === 'waiting'
  const pendingKind = waiting ? find(src?.pending) || 'input' : null
  const backgroundCount = src ? ids.reduce((n, id) => n + (src.background[id] || 0), 0) : 0
  const background = !busy && !waiting && backgroundCount > 0
  const waitingSince = waiting ? find(src?.waitingSince) || now : null

  if (busy || background) {
    if (!workingSince.has(key)) {
      workingSince.set(key, Date.now())
    }
  } else {
    workingSince.delete(key)
  }

  const outcome = s.outcome || null
  const outcomeAt = s.outcome_at ? s.outcome_at * 1000 : null
  const sentAt = Math.max(s.sent_at ? s.sent_at * 1000 : 0, rec.sentAt || 0) || null
  const visitedAt = rec.visitedAt || 0
  // T3's Done is an unseen completion, not a permanent successful outcome.
  // The visit watermark also masks stale unread data until the roster refreshes.
  const completedAt = Math.max(outcomeAt || 0, rec.returnedAt || 0) || (s.unread ? lastActive : 0)
  const unseenCompletion = completedAt > visitedAt && Boolean(s.unread || visitedAt || rec.returnedAt)
  const pinPos = ids.map(id => pinOrder.get(id)).find(v => v !== undefined)
  const isPinned = pinnedFlag(s, key, pinPos, now)
  const live_ = busy || waiting || background
  // A failure you have not looked at since it happened.
  const freshFailure = isFailure(outcome) && (Boolean(s.unread) || (outcomeAt !== null && outcomeAt > visitedAt))

  let isSettled

  if (rec.settledAt && !(rec.unsettledAt > rec.settledAt)) {
    // New messages since the settle bring it back (T3's unsettle on new work).
    const reengaged =
      typeof rec.settledCount === 'number' && typeof s.message_count === 'number'
        ? s.message_count > rec.settledCount
        : lastActive > (rec.settledLast ?? rec.settledAt) + 1000
    isSettled = !reengaged && !live_
  } else {
    const keptSince = Math.max(lastActive, rec.unsettledAt || 0, rec.returnedAt || 0)
    isSettled =
      prefs.autoSettleDays > 0 &&
      !s.unread &&
      !isPinned &&
      !live_ &&
      !freshFailure &&
      now - keptSince > prefs.autoSettleDays * DAY
  }

  // Snooze wakes early for a question or approval, a fresh failure, or a run
  // that finished after you snoozed. A failure that predates the snooze was
  // deliberately parked and does not count.
  const snoozeUntil = rec.snoozeUntil || null
  const snoozeAt = rec.snoozeAt || 0
  let wokeAt = null

  if (snoozeUntil) {
    if (waiting) {
      wokeAt = waitingSince
    } else if (!busy && !background && outcomeAt !== null && outcomeAt > snoozeAt) {
      wokeAt = outcomeAt
    } else if (!busy && !background && outcomeAt === null && s.unread && lastActive > snoozeAt) {
      wokeAt = lastActive
    } else if (snoozeUntil <= now) {
      wokeAt = snoozeUntil
    }
  }

  const isSnoozed = Boolean(snoozeUntil) && snoozeUntil > now && wokeAt === null
  const isWoke = wokeAt !== null && visitedAt < wokeAt && !isSettled
  const isActive = focusedId !== null && ids.includes(focusedId)

  const status = waiting
    ? pendingKind === 'approval'
      ? 'approval'
      : 'input'
    : busy
      ? 'working'
      : background
        ? 'background'
        : freshFailure && !isActive
          ? outcome
          : isWoke
            ? 'woke'
            : unseenCompletion && !isActive
              ? 'done'
              : null

  // T3's inbox order: the latest moment this came back to you, never streaming.
  const attentionAt =
    Math.max(outcomeAt || 0, rec.returnedAt || 0, rec.unsettledAt || 0, isWoke ? wokeAt : 0, waitingSince || 0) ||
    lastActive

  return {
    attentionAt,
    background,
    backgroundCount,
    busy,
    isActive,
    isPinned,
    isSettled,
    isSnoozed,
    isWoke,
    key,
    lastActive,
    outcome,
    outcomeAt,
    pendingKind,
    pinPos,
    s,
    sentAt: sentAt || workingSince.get(key) || lastActive,
    settledAt: rec.settledAt && isSettled ? rec.settledAt : lastActive,
    snoozeUntil,
    stale,
    status,
    waiting,
    wokeAt,
    workingStartedAt: workingSince.get(key) || null
  }
}

// Reuse the previous row object when nothing changed, so memoized rows skip rendering.
function shareRow(row) {
  const prev = rowCache.get(row.key)

  if (prev && Object.keys(row).every(k => prev[k] === row[k])) {
    return prev
  }

  rowCache.set(row.key, row)

  return row
}

const byKey = (a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0)

function classify(sessions, { life, live, prefs, pins, now, focusedId }) {
  const pinned = []
  const inbox = []
  const working = []
  const snoozed = []
  const settled = []
  const rows = []
  const pinOrder = new Map(pins.map((id, i) => [id, i]))
  const cx = { focusedId, life, live, now, pinOrder, prefs }

  for (const s of sessions) {
    const row = shareRow(deriveRow(s, cx))
    rows.push(row)

    // T3's precedence: Snoozed → Settled → Pinned → Working → Inbox.
    if (row.isSnoozed) {
      snoozed.push(row)
    } else if (row.isSettled) {
      settled.push(row)
    } else if (row.isPinned) {
      pinned.push(row)
    } else if (prefs.workingShelf && (row.busy || row.background)) {
      working.push(row)
    } else {
      inbox.push(row)
    }
  }

  const unknownPin = Number.MAX_SAFE_INTEGER
  pinned.sort((a, b) => (a.pinPos ?? unknownPin) - (b.pinPos ?? unknownPin) || b.lastActive - a.lastActive || byKey(a, b))
  // With the Working shelf on, the inbox orders by return-to-you and Working by
  // your last send (completions and streaming never reshuffle it). Off, busy
  // rows sit in the inbox under the plain activity order, like T3 without the beta.
  inbox.sort((a, b) =>
    prefs.workingShelf ? b.attentionAt - a.attentionAt || byKey(a, b) : b.lastActive - a.lastActive || byKey(a, b)
  )
  working.sort((a, b) => b.sentAt - a.sentAt || byKey(a, b))
  snoozed.sort((a, b) => a.snoozeUntil - b.snoozeUntil || byKey(a, b))
  settled.sort((a, b) => b.settledAt - a.settledAt || byKey(a, b))

  return { inbox, pinned, rows, settled, snoozed, working }
}

// ─── actions ────────────────────────────────────────────────────────────────
const asList = rows => (Array.isArray(rows) ? rows : [rows]).filter(Boolean)

function isFocused(row) {
  const focused = host.state.focusedStoredSessionId.get()

  if (focused === null || !sessionIds(row.s).includes(focused)) {
    return false
  }

  const owner = host.state.focusedSessionOwner?.get?.()

  return !owner?.profile || owner.profile === profileOf(row.s)
}

// T3's planForwardNavigation: parking (settle / snooze) the session on screen
// moves to the next card below it in the list — wrapping to the top and
// skipping anything parked in the same batch — or, with no card left, a blank
// session in the same profile. Planned BEFORE the park so the order is the one you saw.
function planForwardNavigation(parked) {
  const focusedRow = parked.find(isFocused)

  if (!focusedRow) {
    return null
  }

  const leaving = new Set(parked.map(r => r.key))
  const groups = lastGroups || groupsForCommands()
  const cards = [...groups.pinned, ...groups.inbox, ...groups.working]
  const i = cards.findIndex(r => r.key === focusedRow.key)
  const ordered = i === -1 ? cards : [...cards.slice(i + 1), ...cards.slice(0, i)]
  const next = ordered.find(r => !leaving.has(r.key))

  return next ? () => openRow(next) : () => host.newChat(profileOf(focusedRow.s))
}

function parkAndNavigate(rows, park) {
  const navigate = planForwardNavigation(rows)
  const focused = rows.find(isFocused)
  park()

  // Only move if the user is still on the parked session.
  if (navigate && focused && isFocused(focused)) {
    navigate()
  }
}

function markVisited(key) {
  writeRecs({ [key]: { visitedAt: Date.now() } })
}

function openRow(row, intent) {
  if (connOf(row.s) === 'local') {
    openedProfiles.add(profileOf(row.s))
  }

  // forceResume = the sidebar's requestSessionResume: without it a chat already
  // cached in the renderer paints its stale snapshot, and a fork taken from that
  // view is cut at the last message it showed (turns behind the real thread).
  host
    .openSession(row.s.id, {
      awaitHydration: true,
      expectHistory: (row.s.message_count || 0) > 0,
      forceResume: true,
      retryHydrationTimeoutOnce: true,
      ...(row.s.profile ? { profile: row.s.profile } : {}),
      ...(intent ? { intent } : {})
    })
    // Acknowledge only once the session actually opened.
    .then(() => markVisited(row.key))
    .catch(err => host.notifyError(err, 'Could not open session'))
}

// What stops a row from being settled (T3's server guards), or null.
function settleBlocker(row) {
  if (row.waiting) {
    return row.pendingKind === 'approval' ? 'is waiting for your approval' : 'is waiting for your answer'
  }

  if (row.busy) {
    return 'is still working'
  }

  if (row.background) {
    return 'still has background work running'
  }

  return null
}

const snapshot = rows => rows.map(r => ({ before: recOf($life.get(), r.key), row: r }))

// Action-specific Undo: reverts only records still exactly as this action
// left them — anything changed since (a new turn, another window) stays.
function offerUndo(message, entries, written, extra) {
  host.notify({
    action: {
      label: 'Undo',
      onClick: () => {
        const life = $life.get()
        let skipped = 0

        for (const e of entries) {
          if (life.records[e.row.key]?.t !== written[e.row.key]?.t) {
            skipped += 1
            continue
          }

          restoreRec(e.row.key, e.before)
          extra?.(e)
        }

        if (skipped) {
          host.notify({ kind: 'info', message: `${plural(skipped, 'session')} changed since — left as is` })
        }
      }
    },
    kind: 'info',
    message
  })
}

function settle(rows) {
  const list = asList(rows)
  const blocked = list.filter(settleBlocker)
  const ok = list.filter(r => !settleBlocker(r))

  if (blocked.length) {
    const snoozable = blocked.filter(r => !r.waiting)
    host.notify({
      ...(snoozable.length ? { action: { label: 'Snooze 1h', onClick: () => snooze(snoozable, Date.now() + HOUR) } } : {}),
      kind: 'warning',
      message:
        blocked.length === 1
          ? `“${rowTitle(blocked[0].s)}” ${settleBlocker(blocked[0])} — ${blocked[0].waiting ? 'answer it first' : 'snooze it instead'}`
          : `${plural(blocked.length, 'session')} still active — snooze ${blocked.length === 1 ? 'it' : 'them'} instead`
    })
  }

  if (!ok.length) {
    return
  }

  parkAndNavigate(ok, () => {
    const now = Date.now()
    writeRecs(
      Object.fromEntries(
        ok.map(r => [
          r.key,
          {
            settledAt: now,
            settledCount: typeof r.s.message_count === 'number' ? r.s.message_count : null,
            settledLast: r.lastActive,
            snoozeAt: null,
            snoozeUntil: null,
            unsettledAt: null
          }
        ])
      )
    )

    for (const r of ok) {
      // Settling clears the pin, like T3's decider.
      if (r.isPinned) {
        setPinnedRaw(r, false)
      }

      if (r.s.unread) {
        setUnread(r, false)
      }
    }
  })
}

function unsettle(rows) {
  const now = Date.now()
  writeRecs(
    Object.fromEntries(
      asList(rows).map(r => [r.key, { returnedAt: now, settledAt: null, settledCount: null, settledLast: null, unsettledAt: now }])
    )
  )
}

function snooze(rows, until) {
  const list = asList(rows)
  // T3 refuses to hide a pending approval or question: it needs you now.
  const blocked = list.filter(r => r.waiting)
  const ok = list.filter(r => !r.waiting)

  if (blocked.length) {
    host.notify({
      kind: 'warning',
      message: `${blocked.length === 1 ? `“${rowTitle(blocked[0].s)}” is` : `${plural(blocked.length, 'session')} are`} waiting on you — answer before snoozing`
    })
  }

  if (!ok.length) {
    return
  }

  const entries = snapshot(ok)
  let written = {}

  parkAndNavigate(ok, () => {
    const now = Date.now()
    written = writeRecs(Object.fromEntries(ok.map(r => [r.key, { snoozeAt: now, snoozeUntil: until }])))
  })

  offerUndo(`${ok.length === 1 ? 'Snoozed' : `${plural(ok.length, 'session')} snoozed`} until ${wakeDescription(until)}`, entries, written)
}

function unsnooze(rows) {
  const now = Date.now()
  writeRecs(
    Object.fromEntries(asList(rows).map(r => [r.key, { returnedAt: now, snoozeAt: null, snoozeUntil: null, visitedAt: now }]))
  )
}

function acknowledgeWoke(row) {
  writeRecs({
    [row.key]: { returnedAt: row.wokeAt || Date.now(), snoozeAt: null, snoozeUntil: null, visitedAt: Date.now() }
  })
}

// The pin write itself: core's store (order, sidebar) + the backend flag for
// the row's own profile (authoritative across windows and devices).
function setPinnedRaw(row, pinned, index) {
  host.sessions.pin(durableOf(row.s), pinned, index)
  pinOverlay.set(row.key, { at: Date.now(), pinned })
  patchSession(row, { pinned }).catch(err => host.notifyError(err, 'Could not update pin'))
  $pinsVersion.set($pinsVersion.get() + 1)
}

function setPinned(rows, pinned, index) {
  const list = asList(rows).filter(r => r.isPinned !== pinned)

  if (!list.length) {
    return
  }

  const entries = snapshot(list)
  list.forEach((r, i) => setPinnedRaw(r, pinned, index === undefined ? undefined : index + i))
  let written = {}

  if (pinned) {
    // A pin is an explicit promotion: it leaves Snoozed and Settled at once.
    const now = Date.now()
    written = writeRecs(
      Object.fromEntries(
        list.map(r => [r.key, { settledAt: null, settledCount: null, settledLast: null, snoozeAt: null, snoozeUntil: null, unsettledAt: now }])
      )
    )
  }

  offerUndo(
    `${list.length === 1 ? 'Session' : plural(list.length, 'session')} ${pinned ? 'pinned' : 'unpinned'}`,
    entries,
    pinned ? written : Object.fromEntries(entries.map(e => [e.row.key, recOf($life.get(), e.row.key)])),
    e => setPinnedRaw(e.row, !pinned)
  )
}

function reorderPinned(keys) {
  const rows = new Map((lastGroups?.rows || []).map(r => [r.key, r]))
  host.sessions.reorderPinned(keys.map(k => (rows.get(k) ? durableOf(rows.get(k).s) : k.split('|').pop())))
  $pinsVersion.set($pinsVersion.get() + 1)
}

function patchSession(row, body) {
  const profile = row.s.profile && row.s.profile !== 'default' ? row.s.profile : null

  return api({
    ...(profile ? { profile } : {}),
    ...(row.s.connection_id ? { connectionId: row.s.connection_id } : {}),
    path: `/api/sessions/${encodeURIComponent(row.s.id)}`,
    method: 'PATCH',
    body: { ...body, ...(profile ? { profile } : {}) }
  })
}

function setUnread(rows, unread) {
  for (const row of asList(rows)) {
    patchCachedRows(list => list.map(s => (sameSession(s, row.s) ? { ...s, unread } : s)))
    patchSession(row, { unread })
      .catch(err => host.notifyError(err, 'Could not update read state'))
      .finally(refresh)
  }
}

function rename(row, title) {
  const next = title.trim()

  if (!next || next === (row.s.title || '')) {
    return
  }

  patchCachedRows(list => list.map(s => (sameSession(s, row.s) ? { ...s, title: next } : s)))
  patchSession(row, { title: next })
    .catch(err => host.notifyError(err, 'Could not rename session'))
    .finally(refresh)
}

function archive(row) {
  patchCachedRows(list => list.filter(s => !sameSession(s, row.s)))
  patchSession(row, { archived: true })
    .then(() =>
      host.notify({
        action: {
          label: 'Undo',
          onClick: () => patchSession(row, { archived: false }).finally(refresh)
        },
        kind: 'info',
        message: 'Session archived'
      })
    )
    .catch(err => host.notifyError(err, 'Could not archive session'))
    .finally(refresh)
}

// ─── selection ──────────────────────────────────────────────────────────────
function clearSelection() {
  if ($selection.get().keys.length) {
    $selection.set({ anchor: null, keys: [] })
  }
}

function toggleSelect(key) {
  const { keys } = $selection.get()
  $selection.set({ anchor: key, keys: keys.includes(key) ? keys.filter(k => k !== key) : [...keys, key] })
}

function rangeSelect(key) {
  const { anchor, keys } = $selection.get()
  const from = anchor ? lastVisibleKeys.indexOf(anchor) : -1
  const to = lastVisibleKeys.indexOf(key)

  if (from === -1 || to === -1) {
    toggleSelect(key)

    return
  }

  const span = lastVisibleKeys.slice(Math.min(from, to), Math.max(from, to) + 1)
  $selection.set({ anchor, keys: [...new Set([...keys, ...span])] })
}

function selectedRows() {
  const keys = new Set($selection.get().keys)

  return (lastGroups?.rows || []).filter(r => keys.has(r.key))
}

// A menu action on a row that is part of a multi-selection applies to the selection.
function targetsOf(row) {
  const keys = $selection.get().keys

  return keys.length > 1 && keys.includes(row.key) ? selectedRows() : [row]
}

// ─── shared row bits ────────────────────────────────────────────────────────
function rowTitle(s) {
  return (s.title || '').trim() || (s.preview || '').trim().split('\n', 1)[0] || 'Untitled session'
}

function ProfileMark({ profile }) {
  const name = profile || 'default'
  const color = profileColor(name)

  return h(
    'span',
    {
      className: 't3i-avatar',
      style: color
        ? { background: profileColorSoft(color, 22), color }
        : { background: 'var(--ui-row-hover-background)', color: 'var(--ui-text-secondary)' },
      title: name
    },
    name.slice(0, 1)
  )
}

// Ticks on its own so the rest of the pane re-renders on state edges only.
function WorkingTimer({ since }) {
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    if (!since) {
      return undefined
    }

    const id = window.setInterval(() => !document.hidden && setNow(Date.now()), 1_000)

    return () => window.clearInterval(id)
  }, [since])

  return since ? h('span', null, workingDuration(now - since)) : null
}

const OUTCOME_MARKS = {
  failed: { className: 't3i-c-err', icon: 'x', title: 'Last turn failed' },
  limited: { className: 't3i-c-warn', icon: 'gauge', title: 'Last turn hit a usage limit' },
  stopped: { className: 't3i-dim', icon: 'square', title: 'Stopped' }
}

function OutcomeMark({ row }) {
  const mark = !row.busy && !row.background && OUTCOME_MARKS[row.outcome]

  return mark ? h('span', { className: mark.className, title: mark.title }, h(Icon, { name: mark.icon, size: 12 })) : null
}

function staleTitle(row) {
  return row.stale ? 'Last known status — this source is not answering right now' : undefined
}

function StatusLabel({ row, now }) {
  const stale = row.stale ? '' : undefined

  switch (row.status) {
    case 'working':
      return h(
        'span',
        { className: 't3i-status t3i-c-info', 'data-stale': stale, title: staleTitle(row) },
        h(Icon, { className: row.stale ? undefined : 't3i-spin', name: row.stale ? 'cloudOff' : 'circleDashed', size: 15 }),
        h('b', { role: 'status' }, 'Working'),
        row.stale ? null : h(WorkingTimer, { since: row.workingStartedAt })
      )
    case 'background':
      return h(
        'span',
        {
          className: 't3i-status t3i-c-info',
          'data-stale': stale,
          title: staleTitle(row) || `${plural(row.backgroundCount, 'delegated task')} still running`
        },
        h(Icon, { name: 'users', size: 14 }),
        h('b', { role: 'status' }, 'Background')
      )
    case 'approval':
      return h(
        'span',
        { className: 't3i-status t3i-c-warn', 'data-stale': stale, title: staleTitle(row) },
        h(Icon, { name: 'shield', size: 15 }),
        h('b', { role: 'status' }, 'Approval')
      )
    case 'input':
      return h(
        'span',
        { className: 't3i-status t3i-c-input', 'data-stale': stale, title: staleTitle(row) },
        h(Icon, { name: 'messageQuestion', size: 15 }),
        h('b', { role: 'status' }, 'Input')
      )
    case 'failed':
      return h(
        'span',
        { className: 't3i-status t3i-c-err', title: 'The last turn failed' },
        h(Icon, { name: 'circleX', size: 15 }),
        h('b', { role: 'status' }, 'Failed')
      )
    case 'limited':
      return h(
        'span',
        { className: 't3i-status t3i-c-warn', title: 'The last turn hit a usage limit' },
        h(Icon, { name: 'gauge', size: 15 }),
        h('b', { role: 'status' }, 'Limited')
      )
    case 'woke':
      return h(
        'span',
        { className: 't3i-status', 'data-keep': '' },
        h(
          Tip,
          { label: 'Dismiss Woke notification' },
          h(
            'button',
            {
              'aria-label': 'Dismiss Woke notification',
              className: 't3i-woke',
              onClick: e => {
                e.stopPropagation()
                acknowledgeWoke(row)
              },
              type: 'button'
            },
            h(Icon, { name: 'alarm', size: 15 }),
            h('span', { role: 'status' }, 'Woke')
          )
        )
      )
    case 'done':
      return h(
        'span',
        { className: 't3i-status t3i-c-ok' },
        h(Icon, { name: 'circleCheck', size: 15 }),
        h('b', { role: 'status' }, 'Done')
      )
    default:
      return h('span', { className: 't3i-status' }, h(OutcomeMark, { row }), compactAgo(row.lastActive, now))
  }
}

function SnoozeItems({ Item, Separator, rows }) {
  const presets = snoozePresets(new Date())

  return [
    ...presets.map(p =>
      h(
        Item,
        { key: p.id, onSelect: () => snooze(rows(), p.until) },
        h('span', null, p.label),
        h('span', { className: 't3i-shortcut' }, p.when)
      )
    ),
    h(Separator, { key: 'sep' }),
    h(Item, { key: 'custom', onSelect: () => $customSnooze.set({ rows: rows() }) }, 'Custom…')
  ]
}

function SnoozeMenu({ row, onOpenChange, open }) {
  return h(
    DropdownMenu,
    { onOpenChange, open },
    h(
      Tip,
      { label: 'Snooze session' },
      h(
        DropdownMenuTrigger,
        { asChild: true },
        h(
          'button',
          {
            'aria-label': 'Snooze session',
            className: 't3i-act',
            onClick: e => e.stopPropagation(),
            onDoubleClick: e => e.stopPropagation(),
            type: 'button'
          },
          h(Icon, { name: 'clock', size: 12 })
        )
      )
    ),
    h(
      DropdownMenuContent,
      { align: 'end', onClick: e => e.stopPropagation(), side: 'bottom' },
      open ? h(SnoozeItems, { Item: DropdownMenuItem, rows: () => [row], Separator: DropdownMenuSeparator }) : null
    )
  )
}

function RowContextMenu({ row, section, children, onRename }) {
  const many = () => targetsOf(row)
  const count = $selection.get().keys.includes(row.key) ? Math.max(1, $selection.get().keys.length) : 1
  const suffix = count > 1 ? ` ${count} sessions` : ''

  return h(
    ContextMenu,
    null,
    h(ContextMenuTrigger, { asChild: true }, children),
    h(
      ContextMenuContent,
      { className: 'min-w-48' },
      h(ContextMenuItem, { onSelect: () => openRow(row) }, 'Open'),
      h(ContextMenuItem, { onSelect: () => openRow(row, 'tab') }, 'Open in new tab'),
      h(ContextMenuItem, { onSelect: () => openRow(row, 'stack') }, 'Open in split'),
      h(ContextMenuSeparator, null),
      count > 1 ? null : h(ContextMenuItem, { onSelect: onRename }, 'Rename'),
      h(ContextMenuItem, { onSelect: () => setPinned(many(), !row.isPinned) }, `${row.isPinned ? 'Unpin' : 'Pin'}${suffix}`),
      h(
        ContextMenuItem,
        { onSelect: () => setUnread(many(), !row.s.unread) },
        `${row.s.unread ? 'Mark as read' : 'Mark as unread'}${suffix}`
      ),
      h(ContextMenuSeparator, null),
      section === 'snoozed'
        ? h(ContextMenuItem, { onSelect: () => unsnooze(many()) }, `Wake now${suffix}`)
        : row.waiting
          ? null
          : h(
              ContextMenuSub,
              null,
              h(ContextMenuSubTrigger, null, `Snooze${suffix}`),
              h(
                ContextMenuSubContent,
                null,
                h(SnoozeItems, { Item: ContextMenuItem, rows: many, Separator: ContextMenuSeparator })
              )
            ),
      section === 'settled'
        ? h(ContextMenuItem, { onSelect: () => unsettle(many()) }, `Un-settle${suffix}`)
        : h(ContextMenuItem, { onSelect: () => settle(many()) }, `Settle${suffix}`),
      h(ContextMenuSeparator, null),
      h(
        ContextMenuItem,
        {
          onSelect: () =>
            ctxRef?.os
              .writeClipboard(row.s.id)
              .then(ok => ok && host.notify({ kind: 'info', message: 'Session ID copied' }))
        },
        'Copy session ID'
      ),
      count > 1 ? null : h(ContextMenuItem, { onSelect: () => archive(row) }, 'Archive')
    )
  )
}

function RenameInput({ row, onDone }) {
  const [value, setValue] = useState(row.s.title || rowTitle(row.s))
  const committed = useRef(false)

  const commit = () => {
    if (committed.current) {
      return
    }

    committed.current = true
    rename(row, value)
    onDone()
  }

  return h('input', {
    'aria-label': 'Session title',
    autoFocus: true,
    className: 't3i-rename',
    onBlur: commit,
    onChange: e => setValue(e.target.value),
    onClick: e => e.stopPropagation(),
    onDoubleClick: e => e.stopPropagation(),
    onFocus: e => e.currentTarget.select(),
    onKeyDown: e => {
      e.stopPropagation()

      if (e.nativeEvent.isComposing) {
        return
      }

      if (e.key === 'Enter') {
        e.preventDefault()
        commit()
      } else if (e.key === 'Escape') {
        e.preventDefault()
        committed.current = true
        onDone()
      }
    },
    value
  })
}

function useRowHandlers(row, drag, section) {
  const [renaming, setRenaming] = useState(false)

  const onClick = useCallback(
    e => {
      if (renaming) {
        return
      }

      // T3: ⌘-click (Ctrl elsewhere) toggles selection, ⇧-click selects a range.
      if (IS_MAC ? e.metaKey : e.ctrlKey) {
        e.preventDefault()
        toggleSelect(row.key)

        return
      }

      if (e.shiftKey && section !== 'search') {
        e.preventDefault()
        rangeSelect(row.key)

        return
      }

      clearSelection()
      openRow(row)
    },
    [row, renaming, section]
  )

  const onAuxClick = useCallback(
    e => {
      if (e.button === 1) {
        e.preventDefault()
        openRow(row, 'tab')
      }
    },
    [row]
  )

  const onDoubleClick = useCallback(e => {
    if (e.target.closest('button, a, input')) {
      return
    }

    e.preventDefault()
    setRenaming(true)
  }, [])

  const onKeyDown = useCallback(
    e => {
      if (e.target !== e.currentTarget) {
        return
      }

      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        openRow(row, e.metaKey || e.ctrlKey ? 'tab' : undefined)
      } else if (e.key === 'F2') {
        e.preventDefault()
        setRenaming(true)
      }
    },
    [row]
  )

  const dragProps = drag
    ? {
        draggable: !renaming,
        onDragStart: e => {
          const keys = $selection.get().keys
          const moving = keys.length > 1 && keys.includes(row.key) ? keys : [row.key]
          e.dataTransfer.effectAllowed = 'move'
          e.dataTransfer.setData(DRAG_MIME, JSON.stringify(moving))
          e.dataTransfer.setData('text/plain', moving.length > 1 ? plural(moving.length, 'session') : rowTitle(row.s))
          drag.start(moving, section)
        },
        onDragEnd: () => drag.end(),
        onDragOver: e => drag.overRow(e, row, section),
        onDrop: e => drag.dropOnRow(e, row, section)
      }
    : {}

  return { dragProps, onAuxClick, onClick, onDoubleClick, onKeyDown, renaming, setRenaming }
}

function useSelected(key) {
  const sel = useValue($selection)

  return sel.keys.includes(key)
}

// ─── card row ───────────────────────────────────────────────────────────────
const CardRow = memo(function CardRow({ row, section, drag, dragging, dropBefore, now }) {
  const { dragProps, onAuxClick, onClick, onDoubleClick, onKeyDown, renaming, setRenaming } = useRowHandlers(row, drag, section)
  const [menuOpen, setMenuOpen] = useState(false)
  const selected = useSelected(row.key)
  const s = row.s
  const recede = (row.status === 'working' || row.status === 'background' || (!row.status && !s.unread)) && !row.isActive
  const attention = ['approval', 'input', 'woke', 'done', 'failed', 'limited'].includes(row.status)
  const title = rowTitle(s)
  const where = s.git_branch || (s.cwd ? s.cwd.split(/[\\/]/).filter(Boolean).pop() : null)
  const sessionDrafts = useValue($sessionDrafts)
  const hasDraft = !!sessionDrafts && sessionIds(s).some(id => sessionDrafts.split('\n').includes(id))

  return h(
    'li',
    { className: 't3i-li t3i-card-li', 'data-session-key': row.key },
    h(
      RowContextMenu,
      { onRename: () => setRenaming(true), row, section },
      h(
        'div',
        {
          'aria-current': row.isActive ? 'page' : undefined,
          'aria-label': `${title}${row.status ? `, ${row.status}` : ''}`,
          'aria-selected': selected || undefined,
          className: 't3i-row',
          'data-active': row.isActive ? '' : undefined,
          'data-attn': attention ? '' : undefined,
          'data-drop-before': dropBefore ? '' : undefined,
          'data-dragging': dragging ? '' : undefined,
          'data-recede': recede ? '' : undefined,
          'data-selected': selected ? '' : undefined,
          'data-working': row.status === 'working' || row.status === 'background' ? '' : undefined,
          onAuxClick,
          onClick,
          onDoubleClick,
          onKeyDown,
          role: 'button',
          tabIndex: 0,
          ...dragProps
        },
        h(
          'div',
          { className: 't3i-card' },
          h(
            'div',
            { className: 't3i-line1' },
            // Like T3 Code: the open chat shows its own composer, so the pen
            // only marks rows you have navigated away from.
            hasDraft && !row.isActive ? h(DraftMark) : null,
            h(ProfileMark, { profile: s.profile }),
            h('span', { className: 't3i-proj' }, s.profile || 'default'),
            s.handoff_platform || (s.source && !LOCAL_SOURCES.has(s.source))
              ? h('span', { className: 't3i-src' }, s.handoff_platform || s.source)
              : null,
            row.isPinned
              ? h(
                  Tip,
                  { label: 'Unpin session' },
                  h(
                    'button',
                    {
                      'aria-label': 'Unpin session',
                      className: 't3i-micro',
                      onClick: e => {
                        e.stopPropagation()
                        setPinned(row, false)
                      },
                      type: 'button'
                    },
                    h(Icon, { name: 'pin', size: 12 })
                  )
                )
              : null,
            h(
              'span',
              { className: 't3i-slot', 'data-menu': menuOpen ? '' : undefined },
              h(StatusLabel, { now, row }),
              h(
                'span',
                { className: 't3i-actions', 'data-open': menuOpen ? '' : undefined },
                row.waiting ? null : h(SnoozeMenu, { onOpenChange: setMenuOpen, open: menuOpen, row }),
                h(
                  Tip,
                  { label: settleBlocker(row) ? `Can’t settle: it ${settleBlocker(row)}` : 'Settle session' },
                  h(
                    'button',
                    {
                      'aria-disabled': settleBlocker(row) ? true : undefined,
                      'aria-label': 'Settle session',
                      className: 't3i-act',
                      onClick: e => {
                        e.stopPropagation()
                        settle(row)
                      },
                      style: { marginRight: -4 },
                      type: 'button'
                    },
                    h(Icon, { name: 'check', size: 14 }),
                    'Settle'
                  )
                )
              )
            )
          ),
          h(
            'div',
            { className: 't3i-line2' },
            renaming ? h(RenameInput, { onDone: () => setRenaming(false), row }) : h('span', { className: 't3i-title' }, title)
          ),
          h(
            'div',
            { className: 't3i-line3' },
            where
              ? h(
                  React.Fragment,
                  null,
                  h(Icon, { className: 't3i-dim', name: s.git_branch ? 'branch' : 'folder', size: 12 }),
                  h('span', { className: 't3i-trunc t3i-dim', style: { flex: 1 } }, where)
                )
              : s.message_count
                ? h('span', { className: 't3i-dim', style: { flex: 1 } }, `${s.message_count} messages`)
                : h('span', { style: { flex: 1 } }),
            s.model
              ? h('span', { className: 't3i-trunc', style: { maxWidth: '50%', marginLeft: 'auto' } }, s.model)
              : null
          )
        )
      )
    )
  )
})

// ─── slim row ───────────────────────────────────────────────────────────────
const SlimRow = memo(function SlimRow({ row, section, drag, dragging, now, highlighted, snippet }) {
  const { dragProps, onAuxClick, onClick, onDoubleClick, onKeyDown, renaming, setRenaming } = useRowHandlers(row, drag, section)
  const selected = useSelected(row.key)
  const s = row.s
  const title = rowTitle(s)
  const snoozedSection = section === 'snoozed'
  const searchSection = section === 'search'

  const action = snoozedSection
    ? { icon: 'alarmOff', label: 'Wake session now', run: () => unsnooze(row) }
    : section === 'settled' || (searchSection && row.isSettled)
      ? { icon: 'undo', label: 'Un-settle session', run: () => unsettle(row) }
      : { icon: 'check', label: 'Settle session', run: () => settle(row) }

  const timeText = snoozedSection && row.snoozeUntil ? null : compactAgo(section === 'settled' ? row.settledAt : row.lastActive, now)
  const attentionStatus = searchSection && row.status && row.status !== 'done' && row.status !== 'woke'

  const slot = h(
    'span',
    { className: 't3i-slim-slot' },
    row.isWoke
      ? h(
          'span',
          { className: 't3i-slim-time', 'data-keep': '' },
          h(
            'button',
            {
              'aria-label': 'Dismiss Woke notification',
              className: 't3i-woke',
              onClick: e => {
                e.stopPropagation()
                acknowledgeWoke(row)
              },
              style: { fontSize: 12 },
              type: 'button'
            },
            h(Icon, { name: 'alarm', size: 12 }),
            'Woke'
          )
        )
      : attentionStatus
        ? h('span', { className: 't3i-slim-time' }, h(StatusLabel, { now, row }))
        : h(
            'span',
            { className: 't3i-slim-time' },
            snoozedSection && row.snoozeUntil
              ? h('span', { className: 't3i-c-info' }, wakeLabel(row.snoozeUntil, now))
              : [h(OutcomeMark, { key: 'o', row }), timeText]
          ),
    h(
      'span',
      { className: 't3i-slim-act', 'data-static': row.isWoke ? '' : undefined },
      h(
        Tip,
        { label: action.label },
        h(
          'button',
          {
            'aria-label': action.label,
            className: 't3i-act',
            onClick: e => {
              e.stopPropagation()
              action.run()
            },
            style: { height: '100%' },
            type: 'button'
          },
          h(Icon, { name: action.icon, size: 13 })
        )
      )
    )
  )

  const rowProps = {
    'aria-current': row.isActive ? 'page' : undefined,
    'aria-label': title,
    'aria-selected': searchSection ? Boolean(highlighted) : selected || undefined,
    className: cn('t3i-row', snippet ? 't3i-hit' : 't3i-slim'),
    'data-active': row.isActive ? '' : undefined,
    'data-attn': row.isWoke ? '' : undefined,
    'data-dragging': dragging ? '' : undefined,
    'data-highlight': highlighted ? '' : undefined,
    'data-selected': selected ? '' : undefined,
    'data-unread': s.unread ? '' : undefined,
    onAuxClick,
    onClick,
    onDoubleClick,
    onKeyDown,
    role: searchSection ? 'option' : 'button',
    tabIndex: searchSection ? -1 : 0,
    title,
    ...dragProps
  }

  const titleNode = renaming
    ? h(RenameInput, { onDone: () => setRenaming(false), row })
    : h('span', { className: 't3i-title' }, title)

  return h(
    'li',
    { className: 't3i-li', 'data-session-key': row.key },
    h(
      RowContextMenu,
      { onRename: () => setRenaming(true), row, section },
      snippet
        ? h(
            'div',
            rowProps,
            h(
              'div',
              { className: 't3i-hit-top t3i-slim', style: { height: 22, padding: 0 } },
              h(ProfileMark, { profile: s.profile }),
              titleNode,
              row.isPinned ? h(Icon, { className: 't3i-dim', name: 'pin', size: 12 }) : null,
              slot
            ),
            h(Snippet, { text: snippet })
          )
        : h(
            'div',
            rowProps,
            h(ProfileMark, { profile: s.profile }),
            titleNode,
            row.isPinned ? h(Icon, { className: 't3i-dim', name: 'pin', size: 12 }) : null,
            slot
          )
    )
  )
})

// FTS snippets mark matches as >>>term<<<.
function Snippet({ text }) {
  const parts = String(text || '')
    .replace(/\s+/g, ' ')
    .split(/(>>>.*?<<<)/g)
    .filter(Boolean)

  return h(
    'div',
    { className: 't3i-snippet' },
    parts.map((p, i) => (p.startsWith('>>>') && p.endsWith('<<<') ? h('mark', { key: i }, p.slice(3, -3)) : p))
  )
}

// ─── section chrome ─────────────────────────────────────────────────────────
function ShelfHeader({ label, count, expanded, onToggle, tone, className, dropProps }) {
  return h(
    'li',
    { className: cn('t3i-li', className), ...(dropProps || {}) },
    h(
      'button',
      {
        'aria-expanded': expanded,
        className: 't3i-shelf',
        'data-tone': tone,
        onClick: onToggle,
        type: 'button'
      },
      h('span', null, expanded ? label : `${label} (${count})`),
      h('span', { 'aria-hidden': true, className: 't3i-shelf-line' }),
      h(Icon, { className: 't3i-caret', name: 'chevronDown', size: 12 })
    )
  )
}

function DragBoundary({ label, target, dropProps }) {
  return h(
    'li',
    { className: 't3i-li', ...(dropProps || {}) },
    h('div', { className: 't3i-boundary', 'data-target': target ? '' : undefined }, h('span', null, label), h('span', null))
  )
}

function DropHint({ label, target, dropProps }) {
  return h(
    'li',
    { className: 't3i-li', ...(dropProps || {}) },
    h('div', { className: 't3i-hint', 'data-target': target ? '' : undefined }, label)
  )
}

// ─── drag & drop (Pinned / Inbox / Settled, pin reordering, multi-row) ──────
function readDragKeys(e) {
  try {
    const keys = JSON.parse(e.dataTransfer.getData(DRAG_MIME) || '[]')

    return Array.isArray(keys) ? keys.filter(k => typeof k === 'string') : []
  } catch {
    return []
  }
}

function useDrag(groupsRef) {
  const [state, setState] = useState(null) // { keys, from, overSection, overKey }

  // Handlers are stable (they read through refs) so memoized rows stay memoized.
  const api_ = useMemo(() => {
    const findRow = key => groupsRef.current?.rows.find(r => r.key === key) || null

    const performDrop = (keys, section, beforeKey) => {
      const rows = keys.map(findRow).filter(Boolean)

      if (!rows.length) {
        return
      }

      if (section === 'pinned') {
        const moving = new Set(rows.map(r => r.key))
        const pins = groupsRef.current.pinned.map(r => r.key).filter(k => !moving.has(k))
        const index = beforeKey && !moving.has(beforeKey) ? Math.max(0, pins.indexOf(beforeKey)) : pins.length
        const fresh = rows.filter(r => !r.isPinned)

        if (fresh.length) {
          setPinned(fresh, true, index)
        }

        // Moving existing pins: write the whole order with every dragged row at the drop slot.
        if (rows.some(r => r.isPinned)) {
          pins.splice(index, 0, ...rows.map(r => r.key))
          reorderPinned(pins)
        }
      } else if (section === 'settled') {
        settle(rows)
      } else if (section === 'inbox') {
        const pinnedRows = rows.filter(r => r.isPinned)

        if (pinnedRows.length) {
          setPinned(pinnedRows, false)
        }

        const snoozedRows = rows.filter(r => r.snoozeUntil)

        if (snoozedRows.length) {
          unsnooze(snoozedRows)
        }

        unsettle(rows)
      }

      clearSelection()
    }

    const accepts = e => e.dataTransfer.types.includes(DRAG_MIME)

    return {
      accepts,
      dropOnRow: (e, row, section) => {
        if (!accepts(e) || section === 'snoozed' || section === 'working') {
          return
        }

        e.preventDefault()
        e.stopPropagation()
        const keys = readDragKeys(e)

        if (!(keys.length === 1 && keys[0] === row.key)) {
          performDrop(keys, section, section === 'pinned' ? row.key : null)
        }

        setState(null)
      },
      end: () => setState(null),
      overRow: (e, row, section) => {
        if (!accepts(e) || section === 'snoozed' || section === 'working') {
          return
        }

        e.preventDefault()
        e.stopPropagation()
        e.dataTransfer.dropEffect = 'move'
        const overKey = section === 'pinned' ? row.key : null
        setState(st => (st && (st.overSection !== section || st.overKey !== overKey) ? { ...st, overKey, overSection: section } : st))
      },
      performDrop,
      start: (keys, from) => setTimeout(() => setState({ from, keys, overKey: null, overSection: from }), 0)
    }
  }, [groupsRef])

  const zone = section => ({
    onDragOver: e => {
      if (!api_.accepts(e)) {
        return
      }

      e.preventDefault()
      e.dataTransfer.dropEffect = 'move'
      setState(st => (st && (st.overSection !== section || st.overKey) ? { ...st, overKey: null, overSection: section } : st))
    },
    onDrop: e => {
      if (!api_.accepts(e)) {
        return
      }

      e.preventDefault()
      api_.performDrop(readDragKeys(e), section, null)
      setState(null)
    }
  })

  return { api: api_, state, zone }
}

// ─── thread mentions: drag a thread onto the composer, or pick it from @ ────
// A mention is core's `@session:<profile>/<durable id>` chip: the composer
// hydrates the token on insert, the transcript renders it with the thread's
// title, and the agent resolves it with session_search.
const COMPOSER_ROOT_SELECTOR = '[data-slot="composer-root"]'
const threadRef = s => `@session:${profileOf(s)}/${durableOf(s)}`

function threadMentionItems(query) {
  const q = String(query || '').trim().toLowerCase()
  const out = []

  for (const s of cachedRows()) {
    const title = rowTitle(s)

    if (
      q &&
      !title.toLowerCase().includes(q) &&
      !profileOf(s).toLowerCase().includes(q) &&
      !durableOf(s).toLowerCase().includes(q)
    ) {
      continue
    }

    out.push({
      display: title,
      insert: threadRef(s),
      meta: `Thread · ${profileOf(s)}${s.message_count ? ` · ${plural(s.message_count, 'message')}` : ''}`
    })

    if (out.length >= 10) {
      break
    }
  }

  return out
}

function sessionsForDragKeys(keys) {
  const byKeyMap = new Map(cachedRows().map(s => [keyOf(s), s]))

  return keys.map(k => byKeyMap.get(k)).filter(Boolean)
}

/** Insert `@session:` mentions for `sessions` into a composer. `sessionId`
 * addresses that session's composer; null = the active one. */
async function mentionSessions(sessions, sessionId = null) {
  const refs = [...new Set(sessions.filter(Boolean).map(threadRef))]

  if (!refs.length) {
    return false
  }

  if (typeof host.composer?.insertText !== 'function') {
    host.notify({ kind: 'error', message: 'Could not mention the thread: this Hermes build has no composer insert.', title: 'Inbox' })

    return false
  }

  const ok = await host.composer.insertText(sessionId, refs.join(' '), { mode: 'inline' })

  if (!ok) {
    host.notify({ kind: 'error', message: 'Could not mention the thread: no composer is showing for that chat.', title: 'Inbox' })
  }

  return ok
}

/** The session a composer belongs to, from the chat surface hosting it
 * (`tile:<stored id>`); null for the primary/active composer. */
function composerSessionIdFor(root) {
  const target = root?.closest?.('[data-composer-target]')?.dataset?.composerTarget || ''

  return target.startsWith('tile:') ? target.slice('tile:'.length) || null : null
}

/**
 * Let an Inbox drag land on the chat composer. Rows already ride native DnD
 * with DRAG_MIME (for shelf reordering); shelf targets stopPropagation, so a
 * document-level listener only sees drags that leave the pane. Only a drop
 * whose target is inside a composer root is claimed — everything else keeps
 * its existing meaning.
 */
function installComposerThreadDrop(ctx) {
  let highlighted = null
  let savedOutline = null

  const clearHighlight = () => {
    if (highlighted) {
      highlighted.style.outline = savedOutline.outline
      highlighted.style.outlineOffset = savedOutline.outlineOffset
      highlighted = null
      savedOutline = null
    }
  }

  const highlight = root => {
    if (highlighted === root) {
      return
    }

    clearHighlight()
    highlighted = root
    savedOutline = { outline: root.style.outline, outlineOffset: root.style.outlineOffset }
    root.style.outline = '2px solid var(--ui-accent)'
    root.style.outlineOffset = '-2px'
  }

  const accepts = e => Array.from(e.dataTransfer?.types || []).includes(DRAG_MIME)
  const rootFor = e => e.target?.closest?.(COMPOSER_ROOT_SELECTOR) || null

  ctx.addEventListener(document, 'dragover', e => {
    if (!accepts(e)) {
      return
    }

    const root = rootFor(e)

    if (!root) {
      clearHighlight()

      return
    }

    e.preventDefault()
    // Rows set effectAllowed='move'; a mismatched dropEffect suppresses the drop.
    e.dataTransfer.dropEffect = 'move'
    highlight(root)
  })

  ctx.addEventListener(document, 'dragleave', e => {
    if (highlighted && e.target === highlighted && !highlighted.contains(e.relatedTarget)) {
      clearHighlight()
    }
  })

  ctx.addEventListener(document, 'dragend', clearHighlight)

  ctx.addEventListener(document, 'drop', e => {
    if (!accepts(e)) {
      return
    }

    const root = rootFor(e)

    clearHighlight()

    if (!root) {
      return
    }

    e.preventDefault()
    e.stopPropagation()
    const sessions = sessionsForDragKeys(readDragKeys(e))

    if (!sessions.length) {
      host.notify({ kind: 'warning', message: 'That thread is no longer in the Inbox list.', title: 'Inbox' })

      return
    }

    void mentionSessions(sessions, composerSessionIdFor(root))
  })
}

// ─── drafts ─────────────────────────────────────────────────────────────────
// Unsent text typed into a new chat, kept like an email draft. Core already
// stashes it under a per-chat `__new__:<uuid>` key, but starting another new
// chat rotates that key and the dead-session prune can drop the orphan, so the
// text becomes unreachable. We mirror those keys into plugin storage, list
// them in a Drafts shelf, and reopen one by moving its text onto a fresh chat.
const CORE_DRAFTS_KEY = 'hermes:composer-drafts:v3'
const CORE_FRESH_KEY = 'hermes.desktop.freshDraftKey'
const DRAFT_SYNC_EVENT = 'hermes:composer-draft-sync'
const isFreshKey = k => typeof k === 'string' && k.startsWith('__new__')

function readCoreDrafts() {
  try {
    return JSON.parse(window.localStorage.getItem(CORE_DRAFTS_KEY) || '{}') || {}
  } catch {
    return {}
  }
}

function writeCoreDrafts(map) {
  try {
    const raw = Object.keys(map).length ? JSON.stringify(map) : null

    if (raw === null) {
      window.localStorage.removeItem(CORE_DRAFTS_KEY)
    } else {
      window.localStorage.setItem(CORE_DRAFTS_KEY, raw)
    }

    // Same-window writes fire no `storage` event; core's reload listener
    // only checks the key, so a synthetic one resyncs its in-memory stash.
    window.dispatchEvent(new StorageEvent('storage', { key: CORE_DRAFTS_KEY, newValue: raw }))
  } catch {
    // best effort
  }
}

const readFreshKey = () => window.localStorage.getItem(CORE_FRESH_KEY) || '__new__'

function setDrafts(next) {
  $drafts.set(next)
  ctxRef?.storage.set('drafts', next)
}

function syncDrafts() {
  const core = readCoreDrafts()
  const fresh = readFreshKey()
  const prev = $drafts.get()
  const next = { ...prev }
  let changed = false

  if ($freshKey.get() !== fresh) {
    $freshKey.set(fresh)
  }

  const withText = Object.keys(core).filter(k => !isFreshKey(k) && String(core[k] || '').trim()).sort().join('\n')

  if ($sessionDrafts.get() !== withText) {
    $sessionDrafts.set(withText)
  }

  for (const [key, text] of Object.entries(core)) {
    if (!isFreshKey(key) || typeof text !== 'string' || !text.trim()) {
      continue
    }

    if (discarded.has(key)) {
      if (discarded.get(key) === text) {
        continue
      }

      discarded.delete(key)
    }

    const old = prev[key]

    if (!old || old.text !== text) {
      next[key] = {
        at: Date.now(),
        profile: old?.profile || (key === fresh ? currentProfile() : 'default'),
        text
      }
      changed = true
    }
  }

  // The chat being edited lost its text → sent or emptied: the draft is gone.
  // Other keys missing from core were pruned there; our copy stays.
  if (next[fresh] && !(core[fresh] || '').trim()) {
    delete next[fresh]
    changed = true
  }

  if (changed) {
    setDrafts(next)
  }
}

// key → text the user discarded; sync must not resurrect it from core's stash
// if core rewrites the same text before it notices the removal.
const discarded = new Map()

function repaintComposer() {
  window.dispatchEvent(new CustomEvent(DRAFT_SYNC_EVENT, { detail: { mode: 'reload', target: 'main' } }))
}

function discardDraft(key) {
  const draft = $drafts.get()[key]
  const next = { ...$drafts.get() }
  delete next[key]
  setDrafts(next)

  if (draft) {
    discarded.set(key, draft.text)
  }

  const core = readCoreDrafts()

  if (key in core) {
    delete core[key]
    writeCoreDrafts(core)
  }

  // The open new chat holds this text in its editor too: empty it.
  if (draftIsOpen(key)) {
    repaintComposer()
    setTimeout(repaintComposer, 300)
  }
}

function draftIsOpen(key) {
  return $freshKey.get() === key && !host.state.focusedStoredSessionId.get()
}

async function openDraft(key) {
  const draft = $drafts.get()[key]

  if (!draft || draftIsOpen(key)) {
    return
  }

  host.newChat(draft.profile || 'default')

  // newChat rotates the fresh key; wait for it, then move the text onto it.
  let fresh = readFreshKey()

  for (let i = 0; i < 40 && (fresh === key || fresh === $freshKey.get()); i++) {
    await new Promise(r => setTimeout(r, 50))
    fresh = readFreshKey()
  }

  const core = readCoreDrafts()
  delete core[key]
  core[fresh] = draft.text
  writeCoreDrafts(core)

  const next = { ...$drafts.get() }
  delete next[key]
  next[fresh] = draft
  setDrafts(next)
  $freshKey.set(fresh)

  // Repaint the mounted composer from the stash (again once it has settled).
  repaintComposer()
  setTimeout(repaintComposer, 300)
}

function DraftMark() {
  return h(Icon, { className: 't3i-c-warn', name: 'squarePen', size: 13 })
}

function DraftRow({ draftKey, draft, now, active }) {
  const title = draft.text.trim().split('\n')[0].slice(0, 120)
  const discardLabel = 'Discard draft'
  const open = () => void openDraft(draftKey)

  return h(
    'li',
    { className: 't3i-li t3i-card-li', 'data-draft-key': draftKey },
    h(
      'div',
      {
        'aria-current': active ? 'page' : undefined,
        'aria-label': `Draft: ${title}`,
        className: 't3i-row',
        'data-active': active ? '' : undefined,
        onClick: open,
        onKeyDown: e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            open()
          }
        },
        role: 'button',
        tabIndex: 0,
        title: draft.text.slice(0, 500)
      },
      h(
        'div',
        { className: 't3i-card' },
        h(
          'div',
          { className: 't3i-line1' },
          h(DraftMark),
          h(ProfileMark, { profile: draft.profile }),
          h('span', { className: 't3i-proj' }, draft.profile || 'default'),
          h(
            'span',
            { className: 't3i-slot' },
            active ? null : h('span', { className: 't3i-status t3i-dim' }, compactAgo(draft.at, now)),
            h(
              'span',
              { className: 't3i-actions' },
              h(
                Tip,
                { label: discardLabel },
                h(
                  'button',
                  {
                    'aria-label': discardLabel,
                    className: 't3i-act',
                    onClick: e => {
                      e.stopPropagation()
                      discardDraft(draftKey)
                    },
                    style: { marginRight: -4 },
                    type: 'button'
                  },
                  h(Icon, { name: 'x', size: 14 }),
                  'Discard'
                )
              )
            )
          )
        ),
        h('div', { className: 't3i-line2' }, h('span', { className: 't3i-title' }, title)),
        h('div', { className: 't3i-line3' })
      )
    )
  )
}

// ─── header ─────────────────────────────────────────────────────────────────
// Profiles are T3's projects. "New session" mirrors T3's "New thread in…":
// one profile → create directly; several → pick one (Shift+click skips the
// picker and uses the profile you're in, like T3's Shift+click).
function startSession(profile) {
  $newPicker.set(false)
  host.newChat(profile || 'default')
}

function currentProfile() {
  return host.state.focusedSessionProfile?.get?.() || host.state.profile.get() || 'default'
}

function NewSessionPicker({ profiles }) {
  const open = useValue($newPicker)
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const listRef = useRef(null)
  const current = currentProfile()
  const q = query.trim().toLowerCase()
  const filtered = q
    ? profiles.filter(p => p.name.toLowerCase().includes(q) || (p.label || '').toLowerCase().includes(q))
    : profiles
  const active = Math.min(index, Math.max(0, filtered.length - 1))

  useEffect(() => {
    if (open) {
      setQuery('')
      setIndex(0)
    }
  }, [open])

  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const onTriggerClick = e => {
    e.preventDefault()

    if (e.shiftKey || profiles.length <= 1) {
      startSession(e.shiftKey ? current : profiles[0]?.name)

      return
    }

    $newPicker.set(!open)
  }

  return h(
    Popover,
    { onOpenChange: o => $newPicker.set(o), open },
    h(
      Tip,
      {
        label: profiles.length > 1 ? 'New session (Shift+click: in current profile)' : 'New session'
      },
      h(
        PopoverTrigger,
        { asChild: true },
        h(
          'button',
          {
            'aria-label': 'New session',
            className: 't3i-iconbtn',
            'data-active': open ? '' : undefined,
            onClick: onTriggerClick,
            type: 'button'
          },
          h(Icon, { name: 'squarePen', size: 16 })
        )
      )
    ),
    h(
      PopoverContent,
      {
        align: 'end',
        className: 't3i-picker',
        onOpenAutoFocus: e => {
          e.preventDefault()
          e.target?.querySelector?.('input')?.focus()
        },
        side: 'bottom',
        style: { padding: 0, width: 272 }
      },
      h('style', null, PICKER_CSS),
      h(
        'div',
        { className: 't3i-picker-search' },
        h(Icon, { name: 'search', size: 14 }),
        h('input', {
          'aria-label': 'Search profiles',
          autoFocus: true,
          onChange: e => {
            setQuery(e.target.value)
            setIndex(0)
          },
          onKeyDown: e => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setIndex(i => Math.min(i + 1, filtered.length - 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setIndex(i => Math.max(i - 1, 0))
            } else if (e.key === 'Enter' && filtered[active]) {
              e.preventDefault()
              startSession(filtered[active].name)
            }
          },
          placeholder: 'New session in…',
          value: query
        })
      ),
      h(
        'div',
        { className: 't3i-picker-list', ref: listRef, role: 'listbox' },
        filtered.length === 0
          ? h('div', { className: 't3i-picker-empty' }, 'No matching profiles.')
          : filtered.map((p, i) =>
              h(
                'button',
                {
                  'aria-selected': i === active,
                  className: 't3i-picker-item',
                  'data-active': i === active ? '' : undefined,
                  'data-index': i,
                  key: p.name,
                  onClick: () => startSession(p.name),
                  onMouseMove: () => i !== active && setIndex(i),
                  role: 'option',
                  type: 'button'
                },
                h(ProfileMark, { profile: p.name }),
                h('span', { className: 't3i-trunc', style: { flex: 1 } }, p.label || p.name),
                p.name === current ? h('span', { className: 't3i-picker-meta' }, 'current') : null,
                p.count ? h('span', { className: 't3i-picker-meta' }, String(p.count)) : null
              )
            )
      )
    )
  )
}

function Header({ query, setQuery, inputRef, profiles, onSearchKey }) {
  return h(
    'div',
    { className: 't3i-header' },
    h(
      'label',
      { className: 't3i-search' },
      h(Icon, { name: 'search', size: 16 }),
      h('input', {
        'aria-label': 'Search sessions',
        onChange: e => setQuery(e.currentTarget.value),
        onKeyDown: e => {
          if (e.key === 'Escape') {
            setQuery('')
            e.currentTarget.blur()

            return
          }

          onSearchKey(e)
        },
        placeholder: 'Search',
        ref: inputRef,
        type: 'search',
        value: query
      }),
      query
        ? h(
            'button',
            {
              'aria-label': 'Clear search',
              className: 't3i-micro',
              onClick: e => {
                e.preventDefault()
                setQuery('')
                inputRef.current?.focus()
              },
              type: 'button'
            },
            h(Icon, { name: 'x', size: 12 })
          )
        : null
    ),
    h(
      'div',
      { style: { display: 'flex', alignItems: 'center', flexShrink: 0 } },
      h(NewSessionPicker, { profiles })
    )
  )
}

const PICKER_CSS = `
.t3i-picker{font-size:13px;overflow:hidden}
.t3i-picker-search{display:flex;align-items:center;gap:8px;height:36px;padding:0 10px;border-bottom:1px solid var(--ui-stroke-tertiary);color:var(--ui-text-tertiary)}
.t3i-picker-search input{all:unset;flex:1;min-width:0;font-size:13px;color:var(--ui-text-primary)}
.t3i-picker-search input::placeholder{color:var(--ui-text-tertiary)}
.t3i-picker-list{max-height:320px;overflow-y:auto;padding:4px}
.t3i-picker-item{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:8px;width:100%;height:30px;padding:0 8px;border-radius:5px;cursor:pointer;color:var(--ui-text-primary)}
.t3i-picker-item[data-active]{background:var(--ui-row-hover-background)}
.t3i-picker-meta{flex-shrink:0;font-size:11px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.t3i-picker-empty{padding:12px 8px;text-align:center;font-size:12px;color:var(--ui-text-tertiary)}
.t3i-avatar{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:4px;font-size:9px;font-weight:700;flex-shrink:0;text-transform:uppercase;line-height:1}
.t3i-trunc{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
`

// ─── selection bar ──────────────────────────────────────────────────────────
function BulkBar() {
  const sel = useValue($selection)
  const [snoozeOpen, setSnoozeOpen] = useState(false)
  const count = sel.keys.length

  if (count === 0) {
    return null
  }

  const rows = () => selectedRows()
  const allPinned = rows().length > 0 && rows().every(r => r.isPinned)
  const anyUnread = rows().some(r => r.s.unread)
  const act = (label, icon, run) =>
    h(
      Tip,
      { key: label, label },
      h(
        'button',
        {
          'aria-label': label,
          className: 't3i-act',
          onClick: () => {
            run()
          },
          style: { height: 26 },
          type: 'button'
        },
        h(Icon, { name: icon, size: 13 })
      )
    )

  return h(
    'div',
    { 'aria-label': 'Selection actions', className: 't3i-bulk', role: 'toolbar' },
    h('span', { className: 't3i-bulk-count' }, `${count} selected`),
    act('Settle selected', 'check', () => {
      settle(rows())
      clearSelection()
    }),
    h(
      DropdownMenu,
      { onOpenChange: setSnoozeOpen, open: snoozeOpen },
      h(
        Tip,
        { label: 'Snooze selected' },
        h(
          DropdownMenuTrigger,
          { asChild: true },
          h('button', { 'aria-label': 'Snooze selected', className: 't3i-act', style: { height: 26 }, type: 'button' }, h(Icon, { name: 'clock', size: 13 }))
        )
      ),
      h(
        DropdownMenuContent,
        { align: 'end', side: 'bottom' },
        snoozeOpen
          ? h(SnoozeItems, {
              Item: DropdownMenuItem,
              rows: () => {
                const list = rows()
                clearSelection()

                return list
              },
              Separator: DropdownMenuSeparator
            })
          : null
      )
    ),
    act(allPinned ? 'Unpin selected' : 'Pin selected', allPinned ? 'pinOff' : 'pin', () => {
      setPinned(rows(), !allPinned)
      clearSelection()
    }),
    act(anyUnread ? 'Mark selected as read' : 'Mark selected as unread', anyUnread ? 'circleCheck' : 'circleAlert', () => {
      setUnread(rows(), !anyUnread)
      clearSelection()
    }),
    act('Clear selection', 'x', clearSelection)
  )
}

// ─── custom snooze dialog ───────────────────────────────────────────────────
function CustomSnoozeDialog() {
  const pending = useValue($customSnooze)
  const [amount, setAmount] = useState('2')
  const [unit, setUnit] = useState('hours')
  const open = pending !== null
  const n = Number(amount)
  const valid = Number.isFinite(n) && n > 0
  const until = valid ? Date.now() + n * (unit === 'minutes' ? MINUTE : unit === 'hours' ? HOUR : DAY) : null

  const submit = () => {
    if (pending && until) {
      snooze(pending.rows, until)
      $customSnooze.set(null)
    }
  }

  return h(
    Dialog,
    { onOpenChange: o => !o && $customSnooze.set(null), open },
    open
      ? h(
          DialogContent,
          { className: 'max-w-sm' },
          h(
            DialogHeader,
            null,
            h(DialogTitle, null, pending.rows.length > 1 ? `Snooze ${plural(pending.rows.length, 'session')} until…` : 'Snooze until…')
          ),
          h(
            'div',
            { style: { display: 'flex', gap: 8, alignItems: 'center' } },
            h(Input, {
              autoFocus: true,
              min: 1,
              onChange: e => setAmount(e.target.value),
              onKeyDown: e => e.key === 'Enter' && submit(),
              style: { width: 90 },
              type: 'number',
              value: amount
            }),
            h(SegmentedControl, {
              onChange: setUnit,
              options: [
                { label: 'Minutes', id: 'minutes' },
                { label: 'Hours', id: 'hours' },
                { label: 'Days', id: 'days' }
              ],
              value: unit
            })
          ),
          h(
            'div',
            { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } },
            until ? `Wakes ${wakeDescription(until)}` : 'Enter a positive amount'
          ),
          h(
            DialogFooter,
            null,
            h(Button, { onClick: () => $customSnooze.set(null), variant: 'ghost' }, 'Cancel'),
            h(Button, { disabled: !valid, onClick: submit }, 'Snooze')
          )
        )
      : null
  )
}

// ─── search ─────────────────────────────────────────────────────────────────
function useDebounced(value, ms) {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), ms)

    return () => window.clearTimeout(id)
  }, [value, ms])

  return debounced
}

// Local title/metadata matches first, then transcript hits from Hermes' full-text
// index (any age — not just the loaded roster), each with its snippet.
function useSearch(query, groups, profileNames, cx) {
  const q = query.trim().toLowerCase()
  const dq = useDebounced(query.trim(), 250)
  const conn = activeConn()
  const includeCron = Boolean(cx.prefs.includeCron)

  const remote = useQuery({
    enabled: dq.length >= 2 && profileNames.length > 0,
    queryFn: () => searchTranscripts(dq, profileNames, includeCron),
    queryKey: [NS, 'search', conn, includeCron, dq, profileNames.join(',')],
    staleTime: 30_000
  })

  const results = useMemo(() => {
    if (!q) {
      return null
    }

    const local = groups.rows
      .filter(r =>
        [r.s.title, r.s.preview, r.s.profile, r.s.model, r.s.git_branch, r.s.id]
          .filter(Boolean)
          .some(v => String(v).toLowerCase().includes(q))
      )
      .sort((a, b) => b.lastActive - a.lastActive)
      .map(row => ({ row, snippet: null }))

    const out = [...local]
    const byKey = new Map(out.map((e, i) => [e.row.key, i]))
    const loaded = groups.rows

    if (dq.toLowerCase() === q) {
      for (const hit of remote.data?.hits || []) {
        const profile = hit.profile || 'default'
        const ids = [hit.session_id, hit.lineage_root, hit.id].filter(Boolean)
        const known = loaded.find(r => profileOf(r.s) === profile && ids.some(id => sessionIds(r.s).includes(id)))
        const row =
          known ||
          deriveRow(
            {
              _conn: conn,
              _lineage_root_id: hit.lineage_root || undefined,
              id: hit.session_id,
              last_active: hit.last_active || hit.session_started || 0,
              message_count: hit.message_count,
              model: hit.model,
              preview: hit.preview,
              profile,
              source: hit.source,
              title: hit.title
            },
            cx
          )
        const snippet = hit.snippet && !String(hit.snippet).startsWith('Session ID:') ? hit.snippet : null

        if (byKey.has(row.key)) {
          const i = byKey.get(row.key)

          if (!out[i].snippet && snippet) {
            out[i] = { ...out[i], snippet }
          }
        } else {
          byKey.set(row.key, out.length)
          out.push({ row, snippet })
        }
      }
    }

    return out.slice(0, 100)
  }, [groups, q, dq, remote.data, conn, cx])

  return {
    failed: remote.data?.failed || 0,
    pending: Boolean(q) && (dq.toLowerCase() !== q || remote.isFetching),
    results
  }
}

// ─── the pane ───────────────────────────────────────────────────────────────
function InboxPane() {
  const prefs = useValue($prefs)
  const life = useValue($life)
  const live = useValue($live)
  const now = useValue($tick)
  const pinsVersion = useValue($pinsVersion)
  const historyLimit = useValue($historyLimit)
  const conn = useValue(host.state.connectionId) || 'local'
  const focusedId = useValue(host.state.focusedStoredSessionId)
  const drafts = useValue($drafts)
  const freshKey = useValue($freshKey)
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  const [settledShown, setSettledShown] = useState(SETTLED_TAIL_PAGE_COUNT)
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const groupsRef = useRef(null)
  const drag = useDrag(groupsRef)

  const { data, error, isFetching, isLoading } = useQuery({
    placeholderData: previous => previous,
    queryFn: () => fetchSessions(conn, Boolean(prefs.includeCron), historyLimit),
    queryKey: [NS, 'sessions', conn, Boolean(prefs.includeCron), historyLimit],
    // Battery: live turn state has its own poll; the roster only needs to be fresh while looked at.
    refetchInterval: () => (document.hasFocus() ? 15_000 : 60_000),
    staleTime: 5_000
  })

  const sessions = data?.sessions || []
  const total = data?.total ?? null

  const { data: profileData } = useQuery({
    queryFn: () => (host.profiles?.list ? host.profiles.list() : Promise.resolve({ profiles: [] })),
    queryKey: [NS, 'profiles'],
    refetchInterval: 60_000,
    staleTime: 30_000
  })

  // Profiles = T3 projects, most recently active first (like T3's picker).
  const profiles = useMemo(() => {
    const byName = new Map()
    const entry = name => {
      if (!byName.has(name)) {
        byName.set(name, { count: 0, label: null, last: 0, name })
      }

      return byName.get(name)
    }

    for (const p of profileData?.profiles || []) {
      if (p.role === 'setup') {
        continue
      }

      entry(p.name).label = p.display_name || null
    }

    for (const s of sessions) {
      const e = entry(s.profile || 'default')
      e.count += 1
      e.last = Math.max(e.last, s.last_active || s.started_at || 0)
    }

    return [...byName.values()].sort((a, b) => b.last - a.last || a.name.localeCompare(b.name))
  }, [profileData, sessions])

  // Pin order only changes on pin edges (ours, another window's, or a roster page).
  const pins = useMemo(() => {
    const flagged = new Set()

    for (const s of sessions) {
      if (s.pinned) {
        sessionIds(s).forEach(id => flagged.add(id))
      }
    }

    return readPinOrder(flagged)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions, pinsVersion])

  const groups = useMemo(
    () => classify(sessions, { focusedId, life, live, now, pins, prefs }),
    [sessions, life, live, now, prefs, focusedId, pins]
  )

  groupsRef.current = groups
  lastGroups = groups

  // Wake snoozed rows exactly on time, not on the next coarse tick.
  useEffect(() => {
    const next = Math.min(...groups.snoozed.map(r => r.snoozeUntil).filter(u => u > Date.now()))

    if (!Number.isFinite(next)) {
      return undefined
    }

    const id = window.setTimeout(() => $tick.set(Date.now()), Math.min(next - Date.now() + 50, 2 ** 31 - 1))

    return () => window.clearTimeout(id)
  }, [groups.snoozed])

  const searchCx = useMemo(() => ({ focusedId, life, live, now, pinOrder: new Map(pins.map((id, i) => [id, i])), prefs }), [focusedId, life, live, now, pins, prefs])
  const profileNames = useMemo(() => profiles.map(p => p.name), [profiles])
  const search = useSearch(query, groups, profileNames, searchCx)
  const results = search.results

  // A new search scope drops the selection and restarts keyboard navigation.
  useEffect(() => {
    clearSelection()
    setHighlight(0)
  }, [query])

  useEffect(() => {
    listRef.current?.querySelector('[data-highlight]')?.scrollIntoView({ block: 'nearest' })
  }, [highlight])

  // Search keyboard navigation keeps focus in the input (T3).
  const onSearchKey = e => {
    if (!results || results.length === 0) {
      return
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight(i => Math.min(i + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight(i => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      const hit = results[Math.min(highlight, results.length - 1)]

      if (hit) {
        e.preventDefault()
        openRow(hit.row, e.metaKey || e.ctrlKey ? 'tab' : undefined)
      }
    }
  }

  const toggle = name => setPrefs({ expanded: { ...prefs.expanded, [name]: !prefs.expanded[name] } })
  const dragState = drag.state
  const dragging = dragState !== null
  const over = dragState?.overSection ?? null
  const draggingKeys = new Set(dragState?.keys || [])
  const { pinned, inbox, working, snoozed, settled } = groups
  const items = []
  const visible = []
  const card = (r, section) => {
    visible.push(r.key)

    return h(CardRow, {
      drag: drag.api,
      dragging: draggingKeys.has(r.key),
      dropBefore: dragState?.overKey === r.key && dragState?.overSection === 'pinned' && section === 'pinned',
      key: r.key,
      now,
      row: r,
      section
    })
  }
  const slim = (r, section) => {
    visible.push(r.key)

    return h(SlimRow, { drag: drag.api, dragging: draggingKeys.has(r.key), key: r.key, now, row: r, section })
  }

  if (results) {
    if (results.length === 0) {
      items.push(h('div', { className: 't3i-empty', key: 'none' }, search.pending ? 'Searching…' : 'No matching sessions'))
    } else {
      results.forEach((hit, i) =>
        items.push(
          h(SlimRow, {
            drag: null,
            highlighted: i === Math.min(highlight, results.length - 1),
            key: hit.row.key,
            now,
            row: hit.row,
            section: 'search',
            snippet: hit.snippet
          })
        )
      )
    }

    items.push(
      h(
        'li',
        { className: 't3i-li', key: 'search-note' },
        h(
          'div',
          { className: 't3i-search-note' },
          search.pending
            ? 'Searching transcripts…'
            : search.failed
              ? `Transcript search unavailable for ${plural(search.failed, 'profile')}`
              : '↑↓ to move · Enter to open · ⌘Enter in a new tab'
        )
      )
    )
  } else {
    // New-chat drafts lead the list, split from the rest by a rule (like T3).
    // The chat being typed into right now gets NO row — a fresh typing session
    // was never left, so there is nothing to surface back to the user (T3 Code
    // hides never-left drafts the same way). The row appears the moment the
    // draft is left: the fresh key rotates to another new chat, or a session
    // takes focus. Text keeps syncing underneath either way.
    const draftList = Object.entries(drafts)
      .filter(([k]) => !(freshKey === k && !focusedId))
      .sort((a, b) => b[1].at - a[1].at)

    draftList.forEach(([k, d]) =>
      items.push(h(DraftRow, { active: freshKey === k && !focusedId, draft: d, draftKey: k, key: `draft:${k}`, now }))
    )

    if (draftList.length > 0 && !dragging) {
      items.push(h('li', { className: 't3i-li', key: 'dd' }, h('div', { className: 't3i-draft-rule' })))
    }

    // Pinned block: no header at rest, a labelled boundary while dragging.
    if (dragging) {
      items.push(h(DragBoundary, { dropProps: drag.zone('pinned'), key: 'pb', label: 'Pinned', target: over === 'pinned' }))
    }

    pinned.forEach(r => items.push(card(r, 'pinned')))

    if (dragging && pinned.length === 0) {
      items.push(h(DropHint, { dropProps: drag.zone('pinned'), key: 'ph', label: 'Pinned', target: over === 'pinned' }))
    }

    if (dragging) {
      items.push(h(DragBoundary, { dropProps: drag.zone('inbox'), key: 'ib', label: 'Active', target: over === 'inbox' }))
    } else if (pinned.length > 0 && inbox.length > 0) {
      items.push(h('li', { className: 't3i-li', key: 'pd', style: { height: 6 } }))
    }

    inbox.forEach(r => items.push(card(r, 'inbox')))

    if (dragging && (inbox.length === 0 || (dragState.from === 'inbox' && inbox.length === dragState.keys.length))) {
      items.push(h(DropHint, { dropProps: drag.zone('inbox'), key: 'ih', label: 'Active', target: over === 'inbox' }))
    }

    if (!dragging && pinned.length + inbox.length === 0 && sessions.length > 0) {
      items.push(
        h(
          'li',
          { className: 't3i-li', key: 'zero' },
          h('div', { className: 't3i-empty' }, h(Icon, { name: 'circleCheck', size: 18 }), 'Inbox zero')
        )
      )
    }

    let pushed = false
    const push = () => {
      const was = pushed
      pushed = true

      return was ? undefined : 't3i-push'
    }

    if (working.length > 0) {
      items.push(
        h(ShelfHeader, {
          className: push(),
          count: working.length,
          expanded: prefs.expanded.working,
          key: 'wh',
          label: 'Working',
          onToggle: () => toggle('working')
        })
      )

      if (prefs.expanded.working) {
        working.forEach(r => items.push(card(r, 'working')))
      }
    }

    if (snoozed.length > 0) {
      items.push(
        h(ShelfHeader, {
          className: push(),
          count: snoozed.length,
          expanded: prefs.expanded.snoozed,
          key: 'sh',
          label: 'Snoozed',
          onToggle: () => toggle('snoozed'),
          tone: 'info'
        })
      )

      if (prefs.expanded.snoozed) {
        snoozed.forEach(r => items.push(slim(r, 'snoozed')))
      }
    }

    const moreOnServer = total !== null && total > sessions.length

    if (settled.length > 0 || dragging || moreOnServer) {
      items.push(
        h(ShelfHeader, {
          className: push(),
          count: moreOnServer ? `${settled.length}+` : settled.length,
          dropProps: drag.zone('settled'),
          expanded: prefs.expanded.settled,
          key: 'seh',
          label: 'Settled',
          onToggle: () => toggle('settled'),
          tone: over === 'settled' ? 'accent' : dragging ? 'emph' : undefined
        })
      )

      if (dragging && (settled.length === 0 || !prefs.expanded.settled)) {
        items.push(
          h(DropHint, { dropProps: drag.zone('settled'), key: 'seph', label: 'Settled', target: over === 'settled' })
        )
      }

      if (prefs.expanded.settled) {
        settled.slice(0, settledShown).forEach(r => items.push(slim(r, 'settled')))
        const hidden = settled.length - settledShown

        // Past the loaded rows, "Show more" fetches older history from the backend.
        if (hidden > 0 || moreOnServer) {
          const loading = hidden <= 0 && isFetching
          items.push(
            h(
              'li',
              { className: 't3i-li', key: 'more' },
              h(
                'button',
                {
                  'aria-busy': loading,
                  className: 't3i-more',
                  onClick: () => {
                    if (hidden > 0) {
                      setSettledShown(n => n + SETTLED_TAIL_PAGE_COUNT)
                    } else if (!loading) {
                      setSettledShown(n => n + SETTLED_TAIL_PAGE_COUNT)
                      $historyLimit.set($historyLimit.get() + HISTORY_PAGE)
                    }
                  },
                  type: 'button'
                },
                h(Icon, { className: loading ? 't3i-spin' : undefined, name: loading ? 'refresh' : 'plus', size: 16 }),
                hidden > 0 ? `Show ${Math.min(hidden, SETTLED_TAIL_PAGE_COUNT)} more` : loading ? 'Loading older sessions…' : 'Load older sessions'
              )
            )
          )
        }
      }
    }
  }

  lastVisibleKeys = visible

  const offline = Object.entries(live.owners).filter(([, o]) => o.stale).map(([owner]) => owner.split('|')[1])
  let body

  if (isLoading && !data) {
    body = h('div', { className: 't3i-empty' }, 'Loading sessions…')
  } else if (error && !data) {
    body = h(
      'div',
      { className: 't3i-empty' },
      'Could not load sessions',
      h('button', { className: 't3i-act', onClick: refresh, type: 'button' }, 'Retry')
    )
  } else if (sessions.length === 0 && Object.keys(drafts).length === 0) {
    body = h(
      'div',
      { className: 't3i-empty' },
      'No sessions yet',
      h(
        'button',
        { className: 't3i-act', onClick: () => (profiles.length > 1 ? $newPicker.set(true) : startSession(profiles[0]?.name)), type: 'button' },
        h(Icon, { name: 'plus', size: 12 }),
        'New session'
      )
    )
  } else {
    body = h('ul', { className: 't3i-list', ref: listRef, role: results ? 'listbox' : 'list' }, items)
  }

  return h(
    'div',
    {
      className: 't3i-root',
      'data-t3-inbox': '',
      onKeyDown: e => {
        if (e.key === 'Escape' && $selection.get().keys.length && !e.target.closest?.('input')) {
          clearSelection()
        }
      }
    },
    h('style', null, CSS),
    h(Header, { inputRef, onSearchKey, profiles, query, setQuery }),
    h(BulkBar, null),
    offline.length
      ? h(
          'div',
          { className: 't3i-offline', role: 'status' },
          h(Icon, { name: 'cloudOff', size: 12 }),
          `Live status unavailable (${[...new Set(offline)].join(', ')}) — showing last known`
        )
      : error && data
        ? h('div', { className: 't3i-offline', role: 'status' }, h(Icon, { name: 'cloudOff', size: 12 }), 'Session list is stale — retrying')
        : null,
    h('div', { className: 't3i-scroll' }, body),
    h(CustomSnoozeDialog, null)
  )
}

// ─── settings page ──────────────────────────────────────────────────────────
function SettingsPage() {
  const prefs = useValue($prefs)
  const life = useValue($life)
  const sync = useValue($sync)
  const records = Object.values(life.records)
  const counts = {
    settled: records.filter(r => r.settledAt && !(r.unsettledAt > r.settledAt)).length,
    snoozed: records.filter(r => r.snoozeUntil && r.snoozeUntil > Date.now()).length
  }
  const where = !prefs.syncLifecycle
    ? 'Stored on this device only.'
    : sync.state === 'synced'
      ? 'Shared through this Hermes backend with every window and device that uses it.'
      : sync.state === 'unavailable'
        ? 'Stored on this device — the T3 Code backend is not loaded yet (restart the backend to share it).'
        : sync.state === 'error'
          ? `Stored on this device — last sync failed${sync.error ? `: ${sync.error}` : ''}.`
          : 'Stored on this device.'

  return h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
    h(ToggleRow, {
      checked: prefs.workingShelf,
      description:
        'Sessions with a running turn or delegated background work fold into a Working shelf, ordered by your last message, like T3 Code’s Working beta. The inbox then orders by when work came back to you.',
      label: 'Working shelf',
      onChange: v => setPrefs({ workingShelf: v })
    }),
    h(ListRow, {
      description:
        'Sessions with no activity for this long settle on their own. Anything you un-settle, pin, that is still working, failed, or has unread replies stays in the inbox.',
      title: 'Auto-settle idle sessions',
      action: h(SegmentedControl, {
        onChange: v => setPrefs({ autoSettleDays: Number(v) }),
        options: [
          { label: 'Off', id: '0' },
          { label: '1d', id: '1' },
          { label: '3d', id: '3' },
          { label: '7d', id: '7' },
          { label: '14d', id: '14' }
        ],
        value: String(prefs.autoSettleDays)
      })
    }),
    h(ToggleRow, {
      checked: prefs.includeCron,
      description: 'Show scheduled-job runs in the inbox (the core sidebar keeps them in Scheduled jobs).',
      label: 'Include cron sessions',
      onChange: v => {
        setPrefs({ includeCron: v })
        refresh()
      }
    }),
    h(ToggleRow, {
      checked: prefs.resumeAfterCrash,
      description:
        'When Hermes reopens, threads that were mid-turn when the app or backend stopped continue on their own, like T3 Code’s “Continue threads after server update”. Settled, snoozed and archived threads are left alone, and a thread is restarted at most three times in a row. While the Orchestrator’s “Continue threads after restarts” is on, the Orchestrator handles this (it also covers updates and quits).',
      label: 'Resume interrupted threads on launch',
      onChange: v => setPrefs({ resumeAfterCrash: v })
    }),
    h(ToggleRow, {
      checked: prefs.autoResumeLimited,
      description:
        'A thread stopped by a usage limit (Claude Code, Codex, any provider whose reply says when it resets) continues as soon as the limit lifts. Threads without a reset time retry after 20 minutes, at most eight times.',
      label: 'Auto-resume usage-limited threads',
      onChange: v => setPrefs({ autoResumeLimited: v })
    }),
    h(ToggleRow, {
      checked: prefs.snoozeLimited,
      description: 'Snooze a usage-limited thread until its limit resets, so it leaves the inbox and comes back when it can run.',
      label: 'Snooze usage-limited threads',
      onChange: v => setPrefs({ snoozeLimited: v })
    }),
    h(ToggleRow, {
      checked: prefs.syncLifecycle,
      description: 'Keep settle, snooze and Woke state in this plugin’s backend so every Hermes window and device agrees.',
      label: 'Share lifecycle across devices',
      onChange: v => {
        setPrefs({ syncLifecycle: v })

        if (v) {
          $sync.set({ at: 0, error: null, state: 'local' })
          void pullLifecycle()
        }
      }
    }),
    h(ListRow, {
      description: `${counts.settled} settled by hand, ${counts.snoozed} snoozed. ${where}`,
      title: 'Reset lifecycle state',
      action: h(
        Button,
        {
          onClick: () => {
            const conn = activeConn()
            // Clear every record with a fresh write so the reset syncs too.
            writeRecs(Object.fromEntries(Object.keys($life.get().records).filter(k => k.startsWith(`${conn}|`)).map(k => [k, { settledAt: null, settledCount: null, settledLast: null, snoozeAt: null, snoozeUntil: null, unsettledAt: null, visitedAt: null }])))
            setLife({ ...$life.get(), legacy: null })
          },
          size: 'sm',
          variant: 'outline'
        },
        'Reset'
      )
    })
  )
}

// ─── registration ───────────────────────────────────────────────────────────
export default {
  id: 'inbox',
  name: 'Inbox',
  register(ctx) {
    ctxRef = ctx
    $prefs = atom({ ...DEFAULT_PREFS, ...ctx.storage.get('prefs', {}) })
    $prefs.set({ ...$prefs.get(), expanded: { ...DEFAULT_PREFS.expanded, ...($prefs.get().expanded || {}) } })
    $life = atom(loadLife(ctx.storage.get('life', null)))
    $live = atom({ owners: {} })
    $tick = atom(Date.now())
    $pinsVersion = atom(0)
    $customSnooze = atom(null)
    $newPicker = atom(false)
    $drafts = atom({ ...ctx.storage.get('drafts', {}) })
    $freshKey = atom(null)
    $sessionDrafts = atom('')
    $selection = atom({ anchor: null, keys: [] })
    $historyLimit = atom(HISTORY_PAGE)
    $sync = atom({ at: 0, error: null, state: 'local' })
    workingSince = new Map()
    pinOverlay = new Map()
    rowCache = new Map()
    dirtyKeys = new Set()
    openedProfiles = new Set()
    pendingLookups = new Set()
    crashTried = new Set()
    recoveryInflight = new Set()
    recoveryBusy = false
    lastInterruptedAt = 0
    lastLiveOk.clear()
    lastGroups = null
    lastVisibleKeys = []
    connGeneration += 1

    // Coarse clock for relative ages, snooze expiry and auto-settle; working
    // timers tick themselves and snooze wakes are scheduled exactly.
    ctx.setInterval(() => $tick.set(Date.now()), 15_000)
    // Live turn state for the sources we can reach: every 3 s focused, 10 s unfocused, 60 s hidden
    // (each poll is two calls per opened profile). Hidden still polls so recovery sees fresh state.
    let polledAt = 0
    ctx.setInterval(() => {
      const every = document.hidden ? 60_000 : document.hasFocus() ? 0 : 10_000
      if (Date.now() - polledAt >= every) {
        polledAt = Date.now()
        void pollLive()
      }
    }, 3_000)
    void pollLive()
    // Resume threads a crash or a usage limit stopped (see "recovery" above). Limits reset on
    // the hour scale, so 30 s is plenty.
    ctx.setInterval(() => void runRecovery(), 30_000)
    // Shared lifecycle: pull on load, then periodically and on return to the window.
    void pullLifecycle()
    ctx.setInterval(() => !document.hidden && void pullLifecycle(), 60_000)
    ctx.addEventListener(document, 'visibilitychange', () => {
      if (!document.hidden) {
        $tick.set(Date.now())
        void pollLive()
        void pullLifecycle()
      }
    })
    // Mirror unsent new-chat text into the Drafts shelf. Only typing changes it, so poll only while
    // focused and catch the last keystrokes on blur.
    ctx.setInterval(() => document.hasFocus() && syncDrafts(), 2_000)
    ctx.addEventListener(window, 'blur', () => syncDrafts())
    syncDrafts()

    // A different connection invalidates what we knew about the old one. A
    // profile swap on the same connection does not: live state is keyed per
    // owner (conn|profile), and New thread / opening another profile's thread
    // flips the active profile, so wiping here blanked every working row until
    // the next poll. Keep it, keep polling the profile we left, poll now.
    let lastConn = activeConn()
    let lastProfile = activeProfile()
    const onSource = () => {
      const conn = activeConn()
      const profile = activeProfile()

      if (conn !== lastConn) {
        lastConn = conn
        lastProfile = profile
        resetSourceState()
      } else if (profile !== lastProfile) {
        if (conn === 'local') {
          openedProfiles.add(lastProfile)
        }

        lastProfile = profile
        void pollLive()
      }
    }
    ctx.onDispose(host.state.connectionId.subscribe(onSource))
    ctx.onDispose(host.state.profile.subscribe(onSource))
    ctx.onDispose(
      host.state.gateway.subscribe(state => {
        if (state === 'open') {
          lastInterruptedAt = 0
          void pollLive()
        }
      })
    )
    ctx.onDispose(() => clearTimeout(syncTimer))

    // Refetch on session lifecycle and completed turns (debounced).
    let pending = null
    const bump = () => {
      pending?.()
      pending = ctx.setTimeout(() => {
        pending = null
        refresh()
        void pollLive()
      }, 600)
    }

    ctx.onEvent('*', ev => {
      const type = ev?.type || ''

      if (
        type === 'message.start' ||
        type === 'message.complete' ||
        type === 'approval.pending' ||
        type === 'approval.received' ||
        type === 'subagent.start' ||
        type === 'subagent.complete' ||
        type.startsWith('session.')
      ) {
        bump()
      }
    })

    // Pins written in another window.
    ctx.addEventListener(window, 'storage', e => {
      if (!e.key || e.key.startsWith(CORE_PINS_KEY)) {
        $pinsVersion.set($pinsVersion.get() + 1)
      }
    })

    // Opening a session anywhere acknowledges its Woke / failure.
    ctx.onDispose(
      host.state.focusedStoredSessionId.subscribe(id => {
        if (!id) {
          return
        }

        const profile = host.state.focusedSessionOwner?.get?.()?.profile
        const s = cachedRows().find(r => sessionIds(r).includes(id) && (!profile || profileOf(r) === profile))

        if (s) {
          if (connOf(s) === 'local') {
            openedProfiles.add(profileOf(s))
          }

          markVisited(keyOf(s))
        }
      })
    )

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Inbox',
      data: {
        placement: 'left',
        width: '300px',
        collapsible: true,
        tabTitleText: () => 'Inbox',
        dock: { pane: 'sessions', pos: 'center' }
      },
      render: () => h(InboxPane, null)
    })

    // Mention a thread without leaving the chat: drop an Inbox row on the
    // composer, or type @ and pick the thread from the completion popover.
    installComposerThreadDrop(ctx)
    ctx.register({
      area: COMPOSER_AREAS?.atCompletions || 'composer.atCompletions',
      data: { provide: query => threadMentionItems(query) },
      id: 'thread-mentions'
    })

    const focusedRow = () => {
      const id = host.state.focusedStoredSessionId.get()

      return id ? groupsForCommands().rows.find(r => isFocused(r)) || null : null
    }

    ctx.registerMany([
      {
        id: 'open',
        area: 'palette',
        data: {
          id: `${NS}.open`,
          label: 'Show Inbox',
          keywords: ['inbox', 't3', 'sessions', 'threads'],
          run: () => host.revealPane(PANE_ID)
        }
      },
      {
        id: 'settle-focused',
        area: 'palette',
        data: {
          id: `${NS}.settle-focused`,
          label: 'Settle current session',
          keywords: ['inbox', 'settle', 'done'],
          run: () => {
            const row = focusedRow()

            if (row) {
              settle(row)
            }
          }
        }
      },
      {
        id: 'snooze-focused',
        area: 'palette',
        data: {
          id: `${NS}.snooze-focused`,
          label: 'Snooze current session for an hour',
          keywords: ['inbox', 'snooze', 'later'],
          run: () => {
            const row = focusedRow()

            if (row) {
              snooze(row, Date.now() + HOUR)
            }
          }
        }
      },
      {
        id: 'new-in',
        area: 'palette',
        data: {
          id: `${NS}.new-in`,
          label: 'New session in…',
          keywords: ['new', 'session', 'profile', 'inbox'],
          run: () => {
            host.revealPane(PANE_ID)
            ctx.setTimeout(() => $newPicker.set(true), 50)
          }
        }
      },
      {
        id: 'refresh',
        area: 'palette',
        data: { id: `${NS}.refresh`, label: 'Refresh Inbox', keywords: ['inbox'], run: refresh }
      }
    ])

    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Inbox',
      icon: 'inbox',
      render: () => h(SettingsPage, null)
    })

    // Front the Inbox tab once, the first time the plugin loads.
    if (!$prefs.get().revealed) {
      ctx.setTimeout(() => {
        try {
          host.revealPane(PANE_ID)
        } catch {}

        setPrefs({ revealed: true })
      }, 800)
    }
  }
}

// Palette commands run outside the pane; classify from the cached rows.
function groupsForCommands() {
  if (lastGroups) {
    return lastGroups
  }

  const sessions = cachedRows()
  const flagged = new Set(sessions.filter(s => s.pinned).flatMap(sessionIds))

  return classify(sessions, {
    focusedId: host.state.focusedStoredSessionId.get(),
    life: $life.get(),
    live: $live.get(),
    now: Date.now(),
    pins: readPinOrder(flagged),
    prefs: $prefs.get()
  })
}
