<script setup lang="ts">
/**
 * Visor astronómico WebGL.
 *
 * El estiramiento (asinh / STF, con punto negro, punto blanco y gamma) se hace
 * **en el fragment shader**: la textura se sube una sola vez y mover un control
 * es un repintado, no una re-descarga ni un recorrido de píxeles en CPU.
 *
 * Accesibilidad: el canvas es focalizable y se maneja entero con teclado
 * (flechas = paneo, + / − = zoom, 0 = ajustar, G = rejilla). En móvil hay
 * pellizco para zoom y arrastre para paneo. Si no hay WebGL, degrada a <img>
 * con un aviso explícito.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDec, formatRa } from '~/lib/astro'
import type { Astrometry } from '~/types/domain'

const props = withDefaults(
  defineProps<{
    src: string
    title?: string
    astrometry?: Astrometry | null
    /** Vista previa ya estirada, para el modo degradado. */
    fallbackSrc?: string | null
    heightClass?: string
  }>(),
  { title: '', astrometry: null, fallbackSrc: null, heightClass: 'h-[70vh] min-h-[320px]' },
)

const { t } = useI18n()

const container = ref<HTMLDivElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const overlay = ref<HTMLCanvasElement | null>(null)

const webglAvailable = ref(true)
const loading = ref(true)
const loadError = ref(false)
const showGrid = ref(false)

const blackPoint = ref(0)
const whitePoint = ref(1)
const gamma = ref(2.2)
const softening = ref(20)
const useAsinh = ref(true)
const invert = ref(false)

const zoom = ref(1)
const tx = ref(0)
const ty = ref(0)

const solved = computed(
  () =>
    props.astrometry?.is_plate_solved === true &&
    typeof props.astrometry.ra_deg === 'number' &&
    typeof props.astrometry.dec_deg === 'number' &&
    typeof props.astrometry.pixel_scale_arcsec === 'number',
)

const centerLabel = computed(() => {
  const a = props.astrometry
  if (!a || typeof a.ra_deg !== 'number' || typeof a.dec_deg !== 'number') return null
  return `${formatRa(a.ra_deg)} ${formatDec(a.dec_deg)}`
})

let gl: WebGLRenderingContext | null = null
let program: WebGLProgram | null = null
let texture: WebGLTexture | null = null
let buffer: WebGLBuffer | null = null
let image: HTMLImageElement | null = null
let raf = 0
let resizeObserver: ResizeObserver | null = null

const VERT = `
attribute vec2 a_pos;
uniform vec2 u_scale;
uniform vec2 u_translate;
varying vec2 v_uv;
void main() {
  v_uv = vec2((a_pos.x + 1.0) * 0.5, 1.0 - (a_pos.y + 1.0) * 0.5);
  gl_Position = vec4(a_pos * u_scale + u_translate, 0.0, 1.0);
}
`

const FRAG = `
precision highp float;
varying vec2 v_uv;
uniform sampler2D u_image;
uniform float u_black;
uniform float u_white;
uniform float u_gamma;
uniform float u_soft;
uniform float u_asinh;
uniform float u_invert;

float asinh_(float x) { return log(x + sqrt(x * x + 1.0)); }

void main() {
  if (v_uv.x < 0.0 || v_uv.x > 1.0 || v_uv.y < 0.0 || v_uv.y > 1.0) {
    gl_FragColor = vec4(0.0, 0.0, 0.0, 0.0);
    return;
  }
  vec3 c = texture2D(u_image, v_uv).rgb;
  float span = max(u_white - u_black, 1e-4);
  c = clamp((c - vec3(u_black)) / span, 0.0, 1.0);
  if (u_asinh > 0.5) {
    float k = asinh_(max(u_soft, 1e-3));
    c = vec3(asinh_(c.r * u_soft) / k, asinh_(c.g * u_soft) / k, asinh_(c.b * u_soft) / k);
  }
  c = pow(c, vec3(1.0 / max(u_gamma, 0.05)));
  if (u_invert > 0.5) c = vec3(1.0) - c;
  gl_FragColor = vec4(c, 1.0);
}
`

function compile(context: WebGLRenderingContext, type: number, source: string): WebGLShader | null {
  const shader = context.createShader(type)
  if (!shader) return null
  context.shaderSource(shader, source)
  context.compileShader(shader)
  if (!context.getShaderParameter(shader, context.COMPILE_STATUS)) {
    context.deleteShader(shader)
    return null
  }
  return shader
}

function initGl(): boolean {
  const el = canvas.value
  if (!el) return false
  const context =
    (el.getContext('webgl', { antialias: false, alpha: true }) as WebGLRenderingContext | null) ??
    (el.getContext('experimental-webgl') as WebGLRenderingContext | null)
  if (!context) return false

  const vs = compile(context, context.VERTEX_SHADER, VERT)
  const fs = compile(context, context.FRAGMENT_SHADER, FRAG)
  if (!vs || !fs) return false

  const prog = context.createProgram()
  if (!prog) return false
  context.attachShader(prog, vs)
  context.attachShader(prog, fs)
  context.linkProgram(prog)
  if (!context.getProgramParameter(prog, context.LINK_STATUS)) return false

  const buf = context.createBuffer()
  context.bindBuffer(context.ARRAY_BUFFER, buf)
  context.bufferData(
    context.ARRAY_BUFFER,
    new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
    context.STATIC_DRAW,
  )

  gl = context
  program = prog
  buffer = buf
  return true
}

function uploadTexture(img: HTMLImageElement) {
  if (!gl) return
  texture = gl.createTexture()
  gl.bindTexture(gl.TEXTURE_2D, texture)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img)
}

/** Escala base para que la imagen encaje en el contenedor (zoom = 1). */
function baseScale(): [number, number] {
  const el = canvas.value
  if (!el || !image) return [1, 1]
  const containerAspect = el.width / el.height
  const imageAspect = image.naturalWidth / image.naturalHeight
  if (imageAspect > containerAspect) return [1, containerAspect / imageAspect]
  return [imageAspect / containerAspect, 1]
}

function resize() {
  const el = canvas.value
  const box = container.value
  if (!el || !box) return
  const dpr = Math.min(globalThis.devicePixelRatio || 1, 2)
  const w = Math.max(1, Math.floor(box.clientWidth * dpr))
  const h = Math.max(1, Math.floor(box.clientHeight * dpr))
  if (el.width !== w || el.height !== h) {
    el.width = w
    el.height = h
  }
  const ov = overlay.value
  if (ov && (ov.width !== w || ov.height !== h)) {
    ov.width = w
    ov.height = h
  }
  schedule()
}

function schedule() {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    draw()
    drawGrid()
  })
}

function draw() {
  if (!gl || !program || !texture || !canvas.value) return
  const context = gl
  context.viewport(0, 0, canvas.value.width, canvas.value.height)
  context.clearColor(0, 0, 0, 0)
  context.clear(context.COLOR_BUFFER_BIT)
  context.useProgram(program)

  const loc = context.getAttribLocation(program, 'a_pos')
  context.bindBuffer(context.ARRAY_BUFFER, buffer)
  context.enableVertexAttribArray(loc)
  context.vertexAttribPointer(loc, 2, context.FLOAT, false, 0, 0)

  const [sx, sy] = baseScale()
  context.uniform2f(context.getUniformLocation(program, 'u_scale'), sx * zoom.value, sy * zoom.value)
  context.uniform2f(context.getUniformLocation(program, 'u_translate'), tx.value, ty.value)
  context.uniform1f(context.getUniformLocation(program, 'u_black'), blackPoint.value)
  context.uniform1f(context.getUniformLocation(program, 'u_white'), whitePoint.value)
  context.uniform1f(context.getUniformLocation(program, 'u_gamma'), gamma.value)
  context.uniform1f(context.getUniformLocation(program, 'u_soft'), softening.value)
  context.uniform1f(context.getUniformLocation(program, 'u_asinh'), useAsinh.value ? 1 : 0)
  context.uniform1f(context.getUniformLocation(program, 'u_invert'), invert.value ? 1 : 0)

  context.activeTexture(context.TEXTURE0)
  context.bindTexture(context.TEXTURE_2D, texture)
  context.uniform1i(context.getUniformLocation(program, 'u_image'), 0)
  context.drawArrays(context.TRIANGLES, 0, 6)
}

/* ---------------------------------------------------------------- rejilla */

const DEG = Math.PI / 180

/** Proyección gnomónica (TAN) al plano tangente del centro del campo. */
function project(
  raDeg: number,
  decDeg: number,
  ra0: number,
  dec0: number,
): { xi: number; eta: number } | null {
  const ra = raDeg * DEG
  const dec = decDeg * DEG
  const r0 = ra0 * DEG
  const d0 = dec0 * DEG
  const cosc =
    Math.sin(d0) * Math.sin(dec) + Math.cos(d0) * Math.cos(dec) * Math.cos(ra - r0)
  if (cosc <= 0) return null
  return {
    xi: (Math.cos(dec) * Math.sin(ra - r0)) / cosc / DEG,
    eta:
      ((Math.cos(d0) * Math.sin(dec) - Math.sin(d0) * Math.cos(dec) * Math.cos(ra - r0)) / cosc) /
      DEG,
  }
}

/** Plano tangente (grados) → píxeles de pantalla, con la vista aplicada. */
function toScreen(xi: number, eta: number): [number, number] | null {
  const a = props.astrometry
  const el = canvas.value
  if (!a || !el || !image) return null
  const scaleArcsec = a.pixel_scale_arcsec
  if (!scaleArcsec || scaleArcsec <= 0) return null

  const theta = (a.orientation_deg ?? 0) * DEG
  const parity = a.parity === -1 ? -1 : 1
  const cos = Math.cos(theta)
  const sin = Math.sin(theta)
  // grados → píxeles de la imagen original
  const px = ((xi * cos + eta * sin) * 3600) / scaleArcsec
  const py = ((-xi * sin + eta * cos) * 3600) / scaleArcsec

  // píxeles → coordenadas de imagen normalizadas [-1, 1]
  const nx = (parity * px * 2) / image.naturalWidth
  const ny = (py * 2) / image.naturalHeight

  const [sx, sy] = baseScale()
  const clipX = nx * sx * zoom.value + tx.value
  const clipY = ny * sy * zoom.value + ty.value
  return [((clipX + 1) / 2) * el.width, ((1 - clipY) / 2) * el.height]
}

function niceStep(spanDeg: number): number {
  const candidates = [30, 15, 10, 5, 2, 1, 0.5, 0.25, 1 / 6, 1 / 12, 1 / 30, 1 / 60]
  for (const c of candidates) if (spanDeg / c >= 3) return c
  return 1 / 120
}

function drawGrid() {
  const ov = overlay.value
  if (!ov) return
  const ctx = ov.getContext('2d')
  if (!ctx) return
  ctx.clearRect(0, 0, ov.width, ov.height)
  const a = props.astrometry
  if (!showGrid.value || !solved.value || !a) return
  const ra0 = a.ra_deg
  const dec0 = a.dec_deg
  if (typeof ra0 !== 'number' || typeof dec0 !== 'number') return

  const radius = Math.max(a.field_radius_deg ?? 1, 0.05) * 1.6
  const decStep = niceStep(radius * 2)
  const raStep = decStep / Math.max(Math.cos(dec0 * DEG), 0.1)

  ctx.lineWidth = Math.max(1, ov.width / 1400)
  ctx.strokeStyle = 'rgba(125, 211, 252, 0.55)'
  ctx.fillStyle = 'rgba(186, 230, 253, 0.9)'
  ctx.font = `${Math.max(11, ov.width / 90)}px ui-monospace, monospace`

  const decMin = Math.max(-89.9, dec0 - radius)
  const decMax = Math.min(89.9, dec0 + radius)

  // Paralelos (Dec constante)
  for (let d = Math.ceil(decMin / decStep) * decStep; d <= decMax; d += decStep) {
    ctx.beginPath()
    let started = false
    for (let i = 0; i <= 64; i++) {
      const ra = ra0 - radius / Math.max(Math.cos(d * DEG), 0.1) + (i / 64) * (2 * radius) / Math.max(Math.cos(d * DEG), 0.1)
      const p = project(ra, d, ra0, dec0)
      if (!p) {
        started = false
        continue
      }
      const s = toScreen(p.xi, p.eta)
      if (!s) continue
      if (!started) {
        ctx.moveTo(s[0], s[1])
        started = true
      } else {
        ctx.lineTo(s[0], s[1])
      }
    }
    ctx.stroke()
    const label = project(ra0, d, ra0, dec0)
    const ls = label ? toScreen(label.xi, label.eta) : null
    if (ls && ls[0] > 0 && ls[1] > 0 && ls[0] < ov.width && ls[1] < ov.height) {
      ctx.fillText(formatDec(d, 0), 8, ls[1] - 4)
    }
  }

  // Meridianos (AR constante)
  for (
    let r = Math.ceil((ra0 - radius / Math.max(Math.cos(dec0 * DEG), 0.1)) / raStep) * raStep;
    r <= ra0 + radius / Math.max(Math.cos(dec0 * DEG), 0.1);
    r += raStep
  ) {
    ctx.beginPath()
    let started = false
    for (let i = 0; i <= 64; i++) {
      const d = decMin + (i / 64) * (decMax - decMin)
      const p = project(r, d, ra0, dec0)
      if (!p) {
        started = false
        continue
      }
      const s = toScreen(p.xi, p.eta)
      if (!s) continue
      if (!started) {
        ctx.moveTo(s[0], s[1])
        started = true
      } else {
        ctx.lineTo(s[0], s[1])
      }
    }
    ctx.stroke()
    const label = project(r, dec0, ra0, dec0)
    const ls = label ? toScreen(label.xi, label.eta) : null
    if (ls && ls[0] > 0 && ls[1] > 0 && ls[0] < ov.width && ls[1] < ov.height) {
      ctx.fillText(formatRa(r, 0), ls[0] + 4, ov.height - 8)
    }
  }
}

/* ------------------------------------------------------------ interacción */

function zoomAt(factor: number, clipX = 0, clipY = 0) {
  const [sx, sy] = baseScale()
  const before = zoom.value
  const next = Math.min(24, Math.max(0.2, before * factor))
  const px = (clipX - tx.value) / (sx * before)
  const py = (clipY - ty.value) / (sy * before)
  zoom.value = next
  tx.value = clipX - px * sx * next
  ty.value = clipY - py * sy * next
  schedule()
}

function clipFromEvent(clientX: number, clientY: number): [number, number] {
  const el = canvas.value
  if (!el) return [0, 0]
  const rect = el.getBoundingClientRect()
  return [((clientX - rect.left) / rect.width) * 2 - 1, 1 - ((clientY - rect.top) / rect.height) * 2]
}

function onWheel(event: WheelEvent) {
  event.preventDefault()
  const [cx, cy] = clipFromEvent(event.clientX, event.clientY)
  zoomAt(event.deltaY < 0 ? 1.15 : 1 / 1.15, cx, cy)
}

let dragging = false
let lastX = 0
let lastY = 0
let pinchDistance = 0

function onPointerDown(event: PointerEvent) {
  dragging = true
  lastX = event.clientX
  lastY = event.clientY
  ;(event.target as HTMLElement).setPointerCapture?.(event.pointerId)
}

function onPointerMove(event: PointerEvent) {
  if (!dragging || !canvas.value) return
  const rect = canvas.value.getBoundingClientRect()
  tx.value += ((event.clientX - lastX) / rect.width) * 2
  ty.value -= ((event.clientY - lastY) / rect.height) * 2
  lastX = event.clientX
  lastY = event.clientY
  schedule()
}

function onPointerUp() {
  dragging = false
}

function onTouchMove(event: TouchEvent) {
  if (event.touches.length !== 2) return
  event.preventDefault()
  const [a, b] = [event.touches[0], event.touches[1]]
  if (!a || !b) return
  const distance = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)
  if (pinchDistance > 0) {
    const [cx, cy] = clipFromEvent((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2)
    zoomAt(distance / pinchDistance, cx, cy)
  }
  pinchDistance = distance
}

function onTouchEnd() {
  pinchDistance = 0
}

function onKeydown(event: KeyboardEvent) {
  const step = event.shiftKey ? 0.25 : 0.08
  switch (event.key) {
    case 'ArrowLeft':
      tx.value += step
      break
    case 'ArrowRight':
      tx.value -= step
      break
    case 'ArrowUp':
      ty.value -= step
      break
    case 'ArrowDown':
      ty.value += step
      break
    case '+':
    case '=':
      zoomAt(1.2)
      return
    case '-':
    case '_':
      zoomAt(1 / 1.2)
      return
    case '0':
      resetView()
      return
    case 'g':
    case 'G':
      if (solved.value) showGrid.value = !showGrid.value
      return
    default:
      return
  }
  event.preventDefault()
  schedule()
}

function resetView() {
  zoom.value = 1
  tx.value = 0
  ty.value = 0
  schedule()
}

function resetStretch() {
  blackPoint.value = 0
  whitePoint.value = 1
  gamma.value = 2.2
  softening.value = 20
  useAsinh.value = true
  invert.value = false
  schedule()
}

/* ------------------------------------------------------------ ciclo vital */

function load() {
  loading.value = true
  loadError.value = false
  const img = new Image()
  img.crossOrigin = 'anonymous'
  img.decoding = 'async'
  img.onload = () => {
    image = img
    loading.value = false
    if (gl) {
      uploadTexture(img)
      resize()
    }
  }
  img.onerror = () => {
    loading.value = false
    loadError.value = true
  }
  img.src = props.src
}

onMounted(() => {
  webglAvailable.value = initGl()
  if (!webglAvailable.value) {
    loading.value = false
    return
  }
  load()
  resize()
  if (typeof ResizeObserver !== 'undefined' && container.value) {
    resizeObserver = new ResizeObserver(() => resize())
    resizeObserver.observe(container.value)
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  if (raf) cancelAnimationFrame(raf)
  if (gl) {
    if (texture) gl.deleteTexture(texture)
    if (buffer) gl.deleteBuffer(buffer)
    if (program) gl.deleteProgram(program)
  }
})

watch(
  () => props.src,
  () => {
    if (webglAvailable.value) load()
  },
)
watch([blackPoint, whitePoint, gamma, softening, useAsinh, invert, showGrid], schedule)
</script>

<template>
  <figure class="surface overflow-hidden">
    <div
      v-if="!webglAvailable"
      class="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm"
      role="status"
    >
      {{ t('viewer.webglUnavailable') }}
    </div>

    <div v-if="!webglAvailable" :class="heightClass" class="flex items-center justify-center">
      <img
        :src="fallbackSrc ?? src"
        :alt="title"
        class="max-h-full max-w-full object-contain"
        loading="lazy"
      />
    </div>

    <template v-else>
      <div ref="container" :class="heightClass" class="relative bg-black">
        <canvas
          ref="canvas"
          class="h-full w-full touch-none"
          tabindex="0"
          role="img"
          :aria-label="t('viewer.canvasLabel', { title: title || t('photo.untitled') })"
          @wheel="onWheel"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @touchmove="onTouchMove"
          @touchend="onTouchEnd"
          @keydown="onKeydown"
        />
        <canvas ref="overlay" class="pointer-events-none absolute inset-0 h-full w-full" />

        <p
          v-if="loading"
          class="absolute inset-0 flex items-center justify-center text-sm text-night-200"
        >
          {{ t('viewer.loading') }}
        </p>
        <p
          v-else-if="loadError"
          class="absolute inset-0 flex items-center justify-center text-sm text-rose-300"
          role="alert"
        >
          {{ t('viewer.loadError') }}
        </p>
      </div>

      <div class="grid gap-4 border-t border-night-800 p-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="field-label" for="viewer-black">
            {{ t('viewer.blackPoint') }} <span class="muted">{{ blackPoint.toFixed(3) }}</span>
          </label>
          <input
            id="viewer-black"
            v-model.number="blackPoint"
            type="range"
            min="0"
            max="0.99"
            step="0.001"
            class="w-full"
          />
        </div>
        <div>
          <label class="field-label" for="viewer-white">
            {{ t('viewer.whitePoint') }} <span class="muted">{{ whitePoint.toFixed(3) }}</span>
          </label>
          <input
            id="viewer-white"
            v-model.number="whitePoint"
            type="range"
            min="0.01"
            max="1"
            step="0.001"
            class="w-full"
          />
        </div>
        <div>
          <label class="field-label" for="viewer-gamma">
            {{ t('viewer.gamma') }} <span class="muted">{{ gamma.toFixed(2) }}</span>
          </label>
          <input
            id="viewer-gamma"
            v-model.number="gamma"
            type="range"
            min="0.2"
            max="4"
            step="0.05"
            class="w-full"
          />
        </div>
        <div>
          <label class="field-label" for="viewer-soft">
            {{ t('viewer.stretch') }} <span class="muted">{{ softening.toFixed(0) }}</span>
          </label>
          <input
            id="viewer-soft"
            v-model.number="softening"
            type="range"
            min="1"
            max="500"
            step="1"
            class="w-full"
            :disabled="!useAsinh"
          />
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2 border-t border-night-800 p-4 text-sm">
        <label class="chip cursor-pointer">
          <input v-model="useAsinh" type="checkbox" />
          asinh
        </label>
        <label class="chip cursor-pointer">
          <input v-model="invert" type="checkbox" />
          {{ t('viewer.invert') }}
        </label>
        <button type="button" class="btn-secondary py-1" @click="zoomAt(1.25)">
          {{ t('viewer.zoomIn') }}
        </button>
        <button type="button" class="btn-secondary py-1" @click="zoomAt(1 / 1.25)">
          {{ t('viewer.zoomOut') }}
        </button>
        <button type="button" class="btn-secondary py-1" @click="resetView">
          {{ t('viewer.fit') }}
        </button>
        <button type="button" class="btn-secondary py-1" @click="resetStretch">
          {{ t('viewer.reset') }}
        </button>
        <button
          type="button"
          class="btn-secondary py-1"
          :disabled="!solved"
          :aria-pressed="showGrid"
          @click="showGrid = !showGrid"
        >
          {{ t('viewer.grid') }}
        </button>
        <span v-if="!solved" class="muted text-xs">{{ t('viewer.gridUnavailable') }}</span>
        <span v-else-if="centerLabel" class="muted ml-auto font-mono text-xs">
          {{ centerLabel }}
        </span>
      </div>

      <figcaption class="muted border-t border-night-800 px-4 py-2 text-xs">
        {{ t('viewer.keyboardHelp') }}
      </figcaption>
    </template>
  </figure>
</template>
