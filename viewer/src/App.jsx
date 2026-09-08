import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

const LS_KEY = 'rera_viewed'

function loadViewed() {
  try { return new Set(JSON.parse(localStorage.getItem(LS_KEY) || '[]')) }
  catch { return new Set() }
}

function saveViewed(set) {
  localStorage.setItem(LS_KEY, JSON.stringify([...set]))
}

function getDocIdxFromUrl() {
  const p = new URLSearchParams(window.location.search).get('doc')
  return p !== null ? parseInt(p, 10) : null
}

// ─── Main app ────────────────────────────────────────────────────────────────

export default function App() {
  const [docs, setDocs]               = useState([])
  const [query, setQuery]             = useState('')
  const [selected, setSelected]       = useState(null)
  const [mdContent, setMdContent]     = useState('')
  const [mdLoading, setMdLoading]     = useState(false)
  const [docsLoading, setDocsLoading] = useState(true)
  const [viewed, setViewed]           = useState(loadViewed)
  // list filters
  const [viewedFilter, setViewedFilter]       = useState('all')
  const [extractedFilter, setExtractedFilter] = useState('all')
  const [toolFilters, setToolFilters]         = useState(new Set())
  const searchRef  = useRef(null)
  const mdCacheRef = useRef({})

  // Page routing state
  const [page, setPage]           = useState(() => new URLSearchParams(window.location.search).get('page'))
  const [runId, setRunId]         = useState(() => new URLSearchParams(window.location.search).get('run'))
  const [runDocPath, setRunDocPath] = useState(() => new URLSearchParams(window.location.search).get('path'))

  function toggleToolFilter(tool) {
    setToolFilters(prev => {
      const next = new Set(prev)
      next.has(tool) ? next.delete(tool) : next.add(tool)
      return next
    })
  }

  // ── Navigation ──────────────────────────────────────────────────────────────

  const navigate = useCallback((newPage, params = {}) => {
    const url = new URL(window.location)
    url.search = ''
    if (newPage) url.searchParams.set('page', newPage)
    Object.entries(params).forEach(([k, v]) => {
      if (v != null) url.searchParams.set(k, String(v))
    })
    history.pushState({}, '', url)
    setPage(newPage || null)
    setRunId(params.run || null)
    setRunDocPath(params.path || null)
    if (!newPage) {
      setSelected(null)
      setMdContent('')
    }
  }, [])

  const openDoc = useCallback((doc, pushHistory = true) => {
    if (pushHistory) {
      const url = new URL(window.location)
      url.searchParams.set('doc', doc.idx)
      history.pushState({ docIdx: doc.idx }, '', url)
    }
    setViewed(prev => {
      const next = new Set(prev)
      next.add(doc.rel_doc)
      saveViewed(next)
      return next
    })
    setSelected(doc)
    if (!doc.has_md) {
      setMdContent('')
      setMdLoading(false)
      mdCacheRef.current[doc.idx] = ''
      return
    }
    const cached = mdCacheRef.current[doc.idx]
    if (cached !== undefined) {
      setMdContent(cached)
      setMdLoading(false)
    } else {
      setMdContent('')
      setMdLoading(true)
      fetch(`/api/doc/${doc.idx}/md`)
        .then(r => r.text())
        .then(text => {
          mdCacheRef.current[doc.idx] = text
          setMdContent(text)
          setMdLoading(false)
        })
        .catch(() => setMdLoading(false))
    }
  }, [])

  const goBack = useCallback(() => {
    const url = new URL(window.location)
    url.searchParams.delete('doc')
    history.pushState({}, '', url)
    setSelected(null)
    setMdContent('')
  }, [])

  // ── Data loading ────────────────────────────────────────────────────────────

  useEffect(() => {
    fetch('/api/docs')
      .then(r => r.json())
      .then(data => {
        setDocs(data)
        setDocsLoading(false)
        const idx = getDocIdxFromUrl()
        if (idx !== null && data[idx]) openDoc(data[idx], false)
      })
      .catch(() => setDocsLoading(false))
  }, [openDoc])

  // Unified popstate handler — covers both doc navigation and page routing
  useEffect(() => {
    const handlePop = () => {
      const params  = new URLSearchParams(window.location.search)
      const newPage = params.get('page')
      const newRun  = params.get('run')
      const newPath = params.get('path')
      setPage(newPage)
      setRunId(newRun)
      setRunDocPath(newPath)
      if (!newPage) {
        const idx = params.get('doc')
        if (idx === null || idx === '') { setSelected(null); setMdContent('') }
        else if (docs[parseInt(idx)]) openDoc(docs[parseInt(idx)], false)
      }
    }
    window.addEventListener('popstate', handlePop)
    return () => window.removeEventListener('popstate', handlePop)
  }, [docs, openDoc])

  useEffect(() => {
    if (!selected) setTimeout(() => searchRef.current?.focus(), 50)
  }, [selected])

  // Preload n±1 neighbours
  useEffect(() => {
    if (!selected || filtered.length === 0) return
    const pos = filtered.findIndex(d => d.idx === selected.idx)
    const neighbours = [filtered[pos - 1], filtered[pos + 1]].filter(Boolean)
    neighbours.forEach(doc => {
      if (mdCacheRef.current[doc.idx] !== undefined || !doc.has_md) return
      const t = setTimeout(() => {
        fetch(`/api/doc/${doc.idx}/md`)
          .then(r => r.text())
          .then(text => { mdCacheRef.current[doc.idx] = text })
          .catch(() => {})
      }, 400)
      return () => clearTimeout(t)
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.idx])

  const filtered = docs.filter(d => {
    if (query.trim()) {
      const q = query.toLowerCase()
      if (!(
        d.rera_id.toLowerCase().includes(q) ||
        d.state.toLowerCase().includes(q) ||
        d.period.toLowerCase().includes(q) ||
        d.filename.toLowerCase().includes(q)
      )) return false
    }
    const isViewed = viewed.has(d.rel_doc)
    if (viewedFilter === 'viewed'   && !isViewed) return false
    if (viewedFilter === 'unviewed' &&  isViewed) return false
    if (extractedFilter === 'extracted'     &&  !d.has_md) return false
    if (extractedFilter === 'not-extracted' &&   d.has_md) return false
    if (toolFilters.has('docling')  && !d.has_docling) return false
    if (toolFilters.has('marker')   && !d.has_marker)  return false
    return true
  })

  const filtersActive = viewedFilter !== 'all' || extractedFilter !== 'all' || toolFilters.size > 0

  const selectedPos = selected ? filtered.findIndex(d => d.idx === selected.idx) : -1
  const canGoPrev   = selectedPos > 0
  const canGoNext   = selectedPos < filtered.length - 1

  function navigatePrev() { if (canGoPrev) openDoc(filtered[selectedPos - 1]) }
  function navigateNext() { if (canGoNext) openDoc(filtered[selectedPos + 1]) }

  useEffect(() => {
    if (!selected) return
    function onKey(e) {
      const tag = document.activeElement?.tagName
      if (tag === 'IFRAME' || tag === 'INPUT' || tag === 'TEXTAREA') return
      if (e.key === 'ArrowLeft')  { e.preventDefault(); navigatePrev() }
      if (e.key === 'ArrowRight') { e.preventDefault(); navigateNext() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, selectedPos, filtered])

  // ── Page routing ─────────────────────────────────────────────────────────────

  if (page === 'runs' && runId && runDocPath) {
    return (
      <RunDocView
        runId={runId}
        docPath={decodeURIComponent(runDocPath)}
        docs={docs}
        onBack={() => navigate('runs', { run: runId })}
      />
    )
  }
  if (page === 'runs' && runId) {
    return (
      <RunDetailPage
        runId={runId}
        docs={docs}
        onBack={() => navigate('runs')}
        onDocClick={(path) => navigate('runs', { run: runId, path: encodeURIComponent(path) })}
        onNavigate={navigate}
      />
    )
  }
  if (page === 'runs') {
    return (
      <RunsPage
        docs={docs}
        onRunClick={(id) => navigate('runs', { run: id })}
        onNavigate={navigate}
      />
    )
  }

  // ── Default: docs page ───────────────────────────────────────────────────────

  if (selected) {
    return (
      <SplitView
        key={selected.idx}
        doc={selected}
        mdContent={mdContent}
        mdLoading={mdLoading}
        onBack={goBack}
        onPrev={canGoPrev ? navigatePrev : null}
        onNext={canGoNext ? navigateNext : null}
        position={selectedPos + 1}
        total={filtered.length}
      />
    )
  }

  return (
    <div className="search-screen">
      <NavBar active="docs" onNavigate={navigate} />

      <div className="search-top">
        <h1 className="search-title">RERA Document Viewer</h1>
        <p className="search-subtitle">
          {docsLoading
            ? 'Loading…'
            : `${docs.length.toLocaleString()} documents · ${viewed.size} viewed`}
        </p>
        <div className="search-field-wrap">
          <svg className="search-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" strokeWidth="1.6"/>
            <path d="M13.5 13.5L17 17" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/>
          </svg>
          <input
            ref={searchRef}
            className="search-input"
            type="text"
            placeholder="Search by RERA ID, state, or period…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            autoFocus
            spellCheck={false}
          />
          {query && (
            <button className="search-clear" onClick={() => setQuery('')} aria-label="Clear">×</button>
          )}
        </div>
      </div>

      <div className="filter-bar">
        <div className="filter-groups">
          <div className="filter-group">
            <span className="filter-label">Viewed</span>
            <div className="seg-ctrl">
              {[['all', 'All'], ['viewed', 'Viewed'], ['unviewed', 'Unviewed']].map(([val, label]) => (
                <button
                  key={val}
                  className={`seg-btn${viewedFilter === val ? ' seg-btn--active' : ''}`}
                  onClick={() => setViewedFilter(val)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="filter-group">
            <span className="filter-label">Extracted</span>
            <div className="seg-ctrl">
              {[['all', 'All'], ['extracted', 'Yes'], ['not-extracted', 'No']].map(([val, label]) => (
                <button
                  key={val}
                  className={`seg-btn${extractedFilter === val ? ' seg-btn--active' : ''}`}
                  onClick={() => setExtractedFilter(val)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="filter-group">
            <span className="filter-label">Extractions</span>
            <div className="filter-tool-btns">
              {['docling', 'marker'].map(tool => (
                <button
                  key={tool}
                  className={`filter-tool-btn${toolFilters.has(tool) ? ' filter-tool-btn--active' : ''}`}
                  onClick={() => toggleToolFilter(tool)}
                >
                  {tool.charAt(0).toUpperCase() + tool.slice(1)}
                </button>
              ))}
            </div>
          </div>

          {filtersActive && (
            <button
              className="filter-clear"
              onClick={() => { setViewedFilter('all'); setExtractedFilter('all'); setToolFilters(new Set()) }}
            >
              Clear filters
            </button>
          )}
        </div>

        <span className="results-count">
          {filtered.length.toLocaleString()}{(query.trim() || filtersActive) ? ` of ${docs.length.toLocaleString()}` : ''} doc{filtered.length !== 1 ? 's' : ''}
        </span>
      </div>

      <div className="results-list">
        {!docsLoading && filtered.length === 0 && (
          <div className="empty-state">
            {query.trim()
              ? `No documents match "${query}"`
              : 'No documents match the active filters.'}
          </div>
        )}
        {filtered.map(doc => {
          const isViewed = viewed.has(doc.rel_doc)
          return (
            <button
              key={doc.idx}
              className={`doc-row${isViewed ? ' doc-row--viewed' : ''}`}
              onClick={() => openDoc(doc)}
            >
              <span className="doc-rera">{doc.rera_id}</span>
              <span className="doc-filename">{doc.filename}</span>
              <span className="doc-badges">
                {isViewed && <span className="badge badge--viewed">Viewed</span>}
                <span className="badge badge--state">{doc.state}</span>
                <span className="badge badge--period">{doc.period}</span>
                {!doc.has_md     && <span className="badge badge--warn">not extracted</span>}
                {doc.has_docling && <span className="badge badge--tool">Docling</span>}
                {doc.has_marker  && <span className="badge badge--tool">Marker</span>}
                {!doc.has_doc   && <span className="badge badge--warn">no file</span>}
              </span>
              <svg className="doc-arrow" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ─── Nav bar ──────────────────────────────────────────────────────────────────

function NavBar({ active, onNavigate }) {
  return (
    <nav className="nav-bar">
      <button
        className={`nav-tab${active === 'docs' ? ' nav-tab--active' : ''}`}
        onClick={() => onNavigate(null)}
      >
        Documents
      </button>
      <button
        className={`nav-tab${active === 'runs' ? ' nav-tab--active' : ''}`}
        onClick={() => onNavigate('runs')}
      >
        Benchmark Runs
      </button>
    </nav>
  )
}

// ─── Runs list page ───────────────────────────────────────────────────────────

function RunsPage({ docs, onRunClick, onNavigate }) {
  const [runs, setRuns]           = useState([])
  const [loading, setLoading]     = useState(true)
  const [showNewRun, setShowNewRun] = useState(false)

  const loadRuns = useCallback(() => {
    fetch('/api/runs')
      .then(r => r.json())
      .then(data => { setRuns(data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  useEffect(() => { loadRuns() }, [loadRuns])

  return (
    <div className="search-screen">
      <NavBar active="runs" onNavigate={onNavigate} />

      <div className="runs-header">
        <div className="runs-header-top">
          <div>
            <h2 className="runs-title">Benchmark Runs</h2>
            <p className="runs-subtitle">
              {loading ? 'Loading…' : `${runs.length} run${runs.length !== 1 ? 's' : ''}`}
            </p>
          </div>
          <div className="runs-actions">
            <button className="action-btn" onClick={loadRuns}>Refresh</button>
            <button className="action-btn action-btn--primary" onClick={() => setShowNewRun(true)}>
              New Run
            </button>
          </div>
        </div>
      </div>

      <div className="runs-list">
        {!loading && runs.length === 0 && (
          <div className="empty-state">No benchmark runs yet. Create one with New Run above.</div>
        )}
        {runs.map(run => (
          <button key={run.run_id} className="run-row" onClick={() => onRunClick(run.run_id)}>
            <div className="run-row-main">
              <span className="run-id">{run.run_id}</span>
              <div className="run-row-badges">
                <span className="badge badge--tool">Var {run.variant}</span>
                <span className="badge badge--state">{run.state}</span>
                <span className="badge badge--version">{run.prompt_version || 'v0'}</span>
                <RunStatusBadge status={run.status} />
              </div>
              <svg className="run-arrow" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </div>
            <div className="run-row-meta">
              <span>{run.mode} mode</span>
              {run.created_at && <><span>·</span><span>{run.created_at}</span></>}
              {run.n_chunks > 0 && (
                <><span>·</span><span>{run.n_chunks_complete}/{run.n_chunks} chunks done</span></>
              )}
            </div>
          </button>
        ))}
      </div>

      {showNewRun && (
        <NewRunModal
          onClose={() => setShowNewRun(false)}
          onCreated={(id) => { setShowNewRun(false); onRunClick(id) }}
        />
      )}
    </div>
  )
}

// ─── Retry dialog (inline popover) ───────────────────────────────────────────

const _HARYANA_STATES = new Set(['hr', 'haryana'])

function RetryDialog({ scope, state, initial, onConfirm, onCancel }) {
  const [selected, setSelected] = useState(initial)
  const [versions, setVersions] = useState([])
  const label = scope === 'failed' ? 'Retry Failed' : 'Retry All'
  const isHaryana = _HARYANA_STATES.has((state || '').toLowerCase())
  const showGjNote = isHaryana && selected === 'v0'

  useEffect(() => {
    fetch('/api/prompt-versions')
      .then(r => r.json())
      .then(data => {
        const vs = data.versions || []
        setVersions(vs)
        // if current selection isn't in the list, pick the current default
        if (vs.length > 0 && !vs.find(v => v.id === selected)) {
          setSelected(data.current || vs[vs.length - 1].id)
        }
      })
      .catch(() => {
        // fallback: derive from initial so at minimum the run's own version appears
        const known = ['v0', 'v1']
        const ids = known.includes(initial)
          ? known
          : [...known, initial].sort()
        setVersions(ids.map(id => ({ id, description: '' })))
      })
  }, [])

  return (
    <div className="retry-popover">
      <div className="retry-popover-label">Prompt version</div>
      {versions.length === 0
        ? <span className="retry-popover-loading">Loading…</span>
        : <div className="seg-ctrl">
            {versions.map(v => (
              <button key={v.id} type="button"
                className={`seg-btn${selected === v.id ? ' seg-btn--active' : ''}`}
                onClick={() => setSelected(v.id)}
                title={v.description}
              >{v.id}</button>
            ))}
          </div>
      }
      {showGjNote && (
        <p className="retry-popover-note">Uses the original Gujarat v0 prompt (no Haryana-specific rules)</p>
      )}
      <div className="retry-popover-actions">
        <button className="action-btn" onClick={onCancel}>Cancel</button>
        <button className="action-btn action-btn--primary"
          onClick={() => onConfirm(selected)}
          disabled={versions.length === 0}
        >{label}</button>
      </div>
    </div>
  )
}

// ─── Run detail page ──────────────────────────────────────────────────────────

function RunDetailPage({ runId, docs, onBack, onDocClick, onNavigate }) {
  const [run, setRun]         = useState(null)
  const [loading, setLoading] = useState(true)
  const [fetching, setFetching]   = useState(false)
  const [stopping, setStopping]   = useState(false)
  const [retrying, setRetrying]   = useState(null) // 'all' | 'failed' | null
  // retryDialog: { scope: 'all'|'failed', promptVersion: string } | null
  const [retryDialog, setRetryDialog] = useState(null)

  const pathToIdx = useMemo(() => {
    const map = {}
    docs.forEach(d => { map[d.rel_doc] = d.idx })
    return map
  }, [docs])

  const loadRun = useCallback(() => {
    fetch(`/api/runs/${runId}`)
      .then(r => r.json())
      .then(data => { setRun(data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [runId])

  useEffect(() => { loadRun() }, [loadRun])

  // Auto-refresh while run is not complete
  useEffect(() => {
    if (!run || run.status === 'complete') return
    const interval = setInterval(loadRun, 5000)
    return () => clearInterval(interval)
  }, [run?.status, loadRun])

  function triggerStop() {
    setStopping(true)
    fetch(`/api/runs/${runId}/stop`, { method: 'POST' })
      .then(() => setTimeout(loadRun, 1000))
      .catch(() => {})
      .finally(() => setStopping(false))
  }

  function triggerFetch() {
    setFetching(true)
    fetch(`/api/runs/${runId}/fetch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ poll_tries: 4, poll_interval: 10 }),
    })
      .then(() => setTimeout(loadRun, 3000))
      .catch(() => {})
      .finally(() => setFetching(false))
  }

  function openRetryDialog(scope) {
    setRetryDialog({ scope, promptVersion: run?.prompt_version || 'v0' })
  }

  function triggerRetry(scope, promptVersion) {
    setRetryDialog(null)
    setRetrying(scope)
    fetch(`/api/runs/${runId}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, prompt_version: promptVersion }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.run_id) onNavigate('runs', { run: data.run_id })
      })
      .catch(() => {})
      .finally(() => setRetrying(null))
  }

  const showFetchBtn = run?.mode === 'batch' && run?.status !== 'complete'
  const runDocs      = run?.docs || []
  const nFailed      = runDocs.filter(d => d.status === 'error').length
  const nPending     = runDocs.filter(d => d.status === 'running' || !d.status).length
  const showRetryFailed = run?.status === 'complete'
    ? nFailed > 0
    : (nFailed + nPending) > 0
  const stats = run?.stats || {}

  return (
    <div className="search-screen">
      <NavBar active="runs" onNavigate={onNavigate} />

      <div className="runs-header">
        <div className="runs-header-top">
          <button className="back-btn-inline" onClick={onBack}>← Back</button>
          <div className="runs-actions">
            {showFetchBtn && (
              <button
                className="action-btn action-btn--primary"
                onClick={triggerFetch}
                disabled={fetching}
              >
                {fetching ? 'Fetching…' : 'Fetch Status'}
              </button>
            )}
            {showRetryFailed && (
              <div className="retry-wrap">
                <button
                  className="action-btn action-btn--warn"
                  onClick={() => openRetryDialog('failed')}
                  disabled={!!retrying}
                  title="Create a new run with only failed/pending docs"
                >
                  {retrying === 'failed' ? 'Starting…' : `Retry Failed${nFailed > 0 ? ` (${nFailed})` : ''}`}
                </button>
                {retryDialog?.scope === 'failed' && (
                  <RetryDialog
                    scope="failed"
                    state={run?.state}
                    initial={retryDialog.promptVersion}
                    onConfirm={v => triggerRetry('failed', v)}
                    onCancel={() => setRetryDialog(null)}
                  />
                )}
              </div>
            )}
            {run && (
              <div className="retry-wrap">
                <button
                  className="action-btn"
                  onClick={() => openRetryDialog('all')}
                  disabled={!!retrying}
                  title="Create a new run with the same documents"
                >
                  {retrying === 'all' ? 'Starting…' : 'Retry All'}
                </button>
                {retryDialog?.scope === 'all' && (
                  <RetryDialog
                    scope="all"
                    state={run?.state}
                    initial={retryDialog.promptVersion}
                    onConfirm={v => triggerRetry('all', v)}
                    onCancel={() => setRetryDialog(null)}
                  />
                )}
              </div>
            )}
            {run?.status === 'running' && (
              <button
                className="action-btn action-btn--stop"
                onClick={triggerStop}
                disabled={stopping}
                title="Terminate the running process"
              >
                {stopping ? 'Stopping…' : 'Stop'}
              </button>
            )}
            <button className="action-btn" onClick={loadRun}>Refresh</button>
          </div>
        </div>

        {loading
          ? <p className="runs-subtitle">Loading…</p>
          : run
            ? <>
                <h2 className="run-detail-id">{run.run_id}</h2>
                <div className="run-detail-meta">
                  <span className="badge badge--tool">Variant {run.variant}</span>
                  <span className="badge badge--state">{run.state}</span>
                  <span className="badge badge--version">{run.prompt_version || 'v0'}</span>
                  <RunStatusBadge status={run.status} />
                  <span className="run-meta-text">{run.mode} mode · {run.created_at}</span>
                </div>

                <div className="run-stats-bar">
                  <StatCell label="Total"   value={stats.total ?? '—'} />
                  <StatCell label="Success"  value={stats.success ?? '—'} variant="success" />
                  <StatCell label="Error"    value={stats.error   ?? '—'} variant="error" />
                  <StatCell label="Skipped"  value={stats.skipped ?? '—'} variant="warn" />
                  {(stats.pending ?? 0) > 0 && (
                    <StatCell label="Pending" value={stats.pending} variant="pending" />
                  )}
                  {stats.cost_total != null && (
                    <StatCell label="Cost" value={`$${stats.cost_total.toFixed(3)}`} />
                  )}
                  {stats.avg_latency_s != null && (
                    <StatCell label="Avg latency" value={`${stats.avg_latency_s}s`} />
                  )}
                </div>
              </>
            : <p className="runs-subtitle">Run not found.</p>
        }
      </div>

      <div className="runs-list">
        {/* Log view — always show if available, labelled by context */}
        {run?.log && (
          <div className="log-block">
            <div className="log-block-label">
              {run.status === 'running'
                ? 'Live log (auto-refreshing…)'
                : stats.success === 0 && (stats.skipped ?? 0) > 0
                  ? 'Process log — all docs skipped (see errors below)'
                  : 'Process log'}
            </div>
            <pre className="log-content">{run.log}</pre>
          </div>
        )}

        {/* Doc list */}
        {(run?.docs || []).length > 0 && (run?.docs || []).map(doc => {
          const docIdx  = pathToIdx[doc.local_path]
          const canView = doc.status !== 'pending' && doc.has_output && docIdx !== undefined
          return (
            <button
              key={doc.local_path}
              className={`run-doc-row${canView ? '' : ' run-doc-row--disabled'}`}
              onClick={() => canView && onDocClick(doc.local_path)}
              disabled={!canView}
              title={doc.error || undefined}
            >
              <DocStatusDot status={doc.status} />
              <span className="run-doc-path">{doc.local_path}</span>
              {doc.cost_usd != null && (
                <span className="run-doc-meta">${doc.cost_usd.toFixed(4)}</span>
              )}
              {doc.latency_s != null && (
                <span className="run-doc-meta">{doc.latency_s.toFixed(1)}s</span>
              )}
              {doc.error && (
                <span className="run-doc-error" title={doc.error}>{doc.error.slice(0, 60)}</span>
              )}
              {canView && (
                <svg className="run-arrow" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              )}
            </button>
          )
        })}

        {!loading && run && (run.docs || []).length === 0 && !run.log && (
          <div className="empty-state">
            {run.status === 'running'
              ? 'Run in progress — no results yet.'
              : 'No document results available.'}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── JSON tree viewer ─────────────────────────────────────────────────────────

function JsonPrimitive({ value }) {
  if (value === null)             return <span className="jv-null">null</span>
  if (value === undefined)        return <span className="jv-null">undefined</span>
  if (typeof value === 'boolean') return <span className="jv-bool">{String(value)}</span>
  if (typeof value === 'number')  return <span className="jv-num">{String(value)}</span>
  if (typeof value === 'string')  return <span className="jv-str">{'"'}{value}{'"'}</span>
  return null
}

function JsonNode({ k, value, depth, last }) {
  const [open, setOpen] = useState(depth < 2)

  const isObj = value !== null && typeof value === 'object'
  const isArr = isObj && Array.isArray(value)
  const keyPart = k !== undefined
    ? <><span className="jv-key">{'"'}{k}{'"'}</span><span className="jv-punct">: </span></>
    : null

  if (!isObj) {
    return (
      <div className="jv-row">
        {keyPart}<JsonPrimitive value={value} />{!last && <span className="jv-punct">,</span>}
      </div>
    )
  }

  const entries = isArr ? value : Object.entries(value)
  const count   = entries.length
  const ob = isArr ? '[' : '{'
  const cb = isArr ? ']' : '}'

  if (count === 0) {
    return (
      <div className="jv-row">
        {keyPart}<span className="jv-bracket">{ob}{cb}</span>{!last && <span className="jv-punct">,</span>}
      </div>
    )
  }

  const hint = isArr
    ? `${count} item${count !== 1 ? 's' : ''}`
    : `${count} key${count !== 1 ? 's' : ''}`

  if (!open) {
    return (
      <div className="jv-row">
        <button className="jv-toggle" onClick={() => setOpen(true)} title="Expand">▸</button>
        {keyPart}
        <span className="jv-bracket">{ob}</span>
        <button className="jv-hint" onClick={() => setOpen(true)}>{hint}</button>
        <span className="jv-bracket">{cb}</span>
        {!last && <span className="jv-punct">,</span>}
      </div>
    )
  }

  return (
    <div className="jv-block">
      <div className="jv-row">
        <button className="jv-toggle" onClick={() => setOpen(false)} title="Collapse">▾</button>
        {keyPart}<span className="jv-bracket">{ob}</span>
      </div>
      <div className="jv-children">
        {(isArr ? value.map((v, i) => [i, v]) : entries).map(([ek, ev], i, arr) => (
          <JsonNode
            key={ek}
            k={isArr ? undefined : ek}
            value={ev}
            depth={depth + 1}
            last={i === arr.length - 1}
          />
        ))}
      </div>
      <div className="jv-row">
        <span className="jv-bracket">{cb}</span>
        {!last && <span className="jv-punct">,</span>}
      </div>
    </div>
  )
}

function JsonViewer({ data }) {
  return (
    <div className="jv-root">
      <JsonNode value={data} depth={0} last={true} />
    </div>
  )
}

// ─── Diff utilities ───────────────────────────────────────────────────────────

function computeLineDiff(linesA, linesB) {
  const m = linesA.length
  const n = linesB.length
  if (m * n > 600000) {
    return [
      ...linesA.map(t => ({ type: 'removed', text: t })),
      ...linesB.map(t => ({ type: 'added',   text: t })),
    ]
  }
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1))
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = linesA[i-1] === linesB[j-1]
        ? dp[i-1][j-1] + 1
        : Math.max(dp[i-1][j], dp[i][j-1])
  const result = []
  let i = m, j = n
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && linesA[i-1] === linesB[j-1]) {
      result.push({ type: 'same',    text: linesA[i-1] }); i--; j--
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      result.push({ type: 'added',   text: linesB[j-1] }); j--
    } else {
      result.push({ type: 'removed', text: linesA[i-1] }); i--
    }
  }
  return result.reverse()
}

function JsonDiff({ base, current }) {
  const diff = useMemo(() => {
    const lA = JSON.stringify(base,    null, 2).split('\n')
    const lB = JSON.stringify(current, null, 2).split('\n')
    return computeLineDiff(lA, lB)
  }, [base, current])

  const nAdded   = diff.filter(l => l.type === 'added').length
  const nRemoved = diff.filter(l => l.type === 'removed').length
  const hasDiff  = nAdded > 0 || nRemoved > 0

  return (
    <div className="diff-wrap">
      <div className="diff-legend">
        {hasDiff
          ? <>
              <span className="diff-legend-add">+{nAdded}</span>
              <span className="diff-legend-sep">·</span>
              <span className="diff-legend-rem">−{nRemoved}</span>
            </>
          : <span className="diff-legend-eq">Outputs are identical</span>
        }
      </div>
      {hasDiff && (
        <pre className="diff-pre">
          {diff.map((line, idx) => (
            <div key={idx} className={`diff-line diff-line--${line.type}`}>
              <span className="diff-gutter" aria-hidden="true">
                {line.type === 'added' ? '+' : line.type === 'removed' ? '−' : ' '}
              </span>
              <span className="diff-text">{line.text}</span>
            </div>
          ))}
        </pre>
      )}
    </div>
  )
}

// ─── Run doc split view (PDF + extracted text + structured JSON) ───────────────

function RunDocView({ runId, docPath, docs, onBack }) {
  const [result, setResult]       = useState(null)
  const [mdContent, setMdContent] = useState('')
  const [loadingResult, setLoadingResult] = useState(true)

  const [compareRuns, setCompareRuns]         = useState([])
  const [compareRunId, setCompareRunId]       = useState(null)
  const [compareResult, setCompareResult]     = useState(null)
  const [compareLoading, setCompareLoading]   = useState(false)
  const [showCompareMenu, setShowCompareMenu] = useState(false)
  const compareMenuRef = useRef(null)

  const doc = docs.find(d => d.rel_doc === docPath)

  useEffect(() => {
    fetch(`/api/runs/${runId}/results`)
      .then(r => r.json())
      .then(results => {
        const r = results.find(x => x.local_path === docPath)
        setResult(r || null)
        setLoadingResult(false)
      })
      .catch(() => setLoadingResult(false))
  }, [runId, docPath])

  useEffect(() => {
    if (!doc?.has_md) return
    fetch(`/api/doc/${doc.idx}/md`)
      .then(r => r.text())
      .then(setMdContent)
      .catch(() => {})
  }, [doc?.idx, doc?.has_md])

  useEffect(() => {
    try {
      const prev = new Set(JSON.parse(localStorage.getItem('rera_viewed_docs') || '[]'))
      prev.add(docPath)
      localStorage.setItem('rera_viewed_docs', JSON.stringify([...prev]))
    } catch (_) {}
  }, [docPath])

  // Fetch other runs that have a result for this document
  useEffect(() => {
    fetch(`/api/doc-runs?path=${encodeURIComponent(docPath)}`)
      .then(r => r.json())
      .then(data => setCompareRuns((data || []).filter(r => r.run_id !== runId)))
      .catch(() => {})
  }, [docPath, runId])

  // Fetch the selected comparison run's result
  useEffect(() => {
    if (!compareRunId) { setCompareResult(null); return }
    setCompareLoading(true)
    fetch(`/api/runs/${compareRunId}/result?path=${encodeURIComponent(docPath)}`)
      .then(r => r.json())
      .then(data => { setCompareResult(data); setCompareLoading(false) })
      .catch(() => { setCompareLoading(false) })
  }, [compareRunId, docPath])

  // Close compare menu on outside click
  useEffect(() => {
    if (!showCompareMenu) return
    function handleClick(e) {
      if (!compareMenuRef.current?.contains(e.target)) setShowCompareMenu(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [showCompareMenu])


  return (
    <div className="split-screen">
      <header className="split-header">
        <button className="back-btn" onClick={onBack}>
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true" width="14" height="14">
            <path d="M10 4L6 8l4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          Back
        </button>
        <div className="split-doc-info">
          <span className="split-rera">{doc?.rera_id || docPath.split('/').pop()}</span>
          <span className="split-sep">·</span>
          <span className="split-state">{runId}</span>
        </div>
        {(result || compareRuns.length > 0) && (
          <div className="split-controls">
            {result && (
              <>
                <RunStatusBadge status={result.success ? 'success' : 'error'} />
                {result.cost_usd != null && (
                  <span className="pane-meta">${result.cost_usd.toFixed(4)}</span>
                )}
                {result.latency_s != null && (
                  <span className="pane-meta">{result.latency_s.toFixed(1)}s</span>
                )}
              </>
            )}
            {compareRuns.length > 0 && (
              <div className="compare-wrap" ref={compareMenuRef}>
                <button
                  className={`ctrl-btn${compareRunId ? ' ctrl-btn--active' : ''}`}
                  onClick={() => setShowCompareMenu(v => !v)}
                  title="Compare extraction with another run"
                >
                  {compareRunId ? `vs ${compareRunId}` : 'Compare…'}
                </button>
                {showCompareMenu && (
                  <div className="compare-menu">
                    {compareRunId && (
                      <button
                        className="compare-menu-item compare-menu-item--clear"
                        onClick={() => {
                          setCompareRunId(null)
                          setCompareResult(null)
                          setShowCompareMenu(false)
                        }}
                      >
                        ✕ Clear comparison
                      </button>
                    )}
                    {compareRuns.map(r => (
                      <button
                        key={r.run_id}
                        className={`compare-menu-item${r.run_id === compareRunId ? ' compare-menu-item--active' : ''}`}
                        onClick={() => { setCompareRunId(r.run_id); setShowCompareMenu(false) }}
                      >
                        <span className="compare-run-id">{r.run_id}</span>
                        <span className="badge badge--version">{r.prompt_version || 'v0'}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </header>

      <div className="split-body">
        {/* Left: PDF */}
        <PaneWithDivider showDivider={false}>
          <PaneHeader label="PDF" />
          <div className="pane-content">
            {doc?.has_doc
              ? <iframe
                  src={`/api/doc/${doc.idx}/pdf#toolbar=0&navpanes=0`}
                  title="PDF"
                  className="pdf-frame"
                />
              : <div className="pane-empty">No PDF available.</div>
            }
          </div>
        </PaneWithDivider>

        {/* Middle: Extracted text */}
        <PaneWithDivider showDivider={true}>
          <PaneHeader label="Extracted Text" />
          <div className="pane-content pane-content--md">
            {mdContent
              ? <MdPane content={mdContent} isRaw={false} />
              : <div className="pane-empty">No extracted text (single-call variant or not available).</div>
            }
          </div>
        </PaneWithDivider>

        {/* Right: Structured JSON / Diff */}
        <PaneWithDivider showDivider={true}>
          <PaneHeader label={compareRunId ? 'Diff' : 'Structured JSON'} />
          <div className="pane-content pane-content--md">
            {compareRunId
              ? compareLoading
                ? <div className="pane-empty">Loading comparison…</div>
                : result?.output && compareResult?.output
                  ? <JsonDiff base={compareResult.output} current={result.output} />
                  : compareResult && !compareResult.output
                    ? <div className="pane-empty pane-empty--error">Comparison run produced no output for this document.</div>
                    : <div className="pane-empty">Loading…</div>
              : loadingResult
                ? <div className="pane-empty">Loading…</div>
                : !result
                  ? <div className="pane-empty">No result for this document.</div>
                  : result.error && !result.output
                    ? <div className="pane-empty pane-empty--error">Error: {result.error}</div>
                    : result.output
                      ? <JsonViewer data={result.output} />
                      : <div className="pane-empty">No output.</div>
            }
          </div>
        </PaneWithDivider>
      </div>
    </div>
  )
}

// ─── New run modal ─────────────────────────────────────────────────────────────

const VARIANTS = [
  { id: 'A', label: 'A — Gemini Flash, 2-call' },
  { id: 'B', label: 'B — Gemini Flash, single' },
  { id: 'C', label: 'C — Gemini Flash-Lite, 2-call' },
  { id: 'D', label: 'D — Gemini Flash-Lite, single' },
  { id: 'E', label: 'E — GLM-OCR → GPT Luna' },
  { id: 'F', label: 'F — Qwen3-VL → GPT Luna' },
]

function DocTag({ label, variant }) {
  return <span className={`dtag dtag--${variant}`}>{label}</span>
}

function DocPickerRow({ doc, checked, viewedSet, onChange }) {
  const isScanned  = doc.doc_type === 'scanned_pdf'
  const isText     = doc.doc_type === 'text_pdf'
  const isViewed   = viewedSet.has(doc.rel_doc)
  const shortName  = doc.rera_id && doc.rera_id.length < 50
    ? doc.rera_id
    : doc.filename.replace(/\.pdf$/i, '').slice(0, 60)

  return (
    <label className={`dp-row${checked ? ' dp-row--checked' : ''}`}>
      <input
        type="checkbox"
        className="dp-cb"
        checked={checked}
        onChange={e => onChange(doc.rel_doc, e.target.checked)}
      />
      <span className="dp-name" title={doc.filename}>{shortName}</span>
      <span className="dp-tags">
        {isScanned  && <DocTag label="scan"      variant="scan" />}
        {isText     && <DocTag label="text"      variant="text" />}
        {doc.has_md && <DocTag label="extracted" variant="ext" />}
        {doc.pages > 0 && <DocTag label={`${doc.pages} pp`} variant="pages" />}
        {doc.already_ran && <DocTag label="ran"   variant="ran" />}
        {isViewed        && <DocTag label="viewed" variant="viewed" />}
      </span>
    </label>
  )
}

function NewRunModal({ onClose, onCreated }) {
  const [form, setForm] = useState({
    variant: 'A', state: '', mode: 'async', prompt_version: '',
    limit: '', concurrency: '10', poll_tries: '8', poll_interval: '15',
  })
  const [states, setStates]                     = useState([])
  const [promptVersions, setPromptVersions]     = useState([])
  const [submitting, setSubmitting]             = useState(false)
  const [error, setError]                       = useState(null)
  const [allDocs, setAllDocs]                   = useState([])
  const [docsLoading, setDocsLoading]           = useState(false)
  const [selected, setSelected]                 = useState(new Set())
  const [search, setSearch]                     = useState('')
  const [tagFilters, setTagFilters]             = useState(new Set())
  const [pickerOpen, setPickerOpen]             = useState(false)
  const [viewedSet, setViewedSet]               = useState(new Set())

  useEffect(() => {
    fetch('/api/states')
      .then(r => r.json())
      .then(data => {
        setStates(data)
        if (data.length > 0) setForm(f => ({ ...f, state: f.state || data[0] }))
      })
      .catch(() => {})
    fetch('/api/prompt-versions')
      .then(r => r.json())
      .then(data => {
        setPromptVersions(data.versions || [])
        setForm(f => ({ ...f, prompt_version: f.prompt_version || data.current || 'v1' }))
      })
      .catch(() => {})
    try {
      const v = JSON.parse(localStorage.getItem('rera_viewed_docs') || '[]')
      setViewedSet(new Set(v))
    } catch (_) {}
  }, [])

  useEffect(() => {
    if (!form.state) return
    setDocsLoading(true)
    setSelected(new Set())
    setSearch('')
    setTagFilters(new Set())
    fetch(`/api/docs?state=${encodeURIComponent(form.state)}`)
      .then(r => r.json())
      .then(data => { setAllDocs(data); setDocsLoading(false) })
      .catch(() => { setAllDocs([]); setDocsLoading(false) })
  }, [form.state])

  const TAG_MATCH = {
    scan:      d => d.doc_type === 'scanned_pdf',
    text:      d => d.doc_type === 'text_pdf',
    extracted: d => d.has_md,
    ran:       d => d.already_ran,
    viewed:    d => viewedSet.has(d.rel_doc),
  }

  const filtered = allDocs.filter(d => {
    if (search.trim()) {
      const q = search.toLowerCase()
      if (!d.rera_id?.toLowerCase().includes(q) && !d.filename.toLowerCase().includes(q))
        return false
    }
    if (tagFilters.size > 0) {
      const anyMatch = [...tagFilters].some(tag => TAG_MATCH[tag]?.(d))
      if (!anyMatch) return false
    }
    return true
  })

  function toggleTagFilter(tag) {
    setTagFilters(prev => {
      const next = new Set(prev)
      next.has(tag) ? next.delete(tag) : next.add(tag)
      return next
    })
  }

  function toggleDoc(path, checked) {
    setSelected(prev => {
      const next = new Set(prev)
      checked ? next.add(path) : next.delete(path)
      return next
    })
  }

  function selectAll()   { setSelected(new Set(filtered.map(d => d.rel_doc))) }
  function selectNone()  { setSelected(new Set()) }

  function setField(field, value) {
    setForm(prev => ({ ...prev, [field]: value }))
  }

  function submit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)

    const body = {
      variant:        form.variant,
      state:          form.state.trim() || 'GJ',
      mode:           form.mode,
      prompt_version: form.prompt_version || 'v1',
      concurrency:    parseInt(form.concurrency) || 10,
      poll_tries:     parseInt(form.poll_tries) || 8,
      poll_interval:  parseInt(form.poll_interval) || 15,
    }
    if (selected.size > 0) {
      body.paths = [...selected]
    } else {
      body.limit = form.limit ? parseInt(form.limit) : null
    }

    fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) { setError(data.error); setSubmitting(false) }
        else onCreated(data.run_id)
      })
      .catch(err => { setError(String(err)); setSubmitting(false) })
  }

  const selLabel = selected.size > 0 ? `${selected.size} selected` : `All (${allDocs.length})`

  return (
    <div className="modal-overlay" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal modal--wide">
        <div className="modal-header">
          <h3 className="modal-title">New Benchmark Run</h3>
          <button className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <form className="modal-body" onSubmit={submit}>

          {/* Variant + State */}
          <div className="form-row form-row--2">
            <div className="form-field">
              <label className="form-label">Variant</label>
              <select className="form-select" value={form.variant} onChange={e => setField('variant', e.target.value)}>
                {VARIANTS.map(v => <option key={v.id} value={v.id}>{v.label}</option>)}
              </select>
            </div>
            <div className="form-field">
              <label className="form-label">State</label>
              {states.length > 0
                ? <select className="form-select" value={form.state} onChange={e => setField('state', e.target.value)}>
                    {states.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                : <input className="form-input" value={form.state} onChange={e => setField('state', e.target.value)} placeholder="Gujarat" spellCheck={false} />
              }
            </div>
          </div>

          {/* Mode */}
          <div className="form-field">
            <label className="form-label">Mode</label>
            <div className="seg-ctrl" style={{ alignSelf: 'flex-start' }}>
              {['async', 'batch'].map(m => (
                <button key={m} type="button"
                  className={`seg-btn${form.mode === m ? ' seg-btn--active' : ''}`}
                  onClick={() => setField('mode', m)}
                >{m}</button>
              ))}
            </div>
          </div>

          {/* Prompt version */}
          <div className="form-field">
            <label className="form-label">Prompt version</label>
            <div className="seg-ctrl" style={{ alignSelf: 'flex-start' }}>
              {promptVersions.map(v => (
                <button key={v.id} type="button"
                  className={`seg-btn${form.prompt_version === v.id ? ' seg-btn--active' : ''}`}
                  onClick={() => setField('prompt_version', v.id)}
                  title={v.description}
                >{v.id}</button>
              ))}
            </div>
          </div>

          {/* Limit / Concurrency / Batch options — hide limit when docs are selected */}
          <div className="form-row form-row--2">
            {selected.size === 0 && (
              <div className="form-field">
                <label className="form-label">Doc limit (optional)</label>
                <input className="form-input" type="number" value={form.limit}
                  onChange={e => setField('limit', e.target.value)} placeholder="All docs" min="1" />
              </div>
            )}
            {form.mode === 'async' && (
              <div className="form-field">
                <label className="form-label">Concurrency</label>
                <input className="form-input" type="number" value={form.concurrency}
                  onChange={e => setField('concurrency', e.target.value)} min="1" max="50" />
              </div>
            )}
            {form.mode === 'batch' && (
              <div className="form-field">
                <label className="form-label">Poll tries</label>
                <input className="form-input" type="number" value={form.poll_tries}
                  onChange={e => setField('poll_tries', e.target.value)} min="1" />
              </div>
            )}
          </div>
          {form.mode === 'batch' && (
            <div className="form-field">
              <label className="form-label">Poll interval (seconds)</label>
              <input className="form-input" type="number" value={form.poll_interval}
                onChange={e => setField('poll_interval', e.target.value)} min="5" />
            </div>
          )}

          {/* Doc picker */}
          <div className="dp-section">
            <button type="button" className="dp-toggle" onClick={() => setPickerOpen(v => !v)}>
              <span>{pickerOpen ? '▾' : '▸'} Select Documents</span>
              <span className="dp-toggle-count">{docsLoading ? 'loading…' : selLabel}</span>
            </button>
            {pickerOpen && (
              <div className="dp-panel">
                <div className="dp-toolbar">
                  <input
                    className="dp-search"
                    type="search"
                    placeholder="Search by ID or filename…"
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button type="button" className="dp-action" onClick={selectAll}>All</button>
                  <button type="button" className="dp-action" onClick={selectNone}>None</button>
                </div>
                <div className="dp-legend">
                  {[
                    { key: 'scan',      label: 'scan',      variant: 'scan' },
                    { key: 'text',      label: 'text',      variant: 'text' },
                    { key: 'extracted', label: 'extracted', variant: 'ext' },
                    { key: 'ran',       label: 'ran',       variant: 'ran' },
                    { key: 'viewed',    label: 'viewed',    variant: 'viewed' },
                  ].map(({ key, label, variant }) => (
                    <button
                      key={key}
                      type="button"
                      className={`dtag dtag--${variant} dp-filter-tag${tagFilters.has(key) ? ' dp-filter-tag--active' : ''}`}
                      onClick={() => toggleTagFilter(key)}
                      title={tagFilters.has(key) ? `Remove "${label}" filter` : `Filter by "${label}"`}
                    >{label}</button>
                  ))}
                  {tagFilters.size > 0 && (
                    <button type="button" className="dp-filter-clear" onClick={() => setTagFilters(new Set())}>
                      clear filters
                    </button>
                  )}
                </div>
                <div className="dp-list">
                  {docsLoading
                    ? <div className="dp-empty">Loading documents…</div>
                    : filtered.length === 0
                      ? <div className="dp-empty">No documents match.</div>
                      : filtered.map(doc => (
                          <DocPickerRow
                            key={doc.rel_doc}
                            doc={doc}
                            checked={selected.has(doc.rel_doc)}
                            viewedSet={viewedSet}
                            onChange={toggleDoc}
                          />
                        ))
                  }
                </div>
                <div className="dp-footer">
                  {filtered.length} shown · {allDocs.length} total · {selected.size} selected
                </div>
              </div>
            )}
          </div>

          {error && <div className="form-error">{error}</div>}

          <div className="modal-footer">
            <button type="button" className="action-btn" onClick={onClose}>Cancel</button>
            <button type="submit" className="action-btn action-btn--primary" disabled={submitting}>
              {submitting ? 'Starting…' : selected.size > 0 ? `Run ${selected.size} docs` : 'Start Run'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ─── Split view (existing doc viewer) ────────────────────────────────────────

function useMdPane(docIdx, tool) {
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [fetched, setFetched] = useState(false)

  const fetch_ = useCallback(() => {
    if (fetched) return
    setLoading(true)
    fetch(`/api/doc/${docIdx}/${tool}`)
      .then(r => r.text())
      .then(text => { setContent(text); setLoading(false); setFetched(true) })
      .catch(() => setLoading(false))
  }, [docIdx, tool, fetched])

  return { content, loading, fetch: fetch_ }
}

function SplitView({ doc, mdContent, mdLoading, onBack, onPrev, onNext, position, total }) {
  const [extraTool, setExtraTool] = useState(null)
  const [showPymu, setShowPymu]   = useState(doc.has_md !== false)
  const [rawPanes, setRawPanes]   = useState(new Set())

  useEffect(() => {
    function blockSaveAndPrint(e) {
      if (e.metaKey || e.ctrlKey) {
        if (['s', 'p'].includes(e.key.toLowerCase())) {
          e.preventDefault()
          e.stopImmediatePropagation()
        }
      }
    }
    window.addEventListener('keydown', blockSaveAndPrint, { capture: true })
    return () => window.removeEventListener('keydown', blockSaveAndPrint, { capture: true })
  }, [])

  function toggleRaw(pane) {
    setRawPanes(prev => {
      const next = new Set(prev)
      next.has(pane) ? next.delete(pane) : next.add(pane)
      return next
    })
  }

  const docling = useMdPane(doc.idx, 'docling')
  const marker  = useMdPane(doc.idx, 'marker')

  function selectTool(tool) {
    if (extraTool === tool) {
      setExtraTool(null)
      setShowPymu(true)
    } else {
      setExtraTool(tool)
      if (tool === 'docling') docling.fetch()
      else                    marker.fetch()
    }
  }

  const panes = [
    'pdf',
    ...(showPymu ? ['pymu'] : []),
    ...(extraTool ? [extraTool] : []),
  ]

  const hasAltTools = doc.has_docling || doc.has_marker
  const altPane = extraTool === 'docling' ? docling : marker

  return (
    <div className="split-screen">
      <header className="split-header">
        <button className="back-btn" onClick={onBack}>
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true" width="14" height="14">
            <path d="M10 4L6 8l4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          Back
        </button>

        <div className="nav-controls">
          <button
            className="nav-btn"
            onClick={onPrev}
            disabled={!onPrev}
            title="Previous document (←)"
            aria-label="Previous"
          >
            <svg viewBox="0 0 16 16" fill="none" width="14" height="14" aria-hidden="true">
              <path d="M10 4L6 8l4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
          <span className="nav-pos">{position} / {total.toLocaleString()}</span>
          <button
            className="nav-btn"
            onClick={onNext}
            disabled={!onNext}
            title="Next document (→)"
            aria-label="Next"
          >
            <svg viewBox="0 0 16 16" fill="none" width="14" height="14" aria-hidden="true">
              <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>

        <div className="split-doc-info">
          <span className="split-rera">{doc.rera_id}</span>
          <span className="split-sep">·</span>
          <span className="split-state">{doc.state}</span>
          <span className="split-sep">·</span>
          <span className="split-period">{doc.period}</span>
        </div>

        {hasAltTools && (
          <div className="split-controls">
            {extraTool && (
              <button
                className={`ctrl-btn${!showPymu ? ' ctrl-btn--muted' : ''}`}
                onClick={() => setShowPymu(v => !v)}
                title={showPymu ? 'Hide PyMuPDF4LLM pane' : 'Show PyMuPDF4LLM pane'}
              >
                PyMu {showPymu ? '✕' : '+'}
              </button>
            )}
            <span className="ctrl-label">Compare</span>
            {doc.has_docling && (
              <button
                className={`ctrl-btn${extraTool === 'docling' ? ' ctrl-btn--active' : ''}`}
                onClick={() => selectTool('docling')}
              >
                Docling
              </button>
            )}
            {doc.has_marker && (
              <button
                className={`ctrl-btn${extraTool === 'marker' ? ' ctrl-btn--active' : ''}`}
                onClick={() => selectTool('marker')}
              >
                Marker
              </button>
            )}
          </div>
        )}
      </header>

      <div className="split-body">
        {panes.map((pane, i) => (
          <PaneWithDivider key={pane} showDivider={i > 0}>
            {pane === 'pdf' && (
              <>
                <PaneHeader label="PDF Document" />
                <div className="pane-content">
                  {doc.has_doc
                    ? <iframe
                        src={`/api/doc/${doc.idx}/pdf#toolbar=0&navpanes=0`}
                        title="PDF"
                        className="pdf-frame"
                      />
                    : <div className="pane-empty">No PDF available.</div>
                  }
                </div>
              </>
            )}
            {pane === 'pymu' && (
              <>
                <PaneHeader
                  label="PyMuPDF4LLM"
                  chars={mdContent.length}
                  isRaw={rawPanes.has('pymu')}
                  onToggleRaw={() => toggleRaw('pymu')}
                />
                <div className="pane-content pane-content--md">
                  {mdLoading
                    ? <div className="pane-empty">Loading…</div>
                    : <MdPane content={mdContent} isRaw={rawPanes.has('pymu')} />
                  }
                </div>
              </>
            )}
            {(pane === 'docling' || pane === 'marker') && (
              <>
                <PaneHeader
                  label={pane === 'docling' ? 'Docling' : 'Marker'}
                  chars={altPane.content.length}
                  isRaw={rawPanes.has(pane)}
                  onToggleRaw={() => toggleRaw(pane)}
                />
                <div className="pane-content pane-content--md">
                  {altPane.loading
                    ? <div className="pane-empty">Loading…</div>
                    : <MdPane content={altPane.content} isRaw={rawPanes.has(pane)} />
                  }
                </div>
              </>
            )}
          </PaneWithDivider>
        ))}
      </div>
    </div>
  )
}

// ─── Shared sub-components ────────────────────────────────────────────────────

function PaneWithDivider({ showDivider, children }) {
  return (
    <>
      {showDivider && <div className="pane-divider" />}
      <div className="pane">{children}</div>
    </>
  )
}

function PaneHeader({ label, chars, isRaw, onToggleRaw }) {
  return (
    <div className="pane-header">
      <span className="pane-label">{label}</span>
      <div className="pane-header-right">
        {chars > 0 && <span className="pane-meta">{chars.toLocaleString()} chars</span>}
        {onToggleRaw && (
          <button
            className={`raw-toggle${isRaw ? ' raw-toggle--active' : ''}`}
            onClick={onToggleRaw}
            title={isRaw ? 'Switch to rendered view' : 'Switch to raw markdown'}
          >
            {isRaw ? 'Rendered' : 'Raw'}
          </button>
        )}
      </div>
    </div>
  )
}

function MdPane({ content, isRaw }) {
  if (isRaw) {
    return <pre className="md-raw">{content}</pre>
  }
  return (
    <div className="md-prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}

// ─── Runs UI micro-components ─────────────────────────────────────────────────

function RunStatusBadge({ status }) {
  return (
    <span className={`badge run-status-badge run-status--${status}`}>{status}</span>
  )
}

function StatCell({ label, value, variant }) {
  return (
    <div className={`run-stat${variant ? ` run-stat--${variant}` : ''}`}>
      <span className="run-stat-value">{value}</span>
      <span className="run-stat-label">{label}</span>
    </div>
  )
}

function DocStatusDot({ status }) {
  const symbols = { success: '✓', error: '✗', skipped: '—', pending: '…' }
  return (
    <span className={`run-doc-status run-doc-status--${status}`}>
      {symbols[status] || '?'}
    </span>
  )
}
