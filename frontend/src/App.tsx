import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import './App.css'

type Tab = 'asking' | 'upload'
type DocumentStatus = 'queued' | 'processing' | 'completed' | 'failed'
type UploadStatus = 'idle' | 'processing' | 'success' | 'error'

type FileProgress = {
  status: DocumentStatus
  stage: string
  message: string
  chunks?: number
  indexed_chunks?: number
  progress_current?: number
  progress_total?: number
  progress_percent?: number
  progress_unit?: string
  error?: string
}

type ProgressEvent = FileProgress & {
  type: 'document_status' | 'job_status'
  job_id: string
  filename: string
  log: string
}

type StoredFile = {
  name: string
  size: number
  file?: File
}

type PersistedUploadState = {
  files: Array<Pick<StoredFile, 'name' | 'size'>>
  fileProgress: Record<string, FileProgress>
  activeJobIds: string[]
  fileJobIds: Record<string, string>
}

type QuerySource = {
  id: number
  document: string
  page: number | null
  page_end?: number
  chapter?: string
  article?: string
  paragraph?: string
  chunk_id: string
  text?: string
  rerank_score?: number
}

type QueryResponse = {
  answer: string
  sources: QuerySource[]
  retrieval: {
    retrieved_chunks?: number
    candidate_chunks?: number
    reranked_chunks?: number
    latency_ms?: number
    rewritten_query?: string | null
  }
}

const API_BASE_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'
const UPLOAD_STATE_KEY = 'insignia-upload-state'
const QUERY_STAGES = [
  'Menormalisasi pertanyaan',
  'Membuat dense dan sparse embedding',
  'Mencari dokumen relevan di Qdrant',
  'Menggabungkan hasil dengan RRF Fusion',
  'Melakukan reranking kandidat teratas',
  'Memperluas konteks Pasal dan Ayat',
  'Menyusun jawaban dan referensi',
]

function stripThinking(answer: string) {
  return answer
    .replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '')
    .replace(/<\/?think>/gi, '')
    .replace(/\*\*/g, '')
    .replace(/__/g, '')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function loadUploadState(): PersistedUploadState {
  const emptyState: PersistedUploadState = {
    files: [],
    fileProgress: {},
    activeJobIds: [],
    fileJobIds: {},
  }

  try {
    const savedState = window.localStorage.getItem(UPLOAD_STATE_KEY)
    if (!savedState) return emptyState
    const parsedState = JSON.parse(savedState) as Partial<PersistedUploadState>
    return {
      files: Array.isArray(parsedState.files) ? parsedState.files : [],
      fileProgress: parsedState.fileProgress ?? {},
      activeJobIds: Array.isArray(parsedState.activeJobIds) ? parsedState.activeJobIds : [],
      fileJobIds: parsedState.fileJobIds ?? {},
    }
  } catch {
    return emptyState
  }
}

function FileIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24" className="file-icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M8 13h8M8 17h6" /></svg>
}

function App() {
  const [initialUploadState] = useState<PersistedUploadState>(loadUploadState)
  const [activeTab, setActiveTab] = useState<Tab>('asking')
  const [question, setQuestion] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [queryLoading, setQueryLoading] = useState(false)
  const [queryStage, setQueryStage] = useState(0)
  const [queryError, setQueryError] = useState('')
  const [queryResponse, setQueryResponse] = useState<QueryResponse | null>(null)
  const [files, setFiles] = useState<StoredFile[]>(initialUploadState.files)
  const [fileProgress, setFileProgress] = useState<Record<string, FileProgress>>(initialUploadState.fileProgress)
  const [fileJobIds, setFileJobIds] = useState<Record<string, string>>(initialUploadState.fileJobIds)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const eventSourcesRef = useRef(new Map<string, EventSource>())
  const activeJobIdsRef = useRef(new Set<string>())
  const [activeJobIds, setActiveJobIds] = useState<Set<string>>(
    () => new Set(initialUploadState.activeJobIds),
  )

  useEffect(() => () => {
    eventSourcesRef.current.forEach((eventSource) => eventSource.close())
  }, [])

  useEffect(() => {
    window.localStorage.setItem(UPLOAD_STATE_KEY, JSON.stringify({
      files: files.map(({ name, size }) => ({ name, size })),
      fileProgress,
      activeJobIds: [...activeJobIds],
      fileJobIds,
    }))
  }, [activeJobIds, fileJobIds, fileProgress, files])

  useEffect(() => {
    activeJobIdsRef.current = new Set(activeJobIds)
  }, [activeJobIds])

  const updateFileProgress = useCallback((event: ProgressEvent) => {
    console.info(event.log)

    if (event.type === 'document_status' && event.filename !== '-') {
      setFileProgress((currentProgress) => ({
        ...currentProgress,
        [event.filename]: {
          ...currentProgress[event.filename],
          status: event.status,
          stage: event.stage,
          message: event.message,
          chunks: event.chunks,
          indexed_chunks: event.indexed_chunks,
          progress_current: event.progress_current,
          progress_total: event.progress_total,
          progress_percent: event.progress_percent,
          progress_unit: event.progress_unit,
          error: event.error,
        },
      }))
    }
  }, [])

  const closeJobStream = useCallback((jobId: string, eventSource: EventSource) => {
    activeJobIdsRef.current.delete(jobId)
    setActiveJobIds(new Set(activeJobIdsRef.current))
    eventSource.close()
    eventSourcesRef.current.delete(jobId)
  }, [])

  const markJobAsUnavailable = useCallback((filenames: string[]) => {
    const message = 'Status job tidak ditemukan; proses mungkin berhenti saat backend restart'
    setFileProgress((currentProgress) => {
      const nextProgress = { ...currentProgress }
      filenames.forEach((filename) => {
        const progress = nextProgress[filename]
        if (progress?.status === 'queued' || progress?.status === 'processing') {
          nextProgress[filename] = {
            ...progress,
            status: 'failed',
            stage: 'failed',
            message,
            error: message,
          }
        }
      })
      return nextProgress
    })
  }, [])

  const subscribeToJob = useCallback((jobId: string, filenames: string[]) => {
    if (eventSourcesRef.current.has(jobId)) return

    const eventSource = new EventSource(`${API_BASE_URL}/documents/${jobId}/events`)
    eventSourcesRef.current.set(jobId, eventSource)
    let fallbackRequested = false

    const handleEvent = (messageEvent: MessageEvent<string>) => {
      try {
        const event = JSON.parse(messageEvent.data) as ProgressEvent
        updateFileProgress(event)
        if (event.type === 'job_status') {
          closeJobStream(jobId, eventSource)
        }
      } catch (error) {
        console.error('Invalid progress event from server', error)
      }
    }

    eventSource.addEventListener('document_status', handleEvent)
    eventSource.addEventListener('job_status', handleEvent)
    eventSource.onerror = () => {
      console.warn(`Progress stream unavailable | job_id=${jobId}`)
      if (fallbackRequested) return
      fallbackRequested = true
      void fetch(`${API_BASE_URL}/documents/${jobId}/status`)
        .then(async (response) => {
          if (response.status === 404) {
            markJobAsUnavailable(filenames)
            closeJobStream(jobId, eventSource)
            return null
          }
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          return response.json() as Promise<{ events?: ProgressEvent[] }>
        })
        .then((result) => {
          result?.events?.forEach(updateFileProgress)
          const terminalEvent = result?.events?.find(
            (event) => event.type === 'job_status' && (event.status === 'completed' || event.status === 'failed'),
          )
          if (terminalEvent) closeJobStream(jobId, eventSource)
        })
        .catch((error) => {
          fallbackRequested = false
          console.warn(`Progress status fallback failed | job_id=${jobId} error=${error}`)
        })
    }
  }, [closeJobStream, markJobAsUnavailable, updateFileProgress])

  useEffect(() => {
    if (activeJobIdsRef.current.size === 0) return
    activeJobIdsRef.current.forEach((jobId) => {
      const filenames = Object.entries(fileJobIds)
        .filter(([, storedJobId]) => storedJobId === jobId)
        .map(([filename]) => filename)
      subscribeToJob(jobId, filenames)
    })
  }, [fileJobIds, subscribeToJob])

  const markFilesAsFailed = (selectedFiles: File[], message: string) => {
    setFileProgress((currentProgress) => {
      const nextProgress = { ...currentProgress }
      selectedFiles.forEach((file) => {
        nextProgress[file.name] = {
          status: 'failed',
          stage: 'failed',
          message,
          error: message,
        }
      })
      return nextProgress
    })
  }

  const uploadDocuments = async (selectedFiles: File[]) => {
    if (selectedFiles.length === 0) return

    setFileProgress((currentProgress) => {
      const nextProgress = { ...currentProgress }
      selectedFiles.forEach((file) => {
        nextProgress[file.name] = {
          status: 'queued',
          stage: 'upload',
          message: 'Menunggu proses di background',
        }
      })
      return nextProgress
    })

    const formData = new FormData()
    selectedFiles.forEach((file) => formData.append('files', file))

    try {
      const response = await fetch(`${API_BASE_URL}/documents/upload`, {
        method: 'POST',
        body: formData,
      })
      const result = await response.json().catch(() => null)

      if (!response.ok) {
        throw new Error(result?.detail ?? 'Upload gagal diterima server')
      }

      const jobId = result?.job_id as string | undefined
      if (!jobId) throw new Error('Server tidak mengembalikan job_id')

      const failedFiles = result?.failed ?? []
      if (failedFiles.length > 0) {
        failedFiles.forEach((file: { filename: string; error: string }) => {
          setFileProgress((currentProgress) => ({
            ...currentProgress,
            [file.filename]: {
              status: 'failed',
              stage: 'failed',
              message: file.error,
              error: file.error,
            },
          }))
        })
      }

      const acceptedFileNames = (result?.files ?? [])
        .map((file: { filename: string }) => file.filename)
      setFileJobIds((currentFileJobIds) => {
        const nextFileJobIds = { ...currentFileJobIds }
        acceptedFileNames.forEach((filename: string) => { nextFileJobIds[filename] = jobId })
        return nextFileJobIds
      })
      activeJobIdsRef.current.add(jobId)
      setActiveJobIds(new Set(activeJobIdsRef.current))
      subscribeToJob(jobId, acceptedFileNames)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Terjadi error saat upload'
      markFilesAsFailed(selectedFiles, message)
    }
  }

  const handleFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(event.target.files ?? [])
      .filter((file) => file.name.toLowerCase().endsWith('.pdf'))
    const existingNames = new Set(files.map((file) => file.name))
    const newFiles = selectedFiles.filter((file) => !existingNames.has(file.name))

    if (newFiles.length > 0) {
      setFiles((currentFiles) => [
        ...currentFiles,
        ...newFiles.map((file) => ({ name: file.name, size: file.size, file })),
      ])
      void uploadDocuments(newFiles)
    }
    event.target.value = ''
  }

  const removeFile = (fileName: string) => {
    const progress = fileProgress[fileName]
    if (progress?.status === 'queued' || progress?.status === 'processing') return
    setFiles((currentFiles) => currentFiles.filter((file) => file.name !== fileName))
    setFileProgress((currentProgress) => {
      const nextProgress = { ...currentProgress }
      delete nextProgress[fileName]
      return nextProgress
    })
    setFileJobIds((currentFileJobIds) => {
      const nextFileJobIds = { ...currentFileJobIds }
      delete nextFileJobIds[fileName]
      return nextFileJobIds
    })
  }

  const formatSize = (size: number) => size < 1024 * 1024
    ? `${Math.max(1, Math.round(size / 1024))} KB`
    : `${(size / (1024 * 1024)).toFixed(1)} MB`

  const askQuestion = async () => {
    const query = question.trim()
    if (!query || queryLoading) return

    setQueryLoading(true)
    setQueryStage(0)
    setSubmitted(false)
    setQueryError('')
    const progressTimer = window.setInterval(() => {
      setQueryStage((currentStage) => Math.min(currentStage + 1, QUERY_STAGES.length - 1))
    }, 1200)
    try {
      const response = await fetch(`${API_BASE_URL}/v1/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
      })
      const result = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(result?.detail ?? 'Pertanyaan gagal diproses')
      }
      if (!result?.answer) throw new Error('Server tidak mengembalikan jawaban')
      setQueryResponse({ ...result, answer: stripThinking(String(result.answer)) } as QueryResponse)
      setSubmitted(true)
    } catch (error) {
      setQueryResponse(null)
      setQueryError(error instanceof Error ? error.message : 'Terjadi error saat bertanya')
    } finally {
      window.clearInterval(progressTimer)
      setQueryLoading(false)
    }
  }

  const pendingCount = Object.values(fileProgress)
    .filter((progress) => progress.status === 'queued' || progress.status === 'processing').length
  const failedCount = Object.values(fileProgress)
    .filter((progress) => progress.status === 'failed').length
  const hasCompletedFile = Object.values(fileProgress)
    .some((progress) => progress.status === 'completed')
  const uploadStatus: UploadStatus = pendingCount > 0
    ? 'processing'
    : failedCount > 0
      ? 'error'
      : hasCompletedFile
        ? 'success'
        : 'idle'
  const statusLabel = pendingCount > 0 ? 'Processing' : failedCount > 0 ? 'Needs attention' : 'Ready'

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-mark">LL</div>
        <div><p className="eyebrow">INSIGNIA</p><h1>Labor Law Assistant</h1></div>
        <span className={`status-pill status-${uploadStatus}`}><span className="status-dot" /> {statusLabel}</span>
      </header>

      <section className="workspace">
        <div className="intro"><p className="eyebrow accent-text">YOUR LEGAL COMPANION</p><h2>Find clarity in labor law.</h2><p className="intro-copy">Ask a question or add your documents to get clear, grounded answers.</p></div>

        <nav className="tabs" aria-label="Assistant actions">
          <button className={activeTab === 'asking' ? 'tab active' : 'tab'} onClick={() => setActiveTab('asking')} type="button"><span className="tab-number">01</span> Asking</button>
          <button className={activeTab === 'upload' ? 'tab active' : 'tab'} onClick={() => setActiveTab('upload')} type="button"><span className="tab-number">02</span> Upload Document</button>
        </nav>

        {activeTab === 'asking' ? <section className="panel asking-panel">
          <div className="panel-heading"><div><p className="panel-kicker">ASK YOUR QUESTION</p><h3>What would you like to know?</h3></div><span className="shortcut">⌘ ↵</span></div>
          <textarea value={question} onChange={(event) => { setQuestion(event.target.value); setSubmitted(false); setQueryError(''); setQueryResponse(null) }} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); void askQuestion() } }} placeholder="e.g. Berapa lama maksimal PKWT?" aria-label="Your labor law question" />
          <div className="panel-footer"><span className="hint">Answers are based on your uploaded documents and applicable law.</span><button className="primary-button" type="button" disabled={!question.trim() || queryLoading} onClick={() => void askQuestion()}>{queryLoading ? 'Thinking...' : submitted ? 'Ask again' : 'Ask Assistant'} <span aria-hidden="true">↗</span></button></div>
          {queryLoading ? <div className="query-progress" role="status" aria-live="polite">{QUERY_STAGES.map((stage, index) => <div className={index < queryStage ? 'query-stage complete' : index === queryStage ? 'query-stage active' : 'query-stage'} key={stage}><span className="query-stage-mark">{index < queryStage ? '✓' : index === queryStage ? '›' : '·'}</span>{stage}{index === queryStage ? <span className="query-stage-dots">...</span> : null}</div>)}</div> : null}
          {queryError ? <p className="query-error" role="alert">{queryError}</p> : null}
          {queryResponse ? <div className="query-result" aria-live="polite">
            <div className="result-heading"><p className="panel-kicker">GROUNDED ANSWER</p><span className="result-meta">{queryResponse.retrieval.retrieved_chunks ?? queryResponse.sources.length} sumber · {queryResponse.retrieval.latency_ms ?? 0} ms</span></div>
            <div className="answer-copy">{queryResponse.answer}</div>
            {queryResponse.sources.length > 0 ? <div className="source-section"><p className="list-label">SOURCES · CLICK TO OPEN</p><ol className="source-list">{queryResponse.sources.map((source) => <li className="source-item" key={`${source.id}-${source.chunk_id}`}><details><summary className="source-summary"><strong>{source.document}</strong><span>Halaman {source.page}{source.page_end && source.page_end !== source.page ? `–${source.page_end}` : ''}{source.article ? ` · ${source.article}` : ''}{source.paragraph ? ` · ${source.paragraph}` : ''}</span></summary><p className="source-content">{source.text ?? 'Isi sumber tidak tersedia.'}</p></details></li>)}</ol></div> : null}
          </div> : null}
        </section> : <section className="panel upload-panel">
          <div className="panel-heading"><div><p className="panel-kicker">YOUR KNOWLEDGE BASE</p><h3>Upload your documents</h3></div><span className="file-count">{files.length} {files.length === 1 ? 'file' : 'files'}</span></div>
          <button className="dropzone" type="button" onClick={() => fileInputRef.current?.click()}><span className="upload-symbol">↑</span><span><strong>Choose PDF files</strong> to upload</span><small>PDF · Processing runs in background</small></button>
          <input ref={fileInputRef} className="visually-hidden" type="file" multiple accept=".pdf,application/pdf" onChange={handleFiles} />

          {files.length > 0 ? <div className="file-list"><p className="list-label">DOCUMENTS</p>{files.map((file) => {
            const progress = fileProgress[file.name]
            const isCompleted = progress?.status === 'completed'
            const isPending = progress?.status === 'queued' || progress?.status === 'processing'
            const fileMessage = isCompleted
              ? `Terindeks · ${progress.indexed_chunks ?? 0} chunk`
              : progress?.message ?? 'Menunggu proses'
            return <div className="file-row" key={file.name} aria-live="polite"><FileIcon /><span className="file-details"><strong>{file.name}</strong><small title={fileMessage}>{formatSize(file.size)} · {fileMessage}</small></span><span className={isCompleted ? 'uploaded-check' : isPending ? 'file-spinner' : 'file-failed'} aria-label={isCompleted ? 'Selesai' : isPending ? 'Sedang diproses' : 'Gagal'}>{isCompleted ? '✓' : isPending ? '' : '!'}</span><button className="remove-button" type="button" onClick={() => removeFile(file.name)} disabled={isPending} aria-label={`Remove ${file.name}`}>×</button></div>
          })}</div> : <p className="empty-state">No documents uploaded yet.</p>}
        </section>}
      </section>
      <footer>INSIGNIA <span>•</span> Built for better decisions</footer>
    </main>
  )
}

export default App
