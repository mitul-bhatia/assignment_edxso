import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowDownToLine, ArrowRight, BadgeCheck, Check, CheckCircle2,
  ChevronDown, ChevronLeft, ChevronRight, CircleAlert, Clock3, ExternalLink,
  FileText, Filter, Globe2, KeyRound, LayoutDashboard, Mail, Menu, Play,
  RefreshCw, Search, Send, ShieldCheck, Sparkles, Users, X,
} from 'lucide-react'
import { api, isDemo } from './api'

const emptySummary = { discovered: 0, passed: 0, failed: 0, review_required: 0,
  contactable: 0, drafts: 0, approved: 0, simulated_sent: 0 }
const statusLabels = { PASSED: 'Qualified', FAILED: 'Not qualified',
  REVIEW_REQUIRED: 'Needs review', DISCOVERED: 'Discovered' }
const eventLabels = { SIMULATED_SENT: 'Simulated send', SKIPPED_NO_EMAIL: 'No email',
  DUPLICATE_BLOCKED: 'Duplicate blocked', MANUAL_DM_RECORDED: 'Manual DM recorded',
  SENT: 'Sent', SENDING: 'Sending', SEND_FAILED: 'Send failed (retryable)',
  SEND_UNCERTAIN: 'Delivery uncertain — check mailbox', SUPPRESSED: 'Suppressed (opted out)' }
const contactKinds = { channel_description: 'Channel description (first-party)', website: 'Creator website',
  video_description: 'Video description (verify before sending)', manual_verified: 'Manually verified' }
const basisLabels = { long_form: 'long-form videos', all_formats: 'all formats (Shorts-heavy)' }

function number(value) {
  return value == null ? '—' : new Intl.NumberFormat('en-US').format(value)
}

function percent(value) {
  return value == null ? '—' : `${Number(value).toFixed(2)}%`
}

function date(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  })
}

function initials(name) {
  return (name || '?').split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase()
}

function StatusPill({ status }) {
  return <span className={`status-pill status-${(status || 'unknown').toLowerCase()}`}>
    <span className="status-dot" />{statusLabels[status] || status || 'Unknown'}
  </span>
}

function StatCard({ icon: Icon, label, value, note, tone = '' }) {
  return <div className={`stat-card ${tone}`}>
    <div className="stat-top"><span>{label}</span><Icon size={18} strokeWidth={1.8} /></div>
    <strong>{number(value)}</strong><small>{note}</small>
  </div>
}

function EmptyState({ query, onRun }) {
  return <div className="empty-state">
    <div className="empty-icon"><Search size={26} strokeWidth={1.7} /></div>
    <h3>{query ? 'No creators match these filters' : 'Your research workspace is ready'}</h3>
    <p>{query
      ? 'Try a different search or status to see more results.'
      : 'Connect the API keys, then start discovery. Real creator profiles, evidence, and messages will appear here—no sample data is invented.'}</p>
    {!query && <button className="button primary" onClick={onRun}>Set up a run <ArrowRight size={16} /></button>}
  </div>
}

function CreatorDetail({ data, onClose, onMutation, busy, notify }) {
  const [tab, setTab] = useState('evidence')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null)
  const profile = data.profile
  const message = data.message
  const video = data.videos.find((item) => item.video_id === message?.referenced_video_id)

  useEffect(() => {
    setTab('evidence')
    setEditing(false)
    setDraft(message ? {
      subject: message.subject, email_body: message.email_body,
      dm: message.dm, referenced_video_id: message.referenced_video_id,
    } : null)
  }, [profile.channel_id, message?.updated_at])

  const handleAction = async (action, success) => {
    try {
      const result = await onMutation(() => api.action(profile.channel_id, action))
      const outcome = result.status.replaceAll('_', ' ').toLowerCase()
      const caution = ['NOT_ELIGIBLE', 'NOT_APPROVED', 'SKIPPED_NO_EMAIL',
        'DUPLICATE_BLOCKED', 'NO_PUBLIC_INSTAGRAM_URL'].includes(result.status)
      notify(`${success}: ${outcome}`, caution ? 'error' : 'success')
    } catch (error) { notify(error.message, 'error') }
  }

  const save = async () => {
    try {
      await onMutation(() => api.editMessage(profile.channel_id, draft))
      setEditing(false)
      notify('Draft saved. Review approval has been reset.', 'success')
    } catch (error) { notify(error.message, 'error') }
  }

  return <aside className="detail-panel" aria-label={`${profile.name} details`}>
    <div className="detail-header">
      <div className="detail-topline"><span>CREATOR PROFILE</span><button className="icon-button" onClick={onClose} aria-label="Close details"><X size={20} /></button></div>
      <div className="detail-identity"><div className="avatar large">{initials(profile.name)}</div><div><h2>{profile.name}</h2><a href={profile.profile_url} target="_blank" rel="noreferrer">YouTube channel <ExternalLink size={13} /></a></div></div>
      <div className="detail-status"><StatusPill status={profile.filter_status} /><span>Fit score <b>{profile.fit_score == null ? '—' : `${profile.fit_score}/100`}</b></span>{profile.brand_tier && <span className={`tier-badge tier-${profile.brand_tier.toLowerCase()}`}>{profile.brand_tier === 'PRIORITY' ? 'Priority · K-12 fit' : 'Standard tier'}</span>}</div>
      <div className="detail-tabs" role="tablist" aria-label="Creator details">
        <button className={tab === 'evidence' ? 'active' : ''} onClick={() => setTab('evidence')} role="tab" aria-selected={tab === 'evidence'}>Evidence</button>
        <button className={tab === 'outreach' ? 'active' : ''} onClick={() => setTab('outreach')} role="tab" aria-selected={tab === 'outreach'}>Outreach{message && <span className="tab-indicator" />}</button>
      </div>
    </div>
    <div className="detail-scroll">
      {tab === 'evidence' ? <>
        <div className="detail-metrics"><div><span>Subscribers</span><strong>{number(profile.subscribers)}</strong></div><div><span>Engagement proxy</span><strong>{percent(profile.engagement_rate)}</strong></div><div><span>Video samples</span><strong>{number(profile.metric_samples)}</strong></div><div><span>Median views</span><strong>{number(profile.median_views)}</strong></div><div><span>Reach / subs</span><strong>{profile.reach_ratio == null ? '—' : `${(profile.reach_ratio * 100).toFixed(1)}%`}</strong></div></div>{profile.engagement_basis && profile.engagement_basis !== 'insufficient' && <p className="fine-print">Rates computed from {basisLabels[profile.engagement_basis] || profile.engagement_basis}; videos under the view floor are excluded as statistical noise.</p>}
        <section className="detail-section"><h3>Classification reasoning</h3><p className="fine-print">Technology is the declared niche; education relevance is a brand-fit signal.</p>
          {profile.filter_reasons?.length ? <ul className="reason-list">{profile.filter_reasons.map((reason, i) => <li key={i}><CircleAlert size={15} />{reason}</li>)}</ul> : <p className="muted">No rejection reasons recorded.</p>}
          {profile.classification_evidence_ids?.length > 0 && <div className="evidence-links"><span>MODEL-CITED VIDEOS</span>{profile.classification_evidence_ids.map((id) => { const item = data.videos.find((candidate) => candidate.video_id === id); return item ? <a href={item.url} target="_blank" rel="noreferrer" key={id}>{item.title} <ExternalLink size={12} /></a> : <small key={id}>{id}</small> })}</div>}
        </section>
        <section className="detail-section"><h3>Content context</h3><div className="chips">{profile.themes?.length ? profile.themes.map((theme) => <span className="chip" key={theme}>{theme}</span>) : <span className="muted">No themes assessed yet</span>}</div>{profile.tone && <p className="context-note">Tone: {profile.tone}</p>}{profile.creator_type && <p className="context-note">Account type: {profile.creator_type.replaceAll('_', ' ')}</p>}{profile.classification_provider && <p className="context-note">Classified with {profile.classification_provider}</p>}</section>
        <section className="detail-section"><h3>Contact provenance</h3><div className="info-rows"><div><span>Email</span><b>{profile.email || 'Not Found'}</b></div><div><span>Confidence</span><b>{contactKinds[profile.email_source_kind] || (profile.email && profile.email !== 'Not Found' ? 'Source recorded' : 'Not available')}</b></div><div><span>Source</span>{profile.email_source ? <a href={profile.email_source} target="_blank" rel="noreferrer">View public source <ExternalLink size={13} /></a> : <b>Not available</b>}</div><div><span>Country</span><b>{profile.channel_country || 'Not Available'}</b></div><div><span>Audience demographics</span><b>Not Available</b></div></div></section>
        <section className="detail-section"><h3>Recent public videos</h3>{data.videos.length ? <div className="video-list">{data.videos.slice(0, 5).map((item) => <a href={item.url} target="_blank" rel="noreferrer" key={item.video_id}><span><b>{item.title}</b><small>{date(item.published_at)} · {number(item.views)} views</small></span><ExternalLink size={15} /></a>)}</div> : <p className="muted">No video evidence stored.</p>}</section>
      </> : <>
        {!message ? <div className="no-draft"><Sparkles size={24} /><h3>No draft yet</h3><p>Qualified creators receive AI-generated email and DM drafts during personalization.</p></div> : <>
          <div className="review-line"><span className={`review-badge review-${message.review_status.toLowerCase()}`}>{message.review_status.replaceAll('_', ' ')}</span><small>Updated {date(message.updated_at)}</small></div>
          <section className="detail-section"><div className="section-heading"><h3>Email pitch</h3>{!editing && <button className="text-button" onClick={() => setEditing(true)} disabled={data.outreach.some((event) => event.status === 'SIMULATED_SENT')}>Edit draft</button>}</div>
            {editing ? <><label className="form-label">Subject<input value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} /></label><label className="form-label">Email body<textarea rows="9" value={draft.email_body} onChange={(e) => setDraft({ ...draft, email_body: e.target.value })} /></label></>
              : <div className="message-preview"><strong>{message.subject}</strong><p>{message.email_body}</p></div>}
          </section>
          <section className="detail-section"><h3>Instagram DM</h3>{editing ? <label className="form-label">DM text<textarea rows="4" value={draft.dm} onChange={(e) => setDraft({ ...draft, dm: e.target.value })} /></label> : <p className="dm-preview">{message.dm}</p>}
            {profile.instagram_url && <a className="subtle-link" href={profile.instagram_url} target="_blank" rel="noreferrer">Open Instagram profile <ExternalLink size={13} /></a>}
          </section>
          <section className="detail-section"><h3>Grounding & validation</h3><div className="info-rows"><div><span>Referenced video</span>{video ? <a href={video.url} target="_blank" rel="noreferrer">{video.title} <ExternalLink size={13} /></a> : <b>{message.referenced_video_id}</b>}</div><div><span>Collaboration angle</span><b>{(message.collaboration_type || 'not recorded').replaceAll('_', ' ')}</b></div><div><span>Prompt version</span><b>{message.prompt_version}</b></div><div><span>Generation model</span><b>{message.generation_model || 'Not recorded'}</b></div><div><span>Validation</span><b>{message.validation_status}</b></div></div>{editing && <label className="form-label">Referenced video ID<select value={draft.referenced_video_id} onChange={(e) => setDraft({ ...draft, referenced_video_id: e.target.value })}>{data.videos.map((item) => <option key={item.video_id} value={item.video_id}>{item.title}</option>)}</select></label>}</section>
          {editing ? <div className="detail-actions"><button className="button secondary" onClick={() => setEditing(false)}>Cancel</button><button className="button primary" onClick={save} disabled={busy}>Save & reset review</button></div> : <div className="detail-actions wrap">
            <button className="button secondary" onClick={() => handleAction('reject', 'Review')} disabled={busy || message.review_status === 'REJECTED'}>Reject</button>
            <button className="button primary" onClick={() => handleAction('approve', 'Review')} disabled={busy || message.review_status === 'APPROVED'}><Check size={16} /> Approve</button>
            <button className="button outline" onClick={() => handleAction('simulate-send', 'Outreach')} disabled={busy || message.review_status !== 'APPROVED'}><Send size={15} /> Simulate email</button>
            {profile.instagram_url && <button className="button outline" onClick={() => handleAction('record-dm', 'Outreach')} disabled={busy || message.review_status !== 'APPROVED'}>Record manual DM</button>}
          </div>}
          <p className="safety-note"><ShieldCheck size={15} /> No live email or DM is sent from this prototype.</p>
        </>}
      </>}
    </div>
  </aside>
}

function App() {
  const [view, setView] = useState('research')
  const [summary, setSummary] = useState(emptySummary)
  const [config, setConfig] = useState(null)
  const [credentials, setCredentials] = useState(null)
  const [creators, setCreators] = useState([])
  const [events, setEvents] = useState([])
  const [run, setRun] = useState({ status: 'idle', results: {} })
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [status, setStatus] = useState('ALL')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState(null)
  const [mobileNav, setMobileNav] = useState(false)

  const notify = (message, type = 'success') => setToast({ message, type })
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 5000); return () => clearTimeout(timer) }, [toast])

  const refresh = useCallback(async () => {
    try {
      const [nextSummary, nextConfig, nextCredentials, nextCreators, nextEvents, nextRun] = await Promise.all([
        api.summary(), api.config(), api.credentials(), api.creators(), api.outreach(), api.runStatus(),
      ])
      setSummary(nextSummary); setConfig(nextConfig); setCredentials(nextCredentials)
      setCreators(nextCreators); setEvents(nextEvents); setRun(nextRun); setError('')
    } catch (cause) { setError(cause.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    if (run.status !== 'running') return
    const timer = setInterval(refresh, 3500)
    return () => clearInterval(timer)
  }, [run.status, refresh])
  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    let current = true
    api.creator(selectedId).then((result) => { if (current) setDetail(result) })
      .catch((cause) => { if (current) notify(cause.message, 'error') })
    return () => { current = false }
  }, [selectedId, summary, events.length])

  const filtered = useMemo(() => creators.filter((creator) => {
    const matchesStatus = status === 'ALL' || creator.filter_status === status
    const text = `${creator.name} ${creator.channel_id} ${creator.themes?.join(' ')}`.toLowerCase()
    return matchesStatus && text.includes(search.trim().toLowerCase())
  }), [creators, status, search])

  const mutate = async (operation) => {
    setBusy(true)
    try {
      const result = await operation()
      await refresh()
      if (selectedId) setDetail(await api.creator(selectedId))
      return result
    } finally { setBusy(false) }
  }

  const startRun = async () => {
    try {
      await mutate(api.startRun)
      setView('run')
      notify('Pipeline started. This page updates as stages complete.')
    } catch (cause) { notify(cause.detail?.missing_keys ? `Missing keys: ${cause.detail.missing_keys.join(', ')}` : cause.message, 'error') }
  }

  const exportFiles = async () => {
    try { await mutate(api.exportAll); notify('Three CSV exports are ready to download.') }
    catch (cause) { notify(cause.message, 'error') }
  }

  const navigate = (next) => { setView(next); setSelectedId(null); setMobileNav(false) }
  const navItems = [
    { id: 'research', label: 'Research', icon: LayoutDashboard },
    { id: 'outreach', label: 'Outreach tracker', icon: Mail },
    { id: 'run', label: 'Run & exports', icon: Activity },
  ]

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? 'open' : ''}`}>
      <div className="brand"><div className="brand-mark"><Sparkles size={20} /></div><div><strong>Signal / Studio</strong><span>OUTREACH INTELLIGENCE</span></div></div>
      <div className="sidebar-label">WORKSPACE</div>
      <nav aria-label="Main navigation">{navItems.map(({ id, label, icon: Icon }) => <button key={id} className={`nav-item ${view === id ? 'active' : ''}`} onClick={() => navigate(id)}><Icon size={19} strokeWidth={1.8} /><span>{label}</span>{id === 'outreach' && events.length > 0 && <small>{events.length}</small>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="workspace-card"><div className="workspace-icon"><ShieldCheck size={17} /></div><strong>Responsible by design</strong><p>Human review and dry-run delivery are built into this workspace.</p></div><div className="sidebar-foot"><span className="live-dot" /> LOCAL PROTOTYPE <span>v1.0</span></div></div>
    </aside>
    {mobileNav && <button className="nav-scrim" onClick={() => setMobileNav(false)} aria-label="Close menu" />}
    <main className="main-content">
      <header className="topbar"><button className="icon-button mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open menu"><Menu size={21} /></button><div className="breadcrumb">Workspace <ChevronRight size={14} /> <strong>{navItems.find((item) => item.id === view)?.label}</strong></div><div className="topbar-right"><span className="campaign-chip"><span className="chip-dot" /> {config?.category || 'Technology'} campaign</span><button className="icon-button refresh-button" onClick={refresh} aria-label="Refresh data" title="Refresh data"><RefreshCw size={17} /></button></div></header>
      {isDemo && <div className="demo-banner"><ShieldCheck size={16} /> Read-only demo of a real run (frozen snapshot, no backend). To review, approve and send, run it locally: see the README.</div>}
      {error && <div className="connection-error"><CircleAlert size={17} /> Could not connect to the local API: {error}. Start the Python server, then refresh.</div>}
      {view === 'research' && <div className="page-content">
        <div className="page-heading"><div><span className="eyebrow">CREATOR DISCOVERY</span><h1>Research workspace</h1><p>Find the right voices, inspect the evidence, and decide who moves forward.</p></div><button className="button primary heading-action" onClick={() => navigate('run')}><Play size={16} fill="currentColor" /> Run pipeline</button></div>
        <div className="stat-grid"><StatCard icon={Users} label="Discovered" value={summary.discovered} note={`Target: ${config?.minimum_discovered || 50} real profiles`} /><StatCard icon={BadgeCheck} label="Qualified" value={summary.passed} note="Passed objective + fit checks" tone="green" /><StatCard icon={Mail} label="Contactable" value={summary.contactable} note="Sourced public email found" /><StatCard icon={FileText} label="Drafts ready" value={summary.drafts} note="Pending human review" /></div>
        <div className="content-card creator-card"><div className="card-header"><div><h2>Creator directory</h2><p>{creators.length ? `${number(creators.length)} profiles in this workspace` : 'Your discovery results will appear here'}</p></div><span className="tiny-tag"><Globe2 size={14} /> YouTube · Technology</span></div>
          <div className="toolbar"><div className="search-box"><Search size={18} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search creators or themes" aria-label="Search creators" /></div><div className="filter-select"><Filter size={16} /><select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter by status"><option value="ALL">All statuses</option><option value="PASSED">Qualified</option><option value="REVIEW_REQUIRED">Needs review</option><option value="FAILED">Not qualified</option><option value="DISCOVERED">Discovered</option></select><ChevronDown size={14} /></div></div>
          {loading ? <div className="loading-state">Loading workspace…</div> : filtered.length ? <div className="table-wrap"><table><thead><tr><th>CREATOR</th><th>SUBSCRIBERS</th><th>ENGAGEMENT</th><th>FIT</th><th>STATUS</th><th>CONTACT</th><th aria-label="Details" /></tr></thead><tbody>{filtered.map((creator) => <tr key={creator.channel_id} onClick={() => setSelectedId(creator.channel_id)} onKeyDown={(event) => { if (event.key === 'Enter') setSelectedId(creator.channel_id) }} tabIndex="0" aria-label={`View ${creator.name}`}><td><div className="creator-cell"><div className="avatar">{initials(creator.name)}</div><div><strong>{creator.name}</strong><span>{creator.themes?.slice(0, 2).join(' · ') || 'Technology'}</span></div></div></td><td>{number(creator.subscribers)}</td><td>{percent(creator.engagement_rate)}</td><td><span className="fit-score">{creator.fit_score == null ? '—' : `${creator.fit_score}/100`}</span></td><td><StatusPill status={creator.filter_status} /></td><td>{creator.email && creator.email !== 'Not Found' ? <span className="contact-yes"><CheckCircle2 size={15} /> Found</span> : <span className="muted">Not Found</span>}</td><td><ChevronRight size={17} className="row-arrow" /></td></tr>)}</tbody></table></div> : <EmptyState query={search || status !== 'ALL'} onRun={() => navigate('run')} />}
          <div className="card-footer"><span>Evidence is collected from public sources; missing data stays unavailable.</span><span>{filtered.length} shown</span></div>
        </div>
      </div>}
      {view === 'outreach' && <div className="page-content"><div className="page-heading"><div><span className="eyebrow">DELIVERY & AUDIT TRAIL</span><h1>Outreach tracker</h1><p>Every simulated send, manual DM, and blocked duplicate—recorded in one place.</p></div><button className="button secondary heading-action" onClick={exportFiles} disabled={busy}><ArrowDownToLine size={16} /> Export CSVs</button></div>
        <div className="stat-grid three"><StatCard icon={FileText} label="Generated drafts" value={summary.drafts} note="Email + Instagram DM" /><StatCard icon={CheckCircle2} label="Approved" value={summary.approved} note="Reviewed by a person" tone="green" /><StatCard icon={Send} label="Simulated sends" value={summary.simulated_sent} note="No live message transmitted" /></div>
        <div className="content-card"><div className="card-header"><div><h2>Activity log</h2><p>Newest events first, including skip and duplicate outcomes</p></div><span className="tiny-tag"><Clock3 size={14} /> Audit ready</span></div>{events.length ? <div className="table-wrap"><table><thead><tr><th>CREATOR</th><th>CHANNEL</th><th>STATUS</th><th>RECIPIENT</th><th>DATE</th></tr></thead><tbody>{events.map((event) => <tr key={event.id}><td><button className="table-link" onClick={() => { setView('research'); setSelectedId(event.channel_id) }}>{event.name}</button></td><td>{event.channel === 'email' ? 'Email · dry run' : 'Instagram · manual'}</td><td><span className={`event-status event-${event.status.toLowerCase()}`}>{eventLabels[event.status] || event.status}</span></td><td className="recipient-cell">{event.recipient || '—'}</td><td>{date(event.created_at)}</td></tr>)}</tbody></table></div> : <div className="empty-state compact"><div className="empty-icon"><Mail size={26} /></div><h3>No outreach activity yet</h3><p>Approve a personalized draft, then simulate an email send or record a manual Instagram DM. No live messages are sent.</p><button className="button secondary" onClick={() => navigate('research')}>Review creators <ArrowRight size={15} /></button></div>}</div>
      </div>}
      {view === 'run' && <div className="page-content"><div className="page-heading"><div><span className="eyebrow">AUTOMATION CONTROL</span><h1>Run & exports</h1><p>Move from public discovery to reviewable drafts, with each stage visible.</p></div></div>
        <div className="run-grid"><div className="content-card run-card"><div className="card-header"><div><h2>Pipeline</h2><p>Technology niche · YouTube public data · local storage</p></div><span className={`run-badge run-${run.status}`}>{run.status === 'running' ? 'Running' : run.status === 'complete' ? 'Complete' : run.status === 'partial' ? 'Partial results' : run.status === 'failed' ? 'Needs attention' : 'Not started'}</span></div><div className="pipeline-list">{[
          ['Discovery', 'Find and deduplicate creator channels', Search], ['Assessment', 'Metrics, classification & brand fit', Filter], ['Enrichment', 'Source contact details from public pages', Globe2], ['Personalization', 'Generate grounded email and DM drafts', Sparkles], ['Export', 'Prepare submission-ready CSVs', ArrowDownToLine],
        ].map(([label, description, Icon], i) => { const keys = ['Discovery', 'Assessment', 'Enrichment', 'Personalization', 'Export']; const currentIndex = keys.indexOf(run.stage); const done = run.status === 'complete' || Boolean(run.results?.[label]); const current = run.status === 'running' && currentIndex === i; return <div className={`pipeline-step ${done ? 'done' : ''} ${current ? 'current' : ''}`} key={label}><div className="step-marker">{done ? <Check size={16} /> : <Icon size={17} />}</div><div><strong>{label}</strong><span>{description}</span></div><small>{done ? 'Done' : current ? 'In progress' : 'Pending'}</small></div> })}</div>
          {run.detail && <div className={`run-detail ${['failed', 'partial'].includes(run.status) ? 'failure' : ''}`}>{['failed', 'partial'].includes(run.status) ? <CircleAlert size={16} /> : <Activity size={16} />}{run.detail}</div>}
          <div className="run-actions"><button className="button primary" onClick={startRun} disabled={busy || run.status === 'running'}><Play size={15} fill="currentColor" /> {run.status === 'complete' ? 'Run again' : 'Start pipeline'}</button><span>Safe to rerun; records are persisted between stages.</span></div>
        </div><div className="run-side"><div className="content-card setup-card"><div className="card-header"><div><h2>Connection checklist</h2><p>Presence only; provider access is tested during a run. Keys never appear here.</p></div><KeyRound size={19} /></div><div className="key-list">{[['YOUTUBE_API_KEY', 'YouTube Data API'], ['GROQ_API_KEY', 'Groq classification'], ['GEMINI_API_KEY', 'Gemini personalization']].map(([key, label]) => <div key={key}><span className={credentials?.[key] ? 'key-ok' : 'key-missing'}>{credentials?.[key] ? <Check size={14} /> : <X size={14} />}</span><span><strong>{label}</strong><small>{credentials?.[key] ? 'Configured' : 'Add to project .env'}</small></span></div>)}</div><p className="setup-note">Classifier mode: <code>{config?.classifier_provider || 'auto'}</code>. Groq is optional when Gemini classification is selected. Keep the local <code>.env</code> out of submissions.</p></div>
          <div className="content-card export-card"><div className="card-header"><div><h2>Submission exports</h2><p>Real records only; never fabricated.</p></div></div><div className="export-list">{[['influencers.csv', 'Profiles & filter decisions'], ['messages.csv', 'Generated outreach drafts'], ['outreach_log.csv', 'Delivery outcomes']].map(([file, label]) => <a key={file} href={isDemo ? `/demo/exports/${file}` : `/api/exports/${file}`} download><FileText size={17} /><span><strong>{file}</strong><small>{label}</small></span><ArrowDownToLine size={16} /></a>)}</div><button className="button secondary export-button" onClick={exportFiles} disabled={busy}>Generate / refresh CSVs</button></div></div></div>
      </div>}
    </main>
    {selectedId && <div className="panel-backdrop" onClick={() => setSelectedId(null)}><div onClick={(event) => event.stopPropagation()}>{detail ? <CreatorDetail data={detail} onClose={() => setSelectedId(null)} onMutation={mutate} busy={busy} notify={notify} /> : <div className="detail-panel loading-panel">Loading creator…</div>}</div></div>}
    {toast && <div className={`toast ${toast.type}`} role="status">{toast.type === 'error' ? <CircleAlert size={17} /> : <CheckCircle2 size={17} />}{toast.message}<button onClick={() => setToast(null)} aria-label="Dismiss notification"><X size={15} /></button></div>}
  </div>
}

export default App
