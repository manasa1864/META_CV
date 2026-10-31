import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
} from 'react'
import {
  getExamples,
  getReplay,
  getStatus,
  sendFeedback,
  streamChat,
  StreamDropped,
  type ChatEvent,
  type Example,
  type Final,
  type Mode,
  type PathItem,
  type Status,
  type Step,
} from './api'
import './App.css'

const MetricsView = lazy(() => import('./Metrics'))
const ResultsView = lazy(() => import('./Results'))

type Phase = 'thinking' | 'answering' | 'done' | 'error'
type BloopMood = 'greeting' | 'thinking' | 'supported' | 'uncertain'
type IconName =
  | 'send'
  | 'settings'
  | 'chevron'
  | 'check'
  | 'link'
  | 'close'
  | 'scale'
  | 'bulb'
  | 'search'
  | 'replay'

type Assistant = {
  id: number
  role: 'assistant'
  steps: Step[]
  phase: Phase
  answer: string
  final?: Final
  error?: string
  showThinking: boolean
  feedback?: boolean
  recorded?: string
  retry?: string
}
type Message = { id: number; role: 'user'; text: string } | Assistant
type View = 'chat' | 'metrics' | 'results'

const INTRO_KEY = 'prism-intro-autoplay'

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* private mode: the choice lasts for this visit only */
  }
}
const WAVE_SRC = '/prism-wave.png'

const STAGES: Record<string, string> = {
  cache: 'Verified-path cache',
  router: 'Hallucination risk & routing',
  states: 'Reused reasoning',
  consistency: 'Conversation consistency',
  graph: 'Knowledge graph',
  factual: 'Factual verification',
  reasoning: 'Reasoning checks',
  compose: 'Composing',
  compression: 'Reusing verified work',
  planner: 'Facts first, then the calculation',
  guard: 'Faithfulness check',
  audit: 'Graph health',
  health: 'Verified-only mode',
  budget: 'Budget',
  ablation: 'Ablation',
}

const STEP_GAP_MS = 220 // reveal new thinking steps one by one
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const newConversationId = () => crypto.randomUUID()

/* ---------- Intro wave: the glass wave photo flows like liquid, then swirls and pinches into a
   glass marble that lands exactly where Bloop's helmet appears. One WebGL shader plus a 2D sparkle burst. */

const FLOW_END = 3000
const PINCH_DUR = 2500
const SETTLE = 700
const SPARKLE_AT = 5350
const END = 6800

/** Width of Bloop in the intro. The helmet radius is 62/200 of it (see Bloop's SVG). */
function introBloopWidth() {
  return Math.min(240, window.innerWidth * 0.55)
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
const easeInOut = (t: number) => t * t * t * (t * (t * 6 - 15) + 10)
const easeOut = (t: number) => 1 - (1 - t) ** 4

const VERT = `attribute vec2 aPos; void main() { gl_Position = vec4(aPos, 0.0, 1.0); }`

const FRAG = `
precision highp float;
uniform sampler2D uImg;
uniform vec2 uRes;
uniform vec2 uImgSize;
uniform vec2 uCenter;
uniform float uR;
uniform float uK;
uniform float uTime;

vec2 coverUv(vec2 q) {
  float s = max(uRes.x / uImgSize.x, uRes.y / uImgSize.y);
  vec2 size = uImgSize * s;
  return (q - (uRes - size) * 0.5) / size;
}

void main() {
  vec2 p = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y);
  vec2 d = p - uCenter;
  float r = length(d);
  float a = atan(d.y, d.x);
  // Everything beyond lim has been pulled into the marble; lim shrinks from infinity to uR.
  float lim = uR / max(uK, 1e-4);
  float x = r / lim;
  if (x >= 1.0) { gl_FragColor = vec4(0.0); return; }
  float rho = r / (1.0 - x * 0.985 * smoothstep(0.0, 1.0, uK * 1.4));
  float swirl = uK * uK * 3.0 * (1.0 - x) + uK * 0.5;
  float sa = a - swirl;
  vec2 uv = coverUv(uCenter + vec2(cos(sa), sin(sa)) * rho);
  // A travelling wave: broad crests roll left to right through the photo, with a slower swell underneath.
  uv = (uv - 0.5) * 0.84 + 0.5;
  float flow = 1.0 - uK * 0.7;
  float ph = uv.x * 6.2831 * 1.0 - uTime * 1.3;
  float band = 0.3 + 0.7 * smoothstep(0.0, 0.5, uv.y);
  float crest = sin(ph) + 0.45 * sin(ph * 2.0 + 1.1 - uTime * 0.5);
  uv.y += flow * band * (0.055 * crest + 0.02 * sin(uv.x * 6.2831 * 0.5 - uTime * 0.6));
  uv.x += flow * band * 0.03 * cos(ph + uv.y * 3.0);
  uv = clamp(uv, 0.0, 1.0);
  vec3 col = texture2D(uImg, uv).rgb;
  // Light slides along each crest so the motion reads as a swell, not a shifting picture.
  col *= 1.0 + flow * 0.09 * (cos(ph) + 0.4 * cos(ph * 2.0 + 1.1 - uTime * 0.5));
  // Glass rim on the marble edge, with an iridescent tint that turns as it spins.
  float edge = 1.0 - smoothstep(0.96, 1.0, x);
  float rim = smoothstep(0.6, 0.97, x) * uK;
  vec3 irid = 0.62 + 0.38 * cos(6.2831 * (a / 6.2831 + vec3(0.0, 0.33, 0.67)) + uTime * 2.0);
  col = mix(col, irid, rim * 0.55);
  col += vec3(1.0) * smoothstep(0.86, 0.97, x) * uK * 0.5;
  // Soft highlight up and to the left, like the helmet's shine.
  vec2 hl = (d / max(lim, 1.0)) - vec2(-0.38, -0.42);
  col += vec3(1.0) * smoothstep(0.32, 0.0, length(hl * vec2(1.0, 1.6))) * uK * 0.55;
  gl_FragColor = vec4(col * edge, edge);
}`

function compile(gl: WebGLRenderingContext, type: number, src: string) {
  const s = gl.createShader(type)!
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader')
  return s
}

type Spark = { a: number; v: number; r: number; hue: string }

/** Starts the intro animation. Adds `gl` to the intro root once the photo is on the GPU. */
function runIntroWave(canvas: HTMLCanvasElement, sparkCanvas: HTMLCanvasElement, src: string) {
  const gl = (canvas.getContext('webgl2', { premultipliedAlpha: true }) ||
    canvas.getContext('webgl', { premultipliedAlpha: true })) as WebGLRenderingContext | null
  const sctx = sparkCanvas.getContext('2d')
  if (!gl || !sctx) return () => {}

  let program: WebGLProgram
  try {
    program = gl.createProgram()!
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT))
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG))
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return () => {}
  } catch {
    return () => {}
  }
  gl.useProgram(program)
  const buf = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW)
  const aPos = gl.getAttribLocation(program, 'aPos')
  gl.enableVertexAttribArray(aPos)
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0)
  const u = (name: string) => gl.getUniformLocation(program, name)
  const uRes = u('uRes'),
    uImgSize = u('uImgSize'),
    uCenter = u('uCenter'),
    uR = u('uR'),
    uK = u('uK'),
    uTime = u('uTime')

  const sparks: Spark[] = Array.from({ length: 18 }, (_, i) => ({
    a: (i / 18) * Math.PI * 2 + Math.random() * 0.3,
    v: 0.08 + Math.random() * 0.12,
    r: 2 + Math.random() * 3,
    hue: ['#ffffff', '#9ff3ff', '#ffc4e6', '#fff1a8'][i % 4],
  }))

  let raf = 0,
    ready = false,
    dpr = 1,
    W = 0,
    H = 0
  let start = 0
  const img = new Image()
  img.onload = () => {
    const tex = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, tex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    // Mipmaps keep the squeezed marble from shimmering; WebGL1 can't mip a non-power-of-two image.
    if (typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext) {
      gl.generateMipmap(gl.TEXTURE_2D)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    }
    gl.uniform2f(uImgSize, img.naturalWidth, img.naturalHeight)
    ready = true
    canvas.parentElement?.classList.add('gl')
  }
  img.src = src

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2)
    W = canvas.clientWidth
    H = canvas.clientHeight
    canvas.width = sparkCanvas.width = Math.round(W * dpr)
    canvas.height = sparkCanvas.height = Math.round(H * dpr)
  }

  function frame() {
    const now = performance.now()
    if (!start) start = now
    const t = now - start
    if (canvas.clientWidth !== W || canvas.clientHeight !== H) resize()
    const cx = W / 2,
      cy = H * 0.46,
      R = introBloopWidth() * 0.31

    if (ready) {
      const k = easeInOut(clamp01((t - FLOW_END) / PINCH_DUR))
      const settle = easeOut(clamp01((t - FLOW_END - PINCH_DUR) / SETTLE))
      const wobble = 1 + 0.035 * k * (1 - settle) * Math.sin(t * 0.008)
      gl!.viewport(0, 0, canvas.width, canvas.height)
      gl!.uniform2f(uRes, canvas.width, canvas.height)
      gl!.uniform2f(uCenter, cx * dpr, cy * dpr)
      gl!.uniform1f(uR, R * dpr * wobble)
      gl!.uniform1f(uK, k)
      gl!.uniform1f(uTime, t / 1000)
      gl!.drawArrays(gl!.TRIANGLE_STRIP, 0, 4)
    }

    // A little burst of sparkle as the marble closes.
    sctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
    sctx!.clearRect(0, 0, W, H)
    const age = t - SPARKLE_AT
    if (age > 0 && age < 1100) {
      const life = age / 1100
      for (const s of sparks) {
        const dist = R * (1.05 + easeOut(life) * s.v * 9)
        const x = cx + Math.cos(s.a) * dist,
          y = cy + Math.sin(s.a) * dist
        const r = s.r * (1 - life) * 2.2
        sctx!.globalAlpha = 1 - life
        sctx!.fillStyle = s.hue
        sctx!.beginPath()
        sctx!.moveTo(x, y - r)
        sctx!.quadraticCurveTo(x, y, x + r, y)
        sctx!.quadraticCurveTo(x, y, x, y + r)
        sctx!.quadraticCurveTo(x, y, x - r, y)
        sctx!.quadraticCurveTo(x, y, x, y - r)
        sctx!.fill()
      }
      sctx!.globalAlpha = 1
    }

    if (t < END) raf = requestAnimationFrame(frame)
  }

  resize()
  raf = requestAnimationFrame(frame)
  return () => cancelAnimationFrame(raf)
}

/* ---------- Visual pieces ---------- */

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    send: (
      <>
        <path d="M12 19V5" />
        <path d="m5 12 7-7 7 7" />
      </>
    ),
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
      </>
    ),
    chevron: <path d="m8 10 4 4 4-4" />,
    check: <path d="m5 12 4 4L19 6" />,
    link: (
      <>
        <path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1" />
        <path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1" />
      </>
    ),
    close: (
      <>
        <path d="m6 6 12 12" />
        <path d="m18 6-12 12" />
      </>
    ),
    scale: (
      <>
        <path d="M12 4v16" />
        <path d="M7 20h10" />
        <path d="M5 8h14" />
        <path d="m5 8-3 6a3 3 0 0 0 6 0L5 8Z" />
        <path d="m19 8-3 6a3 3 0 0 0 6 0l-3-6Z" />
      </>
    ),
    bulb: (
      <>
        <path d="M9 18h6" />
        <path d="M10 21h4" />
        <path d="M12 3a6 6 0 0 0-3.6 10.8c.7.5 1.1 1.3 1.1 2.2h5c0-.9.4-1.7 1.1-2.2A6 6 0 0 0 12 3Z" />
      </>
    ),
    search: (
      <>
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </>
    ),
    replay: (
      <>
        <path d="M3 12a9 9 0 1 0 3-6.7" />
        <path d="M3 4v5h5" />
      </>
    ),
  }
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  )
}

function Button({
  children,
  className = '',
  label,
  onClick,
  type = 'button',
  style,
  disabled,
}: {
  children: ReactNode
  className?: string
  label?: string
  onClick?: () => void
  type?: 'button' | 'submit'
  style?: CSSProperties
  disabled?: boolean
}) {
  return (
    <button type={type} aria-label={label} className={className} onClick={onClick} style={style} disabled={disabled}>
      {children}
    </button>
  )
}

const GLOW = '#fff3c9'

function BloopEyes({ mood }: { mood: BloopMood }) {
  if (mood === 'supported') {
    return (
      <g className="b-eyes" stroke={GLOW} strokeWidth="5.5" strokeLinecap="round" fill="none">
        <path d="M-29 3Q-19-11-9 3" />
        <path d="M9 3Q19-11 29 3" />
      </g>
    )
  }
  const look = mood === 'thinking' ? 'translate(4 -4)' : undefined
  const right = mood === 'uncertain' ? 9 : 13
  return (
    <g className="b-eyes" transform={look}>
      <g className="b-blink">
        <rect x="-29" y="-13" width="20" height="26" rx="10" fill={GLOW} />
        <rect x="9" y={mood === 'uncertain' ? -9 : -13} width="20" height={right * 2} rx="10" fill={GLOW} />
      </g>
    </g>
  )
}

function BloopMouth({ mood }: { mood: BloopMood }) {
  const props = { stroke: GLOW, strokeWidth: 3.2, strokeLinecap: 'round' as const, fill: 'none' }
  if (mood === 'thinking') return <circle cx="3" cy="24" r="3" {...props} />
  if (mood === 'uncertain') return <path d="M-9 24q4.5-4 9 0t9 0" {...props} />
  return <path d={mood === 'supported' ? 'M-10 20Q0 31 10 20' : 'M-8 21Q0 28 8 21'} {...props} />
}

/** Soft pastel helmet robot, drawn in a 200×240 box. Helmet centre sits at (100, 90) with radius 62. */
function Bloop({ mood = 'greeting', size = 170, className = '' }: { mood?: BloopMood; size?: number; className?: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const id = (name: string) => `${name}-${uid}`
  const url = (name: string) => `url(#${id(name)})`
  const armL = mood === 'thinking' ? 'M72 168L62 146' : 'M72 168L60 192'
  const armR = mood === 'greeting' ? 'M128 168L156 146' : 'M128 168L140 192'
  return (
    <svg
      className={`bloop mood-${mood} ${className}`}
      width={size}
      height={size * 1.2}
      viewBox="0 0 200 240"
      role="img"
      aria-label={`Bloop is ${mood}`}
    >
      <defs>
        <radialGradient id={id('shell')} cx="34%" cy="26%" r="85%">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset=".45" stopColor="#f1f0ff" />
          <stop offset=".8" stopColor="#d6dcfb" />
          <stop offset="1" stopColor="#b9cbf7" />
        </radialGradient>
        <linearGradient id={id('visor')} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#c7b9f4" />
          <stop offset=".55" stopColor="#d9bff0" />
          <stop offset="1" stopColor="#f3c2dc" />
        </linearGradient>
        <linearGradient id={id('limb')} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#e4e6ff" />
          <stop offset="1" stopColor="#b9a9ee" />
        </linearGradient>
        <linearGradient id={id('pink')} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#f4b8dc" />
          <stop offset="1" stopColor="#c9a2ec" />
        </linearGradient>
        <linearGradient id={id('torso')} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#f6f4ff" />
          <stop offset=".55" stopColor="#d8dcfb" />
          <stop offset="1" stopColor="#bcc4f6" />
        </linearGradient>
        <filter id={id('glow')} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="2.6" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <ellipse className="b-part b-shadow" cx="100" cy="232" rx="50" ry="5.5" fill="#6a5bb8" opacity=".14" />

      <g className="b-part b-legs">
        <ellipse cx="78" cy="212" rx="21" ry="15" fill={url('pink')} />
        <ellipse cx="122" cy="212" rx="21" ry="15" fill={url('pink')} />
        <ellipse cx="72" cy="206" rx="8" ry="4" fill="#fff" opacity=".55" />
        <ellipse cx="116" cy="206" rx="8" ry="4" fill="#fff" opacity=".55" />
      </g>

      <g className="b-part b-arm-l">
        <g className={mood === 'thinking' ? 'b-chin' : undefined}>
          <path d={armL} stroke={url('limb')} strokeWidth="16" strokeLinecap="round" />
          <path d={armL} stroke="#fff" strokeOpacity=".35" strokeWidth="5" strokeLinecap="round" transform="translate(-2 -2)" />
        </g>
      </g>

      <g className="b-part b-body">
        <path d="M74 150h52q12 0 14 14l3 28q2 20-18 20H75q-20 0-18-20l3-28q2-14 14-14Z" fill={url('torso')} />
        <path d="M66 164q-4 16-2 32" stroke="#fff" strokeWidth="4" strokeLinecap="round" opacity=".6" fill="none" />
        <path
          d="M84 176l8 7 8-3 8 3 8-7"
          stroke="#b9a9ee"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
          opacity=".8"
        />
        <rect className="b-core" x="90" y="190" width="20" height="5" rx="2.5" fill="#8fe8f7" filter={url('glow')} />
        <rect x="80" y="144" width="40" height="12" rx="6" fill="#c8b6f0" />
      </g>

      <g className="b-part b-arm-r">
        <g className={mood === 'greeting' ? 'b-wave' : undefined}>
          <path d={armR} stroke={url('limb')} strokeWidth="16" strokeLinecap="round" />
          <path d={armR} stroke="#fff" strokeOpacity=".35" strokeWidth="5" strokeLinecap="round" transform="translate(-2 -2)" />
        </g>
      </g>

      <g className="b-part b-ears">
        {[36, 164].map((cx) => (
          <g key={cx}>
            <rect x={cx - 15} y="68" width="30" height="46" rx="15" fill={url('pink')} />
            <circle cx={cx} cy="91" r="9.5" fill="#c9b6f2" />
            <circle cx={cx} cy="91" r="7" fill="none" stroke="#8fe8f7" strokeWidth="2.4" filter={url('glow')} />
          </g>
        ))}
      </g>

      <g className="b-part b-antenna">
        {[
          { x: 70, tx: 64, bx: 60 },
          { x: 130, tx: 136, bx: 140 },
        ].map((a) => (
          <g key={a.x}>
            <path d={`M${a.x} 38Q${a.tx} 28 ${a.bx} 20`} stroke="#c6bdee" strokeWidth="4" strokeLinecap="round" fill="none" />
            <circle className="b-bulb" cx={a.bx} cy="17" r="9" fill={url('pink')} />
            <circle cx={a.bx - 3} cy="14" r="3" fill="#fff" opacity=".7" />
          </g>
        ))}
      </g>

      <g className="b-part b-helmet-base">
        <circle cx="100" cy="90" r="62" fill={url('shell')} />
      </g>

      <g className="b-part b-visor">
        <rect x="52" y="62" width="96" height="60" rx="30" fill={url('visor')} stroke="#fff" strokeWidth="3" />
        <path d="M66 73q34-12 68 0" stroke="#fff" strokeOpacity=".4" strokeWidth="5" strokeLinecap="round" fill="none" />
      </g>

      <g className="b-part b-face">
        <ellipse cx="68" cy="108" rx="8" ry="4.5" fill="#ff9ccb" opacity=".45" />
        <ellipse cx="132" cy="108" rx="8" ry="4.5" fill="#ff9ccb" opacity=".45" />
        <g transform="translate(100 90)" filter={url('glow')}>
          <BloopEyes mood={mood} />
          <BloopMouth mood={mood} />
        </g>
      </g>

      <g className="b-part b-helmet">
        <circle cx="100" cy="90" r="61" fill="none" stroke="#fff" strokeOpacity=".8" strokeWidth="2" />
        <path d="M56 62A52 52 0 0 1 90 38" stroke="#fff" strokeWidth="7" strokeLinecap="round" opacity=".85" fill="none" />
        <circle cx="52" cy="78" r="3.4" fill="#fff" opacity=".85" />
      </g>

      {mood === 'supported' && (
        <path className="b-spark" d="M176 34q2 10 12 12-10 2-12 12-2-10-12-12 10-2 12-12Z" fill="#f7c6e2" />
      )}
      {mood === 'uncertain' && (
        <text className="b-question" x="168" y="46" fill="#9a88e6" fontSize="30" fontWeight="700">
          ?
        </text>
      )}
    </svg>
  )
}

/** The Prism mark: three glass petals turning around a centre. */
function PrismMark({ className = 'bloop-mark' }: { className?: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const petal = 'M-9 7C-13-5-3-17 10-12 3-9-1-3-2 7-3 11-8 11-9 7Z'
  return (
    <svg className={className} viewBox="-20 -20 40 40" aria-hidden="true">
      <defs>
        <linearGradient id={`pm-a-${uid}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#8fb4f5" />
          <stop offset=".55" stopColor="#c3b8f4" />
          <stop offset="1" stopColor="#f3bde0" />
        </linearGradient>
        <linearGradient id={`pm-b-${uid}`} x1="1" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#dff0ff" />
          <stop offset="1" stopColor="#a9c3f7" />
        </linearGradient>
      </defs>
      {[0, 120, 240].map((deg) => (
        <g key={deg} transform={`rotate(${deg})`}>
          <path d={petal} fill={`url(#pm-a-${uid})`} />
          <path d={petal} fill={`url(#pm-b-${uid})`} opacity=".45" transform="scale(.7) translate(-1 -1)" />
          <path d="M-6 3C-8-3-3-10 4-11" stroke="#fff" strokeWidth="1.3" strokeLinecap="round" fill="none" opacity=".85" />
        </g>
      ))}
    </svg>
  )
}

function Intro({ onSkip }: { onSkip: () => void }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const sparks = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const id = window.setTimeout(onSkip, 8600)
    return () => window.clearTimeout(id)
  }, [onSkip])
  useEffect(
    () => (canvas.current && sparks.current ? runIntroWave(canvas.current, sparks.current, WAVE_SRC) : undefined),
    [],
  )
  return (
    <div className="intro" role="dialog" aria-label="Prism introduction" style={{ '--bw': `${introBloopWidth()}px` } as CSSProperties}>
      <div className="intro-brand">
        <PrismMark />
        <b>Prism</b>
      </div>
      <Button className="skip" onClick={onSkip}>
        Skip intro
      </Button>
      <img className="wave-photo" src={WAVE_SRC} alt="" />
      <canvas ref={canvas} className="wave-canvas" />
      <canvas ref={sparks} className="wave-canvas" />
      <div className="intro-bloop">
        <Bloop mood="greeting" />
      </div>
      <div className="intro-copy">
        <span>Hey</span>
        <small>I’m Bloop. Let’s figure things out together.</small>
      </div>
    </div>
  )
}

/* ---------- Chat ---------- */

const EXAMPLE_STYLE: Record<string, { icon: IconName; tone: string }> = {
  'false-premise': { icon: 'search', tone: 'coral' },
  'multi-fact': { icon: 'check', tone: 'mint' },
  comparison: { icon: 'scale', tone: 'sky' },
  mixed: { icon: 'bulb', tone: 'sun' },
  reuse: { icon: 'link', tone: 'lav' },
  'follow-up': { icon: 'replay', tone: 'lav' },
}

/** Used only if /api/examples cannot be reached. */
const FALLBACK_EXAMPLES: Example[] = [
  { id: 'false-premise', label: 'False premise', tier: 'factual', replay: false,
    turns: ['Why did Albert Einstein win the Nobel Prize in 1905?'] },
  { id: 'multi-fact', label: 'Several facts at once', tier: 'factual', replay: false,
    turns: ['Who wrote Pride and Prejudice, when was it published, and where was the author born?'] },
  { id: 'mixed', label: 'Facts, then a calculation', tier: 'reasoning', replay: false,
    turns: ['How old was Harper Lee when To Kill a Mockingbird was published?'] },
  { id: 'follow-up', label: 'Follow-up question', tier: 'consistency', replay: false,
    turns: ['Who wrote 1984?', 'How many years after his birth was it published?'] },
]

/** Live, replay or unavailable: decides what an example chip does. */
function liveModel(status: Status | null) {
  return !status || (status.llm.configured && !status.llm.breaker_open)
}

function Banner({ status }: { status: Status | null }) {
  if (!status) return null
  const notes: [string, string][] = []
  if (status.scripted) notes.push(['sun', 'Scripted demo (no model): answers come from a fixed script, not a live model.'])
  else if (!liveModel(status))
    notes.push(['coral', 'The live model is unavailable right now. Examples play recorded runs (Replay mode).'])
  if (status.degraded) notes.push(['sun', 'Running on a local snapshot of the knowledge graph (read-only).'])
  if (status.health.verified_only)
    notes.push(['sun', `Verified-only mode: only graph-backed facts are used (${status.health.reason}).`])
  if (!notes.length) return null
  return (
    <div className="banners" role="status">
      {notes.map(([tone, text]) => (
        <p key={text} className={`banner tone-${tone}`}>
          {text}
        </p>
      ))}
    </div>
  )
}

function ChatComposer({
  onSubmit,
  compact = false,
  disabled = false,
  evidence,
  setEvidence,
}: {
  onSubmit: (value: string) => void
  compact?: boolean
  disabled?: boolean
  evidence: boolean
  setEvidence: (value: boolean) => void
}) {
  const [value, setValue] = useState('')
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!value.trim() || disabled) return
    onSubmit(value.trim())
    setValue('')
  }
  return (
    <form className={`composer ${compact ? 'composer-compact' : ''}`} onSubmit={submit}>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={compact ? 'Ask a follow-up…' : 'What are you curious about?'}
        aria-label="Ask Bloop anything"
        autoFocus
      />
      <div className="composer-bar">
        {!compact && (
          <Button className={`evidence-pill ${evidence ? 'on' : ''}`} onClick={() => setEvidence(!evidence)}>
            <i />
            Evidence {evidence ? 'on' : 'off'}
          </Button>
        )}
        <Button type="submit" className="send-button" label="Send question" disabled={disabled || !value.trim()}>
          <Icon name="send" />
        </Button>
      </div>
    </form>
  )
}

function Greeting({
  onAsk,
  onExample,
  examples,
  live,
  evidence,
  setEvidence,
}: {
  onAsk: (value: string) => void
  onExample: (example: Example) => void
  examples: Example[]
  live: boolean
  evidence: boolean
  setEvidence: (value: boolean) => void
}) {
  return (
    <main className="greeting">
      <Bloop size={180} />
      <div className="greeting-copy">
        <h1>Hello</h1>
        <p>I’m Bloop. Ask me something and we’ll look through the evidence together.</p>
      </div>
      <ChatComposer onSubmit={onAsk} evidence={evidence} setEvidence={setEvidence} />
      <div className="prompt-row">
        {examples.map((ex) => {
          const style = EXAMPLE_STYLE[ex.id] ?? { icon: 'check' as IconName, tone: 'mint' }
          const replayOnly = !live
          return (
            <Button
              key={ex.id}
              className={`prompt-chip tone-${style.tone}`}
              onClick={() => onExample(ex)}
              disabled={replayOnly && !ex.replay}
            >
              <span className="chip-icon">
                <Icon name={style.icon} />
              </span>
              {ex.label}
              {replayOnly && <small className="chip-note">{ex.replay ? 'Recorded run' : 'No recording yet'}</small>}
            </Button>
          )
        })}
      </div>
      <p className="promise">Bloop shows where every answer comes from. It can still get things wrong, so double-check what matters.</p>
    </main>
  )
}

function MessageHeader({ mood, label, tone }: { mood: BloopMood; label: string; tone: string }) {
  return (
    <div className="assistant-label">
      <Bloop mood={mood} size={46} className="bloop-small" />
      <b>Bloop</b>
      <span className={`status-pill tone-${tone}`}>{label}</span>
    </div>
  )
}

function inline(line: string): ReactNode[] {
  return line.split(/(\*\*[^*]+\*\*|_[^_]+_)/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) return <mark key={i}>{part.slice(2, -2)}</mark>
    if (part.length > 2 && part.startsWith('_') && part.endsWith('_')) return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

function Text({ text }: { text: string }) {
  return (
    <div className="answer-body">
      {text
        .split('\n')
        .filter((line) => line.trim())
        .map((line, i) => (
          <p key={i}>{inline(line)}</p>
        ))}
    </div>
  )
}

function ThinkingTrace({ steps, phase }: { steps: Step[]; phase: Phase }) {
  return (
    <div className="trace-steps">
      {steps.map((s, i) => {
        const header = s.stage !== steps[i - 1]?.stage ? STAGES[s.stage] ?? s.stage : null
        return (
          <div key={s.id} className="trace-item">
            {header && <div className="trace-stage">{header}</div>}
            <div className={`trace-step ${s.status}`}>
              <span className="trace-icon">
                {s.status === 'running' ? <i /> : s.status === 'ok' ? <Icon name="check" /> : s.status === 'warn' ? '!' : s.status === 'fail' ? '✕' : '•'}
              </span>
              <div>
                <div className="trace-title">{s.title}</div>
                {s.detail && <div className="trace-detail">{s.detail}</div>}
              </div>
            </div>
          </div>
        )
      })}
      {phase === 'thinking' && steps.length === 0 && <div className="trace-detail">Starting…</div>}
      {phase !== 'thinking' && phase !== 'error' && (
        <div className="trace-complete">
          <Icon name="check" /> Processing complete
        </div>
      )}
    </div>
  )
}

const CLAIM_TAGS: Record<string, [string, string]> = {
  graph: ['Graph', 'mint'],
  'graph-corrected': ['Corrected', 'sun'],
  model: ['Confirmed', 'sky'],
  cache: ['Cached', 'lav'],
}

function pathRow(p: PathItem): { title: string; sub: string; tag: string; tone: string } | null {
  if (p.kind === 'answer') {
    return { title: p.final_answer ?? '', sub: `Earlier answer to “${p.question ?? ''}”`, tag: 'Earlier', tone: 'lav' }
  }
  if (p.subject) {
    const title = `${p.subject} — ${(p.relation ?? '').replaceAll('_', ' ')} — ${p.object}`
    const status = p.status ?? 'accepted'
    if (status === 'rejected') return { title, sub: p.reason ?? '', tag: 'Dropped', tone: 'coral' }
    if (status === 'unverified') return { title, sub: p.reason ?? '', tag: 'Withheld', tone: 'sun' }
    if (status === 'skipped') return { title, sub: p.reason ?? '', tag: 'Skipped', tone: 'coral' }
    const [tag, tone] = CLAIM_TAGS[p.source ?? ''] ?? ['Verified', 'mint']
    return { title, sub: p.reason ?? 'verified earlier in this conversation', tag, tone }
  }
  if (p.text) {
    return { title: p.text, sub: p.expression ? `${p.expression} = ${p.result}` : '', tag: 'Checked', tone: 'mint' }
  }
  return null
}

const TIER_LINE: Record<string, string> = {
  factual: 'Every claim was checked against the knowledge graph before moving to the next one.',
  reasoning: 'Each step was machine-checked, contradictions were backtracked, and independent solutions were cross-checked.',
  consistency: 'Answered from statements this conversation already verified, re-checked against the graph.',
}

const STOP_REASONS: Record<string, string> = {
  nothing_verified: 'No claim passed verification, so Bloop declined rather than guess.',
  false_premise: 'The question assumes something the knowledge graph contradicts.',
  graph_conflict: 'The knowledge graph itself holds conflicting facts about this, so Bloop did not answer.',
  premise_unverified: 'The calculation needed a fact that could not be verified.',
  budget: 'The question needed more checking than one answer is allowed.',
}

const RISK_LABELS: Record<string, string> = {
  'factual.entity_error': 'Wrong entity, date or number',
  'factual.relation_error': 'Wrong relation between facts',
  'factual.fabrication': 'Unverified detail added',
  'factual.overclaim': 'False premise',
  'faithful.logic': 'Faulty step or calculation',
  'faithful.context': 'Contradicted an earlier answer',
  'faithful.premise': 'Calculation on an unverified fact',
  'temporal.stale': 'Possibly out of date',
}

function Verification({ final }: { final: Final }) {
  if (final.stopped) {
    return (
      <div className="verification verification-uncertain">
        <span className="status-icon">?</span>
        <div>
          <b>Stopped instead of guessing</b>
          <p>{STOP_REASONS[final.stopped.reason] ?? final.stopped.detail}</p>
        </div>
      </div>
    )
  }
  const high = final.confidence === 'high'
  const title = high ? 'Backed by the checks' : final.confidence === 'medium' ? 'Partly verified' : 'Could not fully verify'
  const reuse = final.compression?.mode
  const extra = final.from_cache
    ? ` Reused a verified path — ${final.tokens.saved.toLocaleString()} tokens saved.`
    : reuse === 'state_answer'
      ? 'Answered from facts verified for a similar question, re-checked against the graph and the faithfulness check.'
      : reuse === 'template'
        ? 'Adapted a verified solution to a similar problem; the adaptation was machine-checked and logic-checked, and the independent cross-check was skipped.'
        : final.reused_state
          ? ' Adapted a similar verified answer.'
          : ''
  return (
    <div className={`verification ${high ? '' : 'verification-uncertain'}`}>
      <span className="status-icon">{high ? <Icon name="check" /> : '?'}</span>
      <div>
        <b>{title}</b>
        <p>
          {reuse === 'template' || reuse === 'state_answer' ? '' : TIER_LINE[final.tier] ?? ''}
          {extra}
        </p>
      </div>
    </div>
  )
}

function Evidence({ final, showTrace }: { final: Final; showTrace: boolean }) {
  const [open, setOpen] = useState(false)
  const rows = final.path.map(pathRow).filter((r) => r !== null)
  const checked = final.path.length
  const verified = rows.filter((r) => !['Dropped', 'Withheld', 'Skipped'].includes(r.tag)).length
  const noun = final.tier === 'reasoning' ? 'step' : 'claim'
  const caught = final.caught.length
  return (
    <section className={`evidence ${open ? 'evidence-open' : ''}`}>
      <Button className="evidence-toggle" onClick={() => setOpen(!open)}>
        <span>
          <span className="link-tile">
            <Icon name="link" />
          </span>
          <b>Sources & checks</b>
          <small>
            {checked} {noun}
            {checked === 1 ? '' : 's'} checked · {caught} hallucination{caught === 1 ? '' : 's'} caught
          </small>
        </span>
        <span className="toggle-side">
          {open ? 'Hide' : 'Show'} <Icon name="chevron" />
        </span>
      </Button>
      {open && (
        <div className="evidence-content">
          <div className="evidence-stats">
            <span className="tone-mint">
              <b>{verified}</b> verified
            </span>
            <span className="tone-coral">
              <b>{caught}</b> caught
            </span>
            {final.withheld > 0 && (
              <span className="tone-sun">
                <b>{final.withheld}</b> withheld
              </span>
            )}
            <span className="tone-sky">
              <b>{final.tokens.total.toLocaleString()}</b> tokens
            </span>
          </div>
          {final.standalone && <p className="answered-as">Answered as: “{final.standalone}”</p>}
          {rows.map((r, i) => (
            <div className="source" key={i}>
              <span className={`source-num tone-${r.tone}`}>{i + 1}</span>
              <div>
                <b>{r.title}</b>
                {r.sub && <small>{r.sub}</small>}
              </div>
              <span className={`source-tag tone-${r.tone}`}>{r.tag}</span>
            </div>
          ))}
          {final.caught.map((c, i) => (
            <div className="source" key={`c${i}`}>
              <span className="source-num tone-coral">!</span>
              <div>
                <b>{(c.risk && RISK_LABELS[c.risk]) ?? `${c.type} hallucination`}</b>
                <small>{c.detail}</small>
              </div>
              <span className="source-tag tone-coral">{c.action}</span>
            </div>
          ))}
          {showTrace && (
            <div className="trace">
              <span>ROUTING</span> {final.hallucination_type} risk → {(final.plan ?? [final.tier]).join(' → ')} tier
              {final.mode ? ` (${final.mode})` : ''}
              {final.router?.learned ? ' · learned route' : ''}
              {final.classifier === 'fallback' ? ' · keyword fallback' : ''}
              {final.from_cache ? ' · cache hit' : ''}
              {final.degraded ? ' · graph snapshot' : ''}
              {final.compression?.context?.redrafted ? ' · trimmed context restored' : ''}
              {final.compression?.context?.history_expanded ? ' · full history used' : ''} <b>{final.tokens.calls} calls</b>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function headerFor(m: Assistant): [BloopMood, string, string] {
  if (m.phase === 'thinking') return ['thinking', m.recorded ? 'Recorded run' : 'Thinking', 'sky']
  if (m.phase === 'error') return ['uncertain', 'Something went wrong', 'coral']
  if (!m.final) return ['thinking', 'Processing complete', 'lav']
  if (m.recorded) return [m.final.confidence === 'high' ? 'supported' : 'uncertain', 'Recorded run', 'lav']
  if (m.final.stopped) return ['uncertain', 'Stopped instead of guessing', 'sun']
  if (m.final.confidence === 'high') return ['supported', 'Checked', 'mint']
  if (m.final.confidence === 'medium') return ['uncertain', 'Some uncertainty', 'sun']
  return ['uncertain', 'Not verified', 'coral']
}

function AssistantMessage({
  m,
  evidence,
  showTrace,
  onToggleThinking,
  onRate,
  onRetry,
}: {
  m: Assistant
  evidence: boolean
  showTrace: boolean
  onToggleThinking: () => void
  onRate: (helpful: boolean) => void
  onRetry: (question: string) => void
}) {
  const [mood, label, tone] = headerFor(m)
  return (
    <article className="answer">
      <MessageHeader mood={mood} label={label} tone={tone} />
      {m.recorded && <p className="recorded-note">Recorded run · {m.recorded}. Not a live answer.</p>}
      {m.phase === 'thinking' && (
        <div className="thinking-block">
          <div className="progress">
            <i />
            <i />
            <i />
          </div>
          <p>Checking every step before answering…</p>
        </div>
      )}
      <Button className={`see-thinking ${m.showThinking ? 'open' : ''}`} onClick={onToggleThinking}>
        {m.showThinking ? 'Hide the thinking' : 'See the thinking'} <small>{m.steps.length}</small>
        <Icon name="chevron" />
      </Button>
      {m.showThinking && <ThinkingTrace steps={m.steps} phase={m.phase} />}
      {m.phase !== 'thinking' && m.phase !== 'error' && !m.showThinking && (
        <div className="complete-line">
          <Icon name="check" /> Processing complete
        </div>
      )}
      {m.answer && <Text text={m.answer} />}
      {m.error && (
        <div className="chat-error">
          {m.error}
          {m.retry && (
            <Button className="feedback-btn retry-btn" onClick={() => onRetry(m.retry!)}>
              Try again
            </Button>
          )}
        </div>
      )}
      {m.final && (evidence || m.final.stopped) && <Verification final={m.final} />}
      {m.final && evidence && <Evidence final={m.final} showTrace={showTrace} />}
      {m.final && !m.recorded && (
        <div className="feedback">
          {m.feedback === undefined ? (
            <>
              <span>Was this right?</span>
              <Button className="feedback-btn" onClick={() => onRate(true)}>
                Yes
              </Button>
              <Button className="feedback-btn" onClick={() => onRate(false)}>
                No
              </Button>
            </>
          ) : (
            <span>Thanks — the router will learn from this.</span>
          )}
        </div>
      )}
    </article>
  )
}

function Settings({
  dreamy,
  setDreamy,
  onReplay,
  onClose,
  evidence,
  setEvidence,
  showTrace,
  setShowTrace,
  mode,
  setMode,
  autoIntro,
  setAutoIntro,
}: {
  dreamy: boolean
  setDreamy: (value: boolean) => void
  onReplay: () => void
  onClose: () => void
  evidence: boolean
  setEvidence: (value: boolean) => void
  showTrace: boolean
  setShowTrace: (value: boolean) => void
  mode: Mode
  setMode: (value: Mode) => void
  autoIntro: boolean
  setAutoIntro: (value: boolean) => void
}) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="settings-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="settings-head">
          <h2>Settings</h2>
          <Button className="icon-button" label="Close settings" onClick={onClose}>
            <Icon name="close" />
          </Button>
        </div>
        <p>Choose how Prism feels. Answers and evidence checks stay exactly the same.</p>
        <div className="mode-grid">
          <Button className={`mode-card ${dreamy ? 'selected' : ''}`} onClick={() => setDreamy(true)}>
            <span className="mode-orb dreamy-orb" />
            <span>
              <b>Dreamy</b>
              <small>Wave intro and gentle motion</small>
            </span>
            {dreamy && (
              <i>
                <Icon name="check" />
              </i>
            )}
          </Button>
          <Button className={`mode-card ${!dreamy ? 'selected' : ''}`} onClick={() => setDreamy(false)}>
            <span className="mode-orb still-orb" />
            <span>
              <b>Still</b>
              <small>Calm, motion-free</small>
            </span>
            {!dreamy && (
              <i>
                <Icon name="check" />
              </i>
            )}
          </Button>
        </div>
        <Button className="replay" onClick={onReplay}>
          <Icon name="replay" /> Replay the intro
        </Button>
        <div className="settings-row">
          <span>
            <b>Play the intro on start</b>
            <small>Off by default; remembered on this device</small>
          </span>
          <label className="switch">
            <input type="checkbox" checked={autoIntro} onChange={(e) => setAutoIntro(e.target.checked)} />
            <i />
          </label>
        </div>
        <div className="settings-row">
          <span>
            <b>Reasoning mode</b>
            <small>How hard maths and logic answers are cross-checked</small>
          </span>
          <select className="mode-select" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="auto">auto (balanced)</option>
            <option value="conservative">conservative</option>
            <option value="balanced">balanced</option>
            <option value="aggressive">aggressive</option>
          </select>
        </div>
        <div className="settings-row">
          <span>
            <b>Evidence details</b>
            <small>Show sources and checks after answers</small>
          </span>
          <label className="switch">
            <input type="checkbox" checked={evidence} onChange={(e) => setEvidence(e.target.checked)} />
            <i />
          </label>
        </div>
        <div className="settings-row">
          <span>
            <b>Technical trace</b>
            <small>Include model routing information</small>
          </span>
          <label className="switch">
            <input type="checkbox" checked={showTrace} onChange={(e) => setShowTrace(e.target.checked)} />
            <i />
          </label>
        </div>
      </div>
    </div>
  )
}

const pathView = (): View => (window.location.pathname.replace(/\/+$/, '') === '/results' ? 'results' : 'chat')

class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <main className="greeting">
        <Bloop mood="uncertain" size={150} />
        <div className="greeting-copy">
          <h1>Something broke</h1>
          <p>The page hit an unexpected error. Reloading usually fixes it.</p>
        </div>
        <Button className="feedback-btn" onClick={() => window.location.reload()}>
          Reload
        </Button>
      </main>
    )
  }
}

function PrismApp() {
  const prefersStill = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [dreamy, setDreamy] = useState(() => stored('prism-dreamy') !== 'off' && !prefersStill)
  const [autoIntro, setAutoIntroState] = useState(() => stored(INTRO_KEY) === 'on')
  const [intro, setIntro] = useState(() => !prefersStill && stored(INTRO_KEY) === 'on')
  const [settings, setSettings] = useState(false)
  const [evidence, setEvidence] = useState(true)
  const [showTrace, setShowTrace] = useState(true)
  const [messages, setMessages] = useState<Message[]>([])
  const [mode, setMode] = useState<Mode>('auto')
  const [busy, setBusy] = useState(false)
  const [view, setViewState] = useState<View>(pathView)
  const [conversationId, setConversationId] = useState(newConversationId)
  const [status, setStatus] = useState<Status | null>(null)
  const [examples, setExamples] = useState<Example[]>(FALLBACK_EXAMPLES)
  const nextId = useRef(1)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages])

  useEffect(() => {
    let alive = true
    const poll = () =>
      getStatus()
        .then((s) => {
          if (alive) setStatus(s)
        })
        .catch(() => {
          if (alive) setStatus(null)
        })
    poll()
    const id = window.setInterval(poll, 60000)
    getExamples()
      .then((ex) => {
        if (alive && ex.length) setExamples(ex)
      })
      .catch(() => undefined)
    const onPop = () => setViewState(pathView())
    window.addEventListener('popstate', onPop)
    return () => {
      alive = false
      window.clearInterval(id)
      window.removeEventListener('popstate', onPop)
    }
  }, [])

  useEffect(() => {
    document.title = view === 'results' ? 'Prism · Results' : view === 'metrics' ? 'Prism · Metrics' : 'Prism · verified answers'
  }, [view])

  function setView(next: View) {
    const path = next === 'results' ? '/results' : '/'
    if (window.location.pathname !== path) window.history.pushState(null, '', path)
    setViewState(next)
  }

  const finishIntro = useCallback(() => setIntro(false), [])
  function replayIntro() {
    setSettings(false)
    setDreamy(true)
    setIntro(true)
  }
  function updateMode(value: boolean) {
    setDreamy(value)
    store('prism-dreamy', value ? 'on' : 'off')
    if (!value) finishIntro()
  }
  function setAutoIntro(value: boolean) {
    setAutoIntroState(value)
    store(INTRO_KEY, value ? 'on' : 'off')
  }

  const update = (id: number, fn: (m: Assistant) => Partial<Assistant>) =>
    setMessages((prev) => prev.map((m) => (m.id === id && m.role === 'assistant' ? { ...m, ...fn(m) } : m)))

  function addTurn(question: string, recorded?: string) {
    const qid = nextId.current++
    const aid = nextId.current++
    setMessages((prev) => [
      ...prev,
      { id: qid, role: 'user', text: question },
      { id: aid, role: 'assistant', steps: [], phase: 'thinking', answer: '', showThinking: false, recorded },
    ])
    return aid
  }

  function handler(aid: number, gap: number) {
    const seen = new Set<string>()
    return async (ev: ChatEvent) => {
      switch (ev.type) {
        case 'step': {
          const isNew = !seen.has(ev.id)
          seen.add(ev.id)
          update(aid, (m) => ({
            steps: isNew ? [...m.steps, ev] : m.steps.map((s) => (s.id === ev.id ? ev : s)),
          }))
          if (isNew && gap) await sleep(gap)
          break
        }
        case 'thinking_done':
          update(aid, () => ({ phase: 'answering' }))
          await sleep(gap ? 400 : 0)
          break
        case 'answer':
          update(aid, (m) => ({ answer: m.answer + ev.delta }))
          break
        case 'final':
          update(aid, () => ({ phase: 'done', final: ev }))
          break
        case 'error':
          update(aid, (m) => ({
            phase: 'error',
            error: ev.message,
            steps: m.steps.map((s) => (s.status === 'running' ? { ...s, status: 'fail' } : s)),
          }))
          break
      }
    }
  }

  async function ask(question: string, convId = conversationId) {
    if (!question) return
    setView('chat')
    const aid = addTurn(question)
    setBusy(true)
    try {
      await streamChat(question, mode, convId, handler(aid, STEP_GAP_MS))
    } catch (err) {
      const dropped = err instanceof StreamDropped
      const message = err instanceof Error ? err.message : String(err)
      update(aid, (m) => ({
        phase: 'error',
        error: dropped ? `${message} The answer may still have been logged.` : message,
        retry: question,
        steps: m.steps.map((s) => (s.status === 'running' ? { ...s, status: 'fail' } : s)),
      }))
    } finally {
      update(aid, (m) => (m.phase === 'answering' ? { phase: 'done' } : {}))
      setBusy(false)
    }
  }

  async function playReplay(ex: Example) {
    setView('chat')
    setBusy(true)
    try {
      const rec = await getReplay(ex.id)
      const when = new Date(rec.recorded_at * 1000).toLocaleDateString()
      for (const turn of rec.turns) {
        const aid = addTurn(turn.question, `${rec.model || 'model'}, ${when}`)
        const onEvent = handler(aid, 0)
        let last = 0
        for (const { t, event } of turn.events) {
          await sleep(Math.min(700, Math.max(0, (t - last) * 1000)))
          last = t
          await onEvent(event)
        }
      }
    } catch (err) {
      const aid = addTurn(ex.turns[0], 'unavailable')
      update(aid, () => ({ phase: 'error', error: `The recorded run could not be loaded (${String(err)}).` }))
    } finally {
      setBusy(false)
    }
  }

  async function runExample(ex: Example) {
    if (busy) return
    if (!liveModel(status)) {
      if (ex.replay) await playReplay(ex)
      return
    }
    let conv = newConversationId()
    setMessages([])
    setConversationId(conv)
    for (const [i, q] of ex.turns.entries()) {
      if (ex.separate && i > 0) {
        conv = newConversationId()
        setConversationId(conv)
      }
      await ask(q, conv)
    }
  }

  async function rate(id: number, turnId: number, helpful: boolean) {
    try {
      await sendFeedback(turnId, helpful)
      update(id, () => ({ feedback: helpful }))
    } catch (err) {
      update(id, () => ({ error: `Feedback not saved: ${String(err)}` }))
    }
  }

  function newChat() {
    if (busy) return
    setMessages([])
    setConversationId(newConversationId())
    setView('chat')
  }

  const live = liveModel(status)
  return (
    <div className={`app ${dreamy ? 'dreamy' : 'still'}`}>
      {intro && <Intro onSkip={finishIntro} />}
      <header className="nav">
        <Button className="brand" label="New chat" onClick={newChat}>
          <PrismMark />
          <b>Prism</b>
          <small>with Bloop</small>
        </Button>
        <div className="nav-right">
          <nav className="view-tabs">
            <Button className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')}>
              Chat
            </Button>
            <Button className={view === 'metrics' ? 'active' : ''} onClick={() => setView('metrics')}>
              Metrics
            </Button>
            <Button className={view === 'results' ? 'active' : ''} onClick={() => setView('results')}>
              Results
            </Button>
          </nav>
          {evidence && (
            <span className="evidence-on">
              <i /> Evidence mode on
            </span>
          )}
          <Button className="icon-button" label="Open settings" onClick={() => setSettings(true)}>
            <Icon name="settings" />
          </Button>
        </div>
      </header>

      {view === 'chat' && <Banner status={status} />}

      {view === 'metrics' && (
        <main className="metrics-view">
          <Suspense fallback={<div className="metrics"><p className="card-note">Loading…</p></div>}>
            <MetricsView />
          </Suspense>
        </main>
      )}

      {view === 'results' && (
        <main className="metrics-view">
          <Suspense fallback={<div className="metrics"><p className="card-note">Loading…</p></div>}>
            <ResultsView />
          </Suspense>
        </main>
      )}

      {view === 'chat' && messages.length === 0 && (
        <Greeting
          onAsk={(q) => ask(q)}
          onExample={runExample}
          examples={examples}
          live={live}
          evidence={evidence}
          setEvidence={setEvidence}
        />
      )}

      {view === 'chat' && messages.length > 0 && (
        <main className="conversation">
          <div className="conversation-inner">
            {messages.map((m) =>
              m.role === 'user' ? (
                <div key={m.id} className="user-message">
                  {m.text}
                </div>
              ) : (
                <AssistantMessage
                  key={m.id}
                  m={m}
                  evidence={evidence}
                  showTrace={showTrace}
                  onToggleThinking={() => update(m.id, (a) => ({ showThinking: !a.showThinking }))}
                  onRate={(helpful) => m.final && rate(m.id, m.final.turn_id, helpful)}
                  onRetry={(q) => ask(q)}
                />
              ),
            )}
            <div ref={bottom} />
          </div>
          <div className="sticky-composer">
            <ChatComposer compact onSubmit={(q) => ask(q)} disabled={busy} evidence={evidence} setEvidence={setEvidence} />
          </div>
        </main>
      )}

      {settings && (
        <Settings
          dreamy={dreamy}
          setDreamy={updateMode}
          onReplay={replayIntro}
          onClose={() => setSettings(false)}
          evidence={evidence}
          setEvidence={setEvidence}
          showTrace={showTrace}
          setShowTrace={setShowTrace}
          mode={mode}
          setMode={setMode}
          autoIntro={autoIntro}
          setAutoIntro={setAutoIntro}
        />
      )}
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <PrismApp />
    </ErrorBoundary>
  )
}
