# T3 Code · Usage

T3 Code-inspired usage tracking in the existing **Command Center → Usage** tab.
Open it with Hermes' normal bottom-left Command Center button; no extra sidebar
row or statusbar button is added.

- **Tokens / Cost:** Claude Code and Codex transcript usage, plus Hermes' own
  Codex usage, Antigravity (conversation databases, T3's protobuf reader) and
  Muse Code (`model_completed` events in its session journals), with 24H / 7D /
  30D / 90D charts and model/time breakdowns.
- **Limits:** live Claude Code, Codex, Z.ai and Antigravity subscription windows
  (Antigravity: Google's quota summary for the first signed-in profile, Hermes's
  own then T3 Code's, as in T3), and Muse Code's 5-hour / weekly windows as last
  pushed during a Muse Code turn (Muse keeps no copy, so the card says "as of"),
  remaining percentages, reset countdowns and pace indicators. Costs are API-equivalent
  estimates, not subscription charges. Unknown model prices remain unpriced.

Backend: `features/usage/api.py` (+ `native.py` for Antigravity and Muse Code), served under `/api/plugins/t3code/usage/`
with `/usage?days=N` and `/limits`. Desktop: `features/usage/desktop.js`
(bundled into `desktop/plugin.js` by `scripts/build.mjs`).

## Integration

The current desktop SDK has no Command Center content slot. A hidden mount
moves the plugin panel into the Usage tab, leaving the normal navigation intact.
A `MutationObserver` catches the overlay's DOM commit before browser paint;
`useLayoutEffect` handles initial mounting. The hiding stylesheet is installed
before activation, avoiding a flash of the stock panel. No UI polling interval
is used.

This is the only usage page in T3 Code (the Inbox's old copy was removed in the
merge). Cleanup
returns moved nodes and disposes listeners/observers on hot reload.

Plugin files live outside the Hermes source checkout. The DOM integration still
depends on the app's overlay structure; updates should be smoke-tested rather
than assumed compatible. Backend changes require a backend restart; desktop
changes hot-reload.

## Verification

A React + jsdom regression check exercises the actual desktop component with a
mock SDK, navigation by `pushState`, and a first-frame assertion:

```bash
node ~/.hermes/plugins/t3code/features/usage/tests/desktop/flicker.cjs
```

It resolves React, jsdom and esbuild from the local Hermes source tree. Set
`HERMES_SOURCE_ROOT` and `T3_USAGE_DESKTOP_FILE` for nonstandard paths.

Expected: stock panel hidden before first paint, normal content restored when
switching away, and no duplicate panels after reopening. Subscription values
were also read back in the running desktop app.
