async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const type = response.headers.get('content-type') || ''
  const payload = type.includes('application/json') ? await response.json() : null
  if (!response.ok) {
    const detail = payload?.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || (Array.isArray(detail) ? detail.join('; ') : `Request failed (${response.status})`)
    const error = new Error(message)
    error.detail = detail
    throw error
  }
  return payload
}

// Hosted demo (Vercel): an interactive SANDBOX. The data is a frozen snapshot of a real run
// (`python main.py snapshot`). Review, edit, approve, simulated send and duplicate protection work
// with key safety gates in this browser; the full Python validators and pipeline remain local. Nothing is transmitted and there is
// no backend or secret. The real pipeline (discovery, LLM calls, SMTP) runs locally.
export const isDemo = import.meta.env.VITE_DEMO === '1'
const NEEDS_LOCAL = 'The pipeline needs API keys and runs locally. See the README to run it.'
const STORE = 'edxso-sandbox-v1'

const blank = () => ({ reviews: {}, edits: {}, events: [] })
let memory = null
function state() {
  if (memory) return memory
  try { memory = JSON.parse(localStorage.getItem(STORE)) || blank() } catch { memory = blank() }
  return memory
}
function persist() { try { localStorage.setItem(STORE, JSON.stringify(memory)) } catch { /* private mode: keep in memory */ } }
export function resetSandbox() { memory = blank(); persist(); window.location.reload() }
export const sandboxChanges = () => {
  const { reviews, events } = state()
  return Object.keys(reviews).length + events.length
}

async function snapshot(file) {
  const response = await fetch(`/demo/${file}`)
  if (!response.ok) throw new Error(response.status === 404 ? 'Detail is not included in the demo snapshot.' : `Request failed (${response.status})`)
  return response.json()
}

const wordCount = (text) => (text.match(/[\p{L}\p{N}’'-]+/gu) || []).length
const validEmail = (value) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value || '')

function withLocal(detail) {
  const local = state()
  const id = detail.profile.channel_id
  const message = detail.message ? { ...detail.message, ...(local.edits[id] || {}) } : null
  if (message && local.reviews[id]) message.review_status = local.reviews[id]
  const mine = local.events.filter((event) => event.channel_id === id).sort((a, b) => b.id - a.id)
  return { ...detail, message, outreach: [...mine, ...detail.outreach] }
}

async function allEvents() {
  const base = await snapshot('outreach.json')
  return [...state().events, ...base].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
}

function logEvent(profile, channel, method, status, recipient) {
  const local = state()
  local.events.push({
    id: 1000000 + local.events.length + 1, created_at: new Date().toISOString(), channel, method, status,
    recipient, error: null, channel_id: profile.channel_id, name: profile.name, profile_url: profile.profile_url,
  })
  persist()
}

const demoApi = {
  summary: async () => {
    const base = await snapshot('summary.json')
    const local = state()
    let approved = base.approved
    for (const [id, review] of Object.entries(local.reviews)) {
      const original = (await snapshot(`creators/${encodeURIComponent(id)}.json`).catch(() => null))?.message?.review_status
      approved += (review === 'APPROVED' ? 1 : 0) - (original === 'APPROVED' ? 1 : 0)
    }
    const sent = local.events.filter((event) => event.status === 'SIMULATED_SENT').length
    return { ...base, approved, simulated_sent: base.simulated_sent + sent }
  },
  config: () => snapshot('config.json'),
  credentials: () => Promise.resolve({ YOUTUBE_API_KEY: true, GROQ_API_KEY: false, GEMINI_API_KEY: true }),
  creators: async ({ status = 'ALL' } = {}) => {
    const all = await snapshot('creators.json')
    return status === 'ALL' ? all : all.filter((creator) => creator.filter_status === status)
  },
  creator: async (id) => withLocal(await snapshot(`creators/${encodeURIComponent(id)}.json`)),
  outreach: allEvents,
  runStatus: () => Promise.resolve({ status: 'complete', stage: null, detail: 'Snapshot of a completed real run. Re-running needs API keys, so it is done locally.', results: {} }),
  startRun: () => Promise.reject(new Error(NEEDS_LOCAL)),
  exportAll: () => Promise.resolve({}),
  editMessage: async (id, body) => {
    const detail = withLocal(await snapshot(`creators/${encodeURIComponent(id)}.json`))
    if (detail.outreach.some((event) => event.status === 'SIMULATED_SENT')) throw new Error('This message already has a simulated send; its history cannot be edited')
    const errors = []
    const email = wordCount(body.email_body), dm = wordCount(body.dm)
    if (email < 60 || email > 90) errors.push(`email_body has ${email} words; expected 60–90`)
    if (dm < 15 || dm > 30) errors.push(`dm has ${dm} words; expected 15–30`)
    if (!detail.videos.some((video) => video.video_id === body.referenced_video_id)) errors.push('referenced_video_id does not belong to this creator')
    if (errors.length) { const error = new Error(errors.join('; ')); error.detail = errors; throw error }
    const local = state()
    local.edits[id] = { subject: body.subject.trim(), email_body: body.email_body.trim(), dm: body.dm.trim(), referenced_video_id: body.referenced_video_id }
    local.reviews[id] = 'PENDING_REVIEW'
    persist()
    return { status: 'PENDING_REVIEW' }
  },
  action: async (id, kind) => {
    const detail = withLocal(await snapshot(`creators/${encodeURIComponent(id)}.json`))
    const { profile, message } = detail
    const local = state()
    if (kind === 'approve' || kind === 'reject') {
      if (!message) throw new Error('NO_MESSAGE')
      if (kind === 'approve' && message.validation_status !== 'VALID') throw new Error('INVALID_MESSAGE')
      local.reviews[id] = kind === 'approve' ? 'APPROVED' : 'REJECTED'
      persist()
      return { status: kind === 'approve' ? 'APPROVED' : 'REJECTED' }
    }
    if (!message || profile.filter_status !== 'PASSED') return { status: 'NOT_ELIGIBLE' }
    const events = await allEvents()
    if (kind === 'simulate-send') {
      if (!validEmail(profile.email)) { logEvent(profile, 'email', 'dry_run', 'SKIPPED_NO_EMAIL', profile.email); return { status: 'SKIPPED_NO_EMAIL' } }
      if (message.review_status !== 'APPROVED' || message.validation_status !== 'VALID') return { status: 'NOT_APPROVED' }
      const recipient = profile.email.trim().toLowerCase()
      const duplicate = events.some((event) => event.status === 'SIMULATED_SENT' && (event.recipient || '').toLowerCase() === recipient)
      logEvent(profile, 'email', 'dry_run', duplicate ? 'DUPLICATE_BLOCKED' : 'SIMULATED_SENT', recipient)
      return { status: duplicate ? 'DUPLICATE_BLOCKED' : 'SIMULATED_SENT' }
    }
    if (kind === 'record-dm') {
      if (!profile.instagram_url) return { status: 'NO_PUBLIC_INSTAGRAM_URL' }
      if (message.review_status !== 'APPROVED') return { status: 'NOT_APPROVED' }
      const duplicate = events.some((event) => event.status === 'MANUAL_DM_RECORDED' && event.recipient === profile.instagram_url)
      logEvent(profile, 'instagram_dm', 'manual', duplicate ? 'DUPLICATE_BLOCKED' : 'MANUAL_DM_RECORDED', profile.instagram_url)
      return { status: duplicate ? 'DUPLICATE_BLOCKED' : 'MANUAL_DM_RECORDED' }
    }
    throw new Error('Unknown action')
  },
}

const liveApi = {
  summary: () => request('/api/summary'),
  config: () => request('/api/config'),
  credentials: () => request('/api/credentials'),
  creators: ({ status = 'ALL', search = '' } = {}) =>
    request(`/api/creators?${new URLSearchParams({ status, search })}`),
  creator: (id) => request(`/api/creators/${encodeURIComponent(id)}`),
  outreach: () => request('/api/outreach'),
  runStatus: () => request('/api/run'),
  startRun: () => request('/api/run', { method: 'POST' }),
  exportAll: () => request('/api/exports', { method: 'POST' }),
  editMessage: (id, body) => request(`/api/creators/${encodeURIComponent(id)}/message`, {
    method: 'PUT', body: JSON.stringify(body),
  }),
  action: (id, action) => request(`/api/creators/${encodeURIComponent(id)}/${action}`, { method: 'POST' }),
}

export const api = isDemo ? demoApi : liveApi
