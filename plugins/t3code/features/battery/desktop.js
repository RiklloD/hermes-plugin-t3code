/**
 * Battery saver: stop Hermes redrawing at the display's refresh rate while nobody is looking.
 *
 * Measured 22.10.2026 on a 120 Hz MacBook during a running turn: the core "Working" shimmer, the
 * core glyph spinner and the inbox row spinner kept the renderer at 38 %, the GPU process at 22 % and
 * WindowServer at 38 % of a core. Pausing all three dropped that to 4 %, 0.7 % and 7 %. Any single
 * infinite animation keeps the whole frame pipeline running, so all of them have to stop together.
 * Core only pauses them when the window is hidden or minimized, not when it sits visible behind
 * another app.
 *
 * Idle = the window lost focus, is hidden, or got no input for IDLE_MS. While idle every running
 * infinite CSS animation is held (CSS play-state, so core's own pause rules keep working) and stepped
 * forward once per BEAT_MS: spinners still move at 1 fps instead of looking hung. Any input or focus
 * releases them at once. Finite animations (fades, pops, entrances) are never touched.
 */
const HOLD_ATTR = 'data-t3-battery-hold'
const IDLE_MS = 30_000
const BEAT_MS = 1_000
const CHECK_MS = 5_000

const CSS = `[${HOLD_ATTR}],[${HOLD_ATTR}]::before,[${HOLD_ATTR}]::after{animation-play-state:paused!important}`

let held = new Set()
let idle = false
let lastInput = Date.now()

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
function beat() {
  for (const a of held) {
    if (!a.effect?.target?.isConnected || a.playState === 'idle') {
      held.delete(a)
    } else if (a.playState === 'paused') {
      a.currentTime = (a.currentTime || 0) + BEAT_MS
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

function shouldIdle() {
  return document.hidden || !document.hasFocus() || Date.now() - lastInput > IDLE_MS
}

function update() {
  const next = shouldIdle()

  if (next === idle) {
    return
  }

  idle = next
  idle ? holdRunning() : release()
}

function onInput() {
  lastInput = Date.now()

  if (idle) {
    update()
  }
}

export default {
  register(ctx) {
    held = new Set()
    idle = false
    lastInput = Date.now()

    const style = document.createElement('style')
    style.dataset.t3Battery = ''
    style.textContent = CSS
    document.head.appendChild(style)
    ctx.onDispose(() => {
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
    ctx.setInterval(() => idle && beat(), BEAT_MS)
    update()
  }
}
