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

// Hosted demo (Vercel): the UI reads frozen JSON written by `python main.py snapshot`.
// There is no backend, so nothing can be approved, sent, or run from the public site.
export const isDemo = import.meta.env.VITE_DEMO === '1'
const READ_ONLY = 'Read-only demo. Clone the repository and run it locally to review, approve and send.'

async function snapshot(file) {
  const response = await fetch(`/demo/${file}`)
  if (!response.ok) throw new Error(response.status === 404 ? 'Detail is not included in the demo snapshot.' : `Request failed (${response.status})`)
  return response.json()
}

const demoApi = {
  summary: () => snapshot('summary.json'),
  config: () => snapshot('config.json'),
  credentials: () => Promise.resolve({ YOUTUBE_API_KEY: true, GROQ_API_KEY: false, GEMINI_API_KEY: true }),
  creators: async ({ status = 'ALL' } = {}) => {
    const all = await snapshot('creators.json')
    return status === 'ALL' ? all : all.filter((creator) => creator.filter_status === status)
  },
  creator: (id) => snapshot(`creators/${encodeURIComponent(id)}.json`),
  outreach: () => snapshot('outreach.json'),
  runStatus: () => Promise.resolve({ status: 'complete', stage: null, detail: 'Read-only snapshot of a completed run.', results: {} }),
  startRun: () => Promise.reject(new Error(READ_ONLY)),
  exportAll: () => Promise.resolve({}),
  editMessage: () => Promise.reject(new Error(READ_ONLY)),
  action: () => Promise.reject(new Error(READ_ONLY)),
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
