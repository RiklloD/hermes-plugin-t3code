/**
 * Battery saver: stop Hermes redrawing at the display's refresh rate.
 *
 * Measured 22.10.2026 on a 120 Hz MacBook during a running turn: the core "Working" shimmer, the
 * core glyph spinner and the inbox row spinner kept the renderer at 38 %, the GPU process at 22 % and
 * WindowServer at 38 % of a core. Pausing all three dropped that to 4 %, 0.7 % and 7 %. Any single
 * infinite animation keeps the whole frame pipeline running, so all of them have to stop together.
 * Core only pauses them when the window is hidden or minimized, not when it sits visible behind
 * another app.
 *
 * Three modes:
 *  - live: on AC power and in use. Animations run untouched.
 *  - capped: on battery and in use. Every running infinite CSS animation is held (CSS play-state,
 *    so core's own pause rules keep working) and stepped forward every CAPPED_MS: 10 fps instead
 *    of 120, which still reads as motion.
 *  - idle: the window lost focus, is hidden, or got no input for IDLE_MS. Same hold, stepped once
 *    per IDLE_MS_BEAT so spinners still move instead of looking hung.
 * Any input or focus returns to live/capped at once. Finite animations (fades, pops) are never touched.
 */
const HOLD_ATTR = 'data-t3-battery-hold'
const IDLE_MS = 30_000
const IDLE_BEAT_MS = 1_000
const CAPPED_MS = 100
const CHECK_MS = 5_000

const CSS = `[${HOLD_ATTR}],[${HOLD_ATTR}]::before,[${HOLD_ATTR}]::after{animation-play-state:paused!important}`

let held = new Set()
let mode = 'live'
let onBattery = false
let lastInput = Date.now()
let beatTimer = 0

const isLooping = a => a.effect?.getTiming?.().iterations === Infinity && a.effect?.target?.setAttribute

function holdRunning() {
  for (const a of document.getAnimations()) {
    if (a.playState === 'running' && isLooping(a) && !held.has(a)) {
      a.effect.target.setAttribute(HOLD_ATTR, '')
      held.add(a)
    }
  }
}

// Seeking keeps CSS in charge of play-state (pause()/play() would take it over for good).
function step(ms) {
  for (const a of held) {
    if (!a.effect?.target?.isConnected || a.playState === 'idle') {
      held.delete(a)
    } else if (a.playState === 'paused') {
      a.currentTime = (a.currentTime || 0) + ms
    }
  }
  holdRunning()
}

function release() {
  for (const a of held) {
    a.effect?.target?.removeAttribute?.(HOLD_ATTR)
  }
  held = new Set()
}

// One timer, re-armed per beat, so live mode and hidden windows cost no wakeups at all.
function scheduleBeat() {
  window.clearTimeout(beatTimer)
  beatTimer = 0

  if (mode === 'live' || document.hidden) {
    return
  }

  const ms = mode === 'capped' ? CAPPED_MS : IDLE_BEAT_MS

  beatTimer = window.setTimeout(() => {
    step(ms)
    scheduleBeat()
  }, ms)
}

function nextMode() {
  if (document.hidden || !document.hasFocus() || Date.now() - lastInput > IDLE_MS) {
    return 'idle'
  }

  return onBattery ? 'capped' : 'live'
}

function update() {
  const next = nextMode()

  if (next !== mode) {
    mode = next
    mode === 'live' ? release() : holdRunning()
  }

  scheduleBeat()
}

function onInput() {
  lastInput = Date.now()

  if (mode === 'idle') {
    update()
  }
}

export default {
  register(ctx) {
    held = new Set()
    mode = 'live'
    onBattery = false
    lastInput = Date.now()

    const style = document.createElement('style')
    style.dataset.t3Battery = ''
    style.textContent = CSS
    document.head.appendChild(style)

    const desktop = window.hermesDesktop
    const setPower = value => {
      onBattery = Boolean(value)
      update()
    }

    void desktop?.getOnBattery?.().then(setPower, () => {})
    const unsubscribePower = desktop?.onBatteryChanged?.(setPower)

    ctx.onDispose(() => {
      window.clearTimeout(beatTimer)
      unsubscribePower?.()
      release()
      style.remove()
    })

    for (const type of ['pointerdown', 'pointermove', 'keydown', 'wheel']) {
      ctx.addEventListener(window, type, onInput, { capture: true, passive: true })
    }
    ctx.addEventListener(window, 'focus', onInput)
    ctx.addEventListener(window, 'blur', update)
    ctx.addEventListener(document, 'visibilitychange', update)
    ctx.setInterval(update, CHECK_MS)
    update()
  }
}
