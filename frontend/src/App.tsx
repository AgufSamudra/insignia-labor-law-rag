import { useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import './App.css'

type Tab = 'asking' | 'upload'

function FileIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24" className="file-icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M8 13h8M8 17h6" /></svg>
}

function App() {
  const [activeTab, setActiveTab] = useState<Tab>('asking')
  const [question, setQuestion] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [submitted, setSubmitted] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(event.target.files ?? [])
    setFiles((currentFiles) => {
      const existingNames = new Set(currentFiles.map((file) => file.name))
      return [...currentFiles, ...selectedFiles.filter((file) => !existingNames.has(file.name))]
    })
    event.target.value = ''
  }

  const removeFile = (fileName: string) => setFiles((currentFiles) => currentFiles.filter((file) => file.name !== fileName))
  const formatSize = (size: number) => size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / (1024 * 1024)).toFixed(1)} MB`

  return (
    <main className="app-shell">
      <header className="topbar"><div className="brand-mark">LL</div><div><p className="eyebrow">INSIGNIA</p><h1>Labor Law Assistant</h1></div><span className="status-pill"><span className="status-dot" /> Ready</span></header>
      <section className="workspace">
        <div className="intro"><p className="eyebrow accent-text">YOUR LEGAL COMPANION</p><h2>Find clarity in labor law.</h2><p className="intro-copy">Ask a question or add your documents to get clear, grounded answers.</p></div>
        <nav className="tabs" aria-label="Assistant actions">
          <button className={activeTab === 'asking' ? 'tab active' : 'tab'} onClick={() => setActiveTab('asking')} type="button"><span className="tab-number">01</span> Asking</button>
          <button className={activeTab === 'upload' ? 'tab active' : 'tab'} onClick={() => setActiveTab('upload')} type="button"><span className="tab-number">02</span> Upload Document</button>
        </nav>
        {activeTab === 'asking' ? <section className="panel asking-panel">
          <div className="panel-heading"><div><p className="panel-kicker">ASK YOUR QUESTION</p><h3>What would you like to know?</h3></div><span className="shortcut">⌘ ↵</span></div>
          <textarea value={question} onChange={(event) => { setQuestion(event.target.value); setSubmitted(false) }} placeholder="e.g. What are my rights during a probation period?" aria-label="Your labor law question" />
          <div className="panel-footer"><span className="hint">Answers are based on your uploaded documents and applicable law.</span><button className="primary-button" type="button" onClick={() => setSubmitted(true)} disabled={!question.trim()}>{submitted ? 'Question sent' : 'Ask Assistant'} <span aria-hidden="true">↗</span></button></div>
        </section> : <section className="panel upload-panel">
          <div className="panel-heading"><div><p className="panel-kicker">YOUR KNOWLEDGE BASE</p><h3>Upload your documents</h3></div><span className="file-count">{files.length} {files.length === 1 ? 'file' : 'files'}</span></div>
          <button className="dropzone" type="button" onClick={() => fileInputRef.current?.click()}><span className="upload-symbol">↑</span><span><strong>Choose files</strong> or click to browse</span><small>PDF, DOC, DOCX, or TXT · Max 10 MB each</small></button>
          <input ref={fileInputRef} className="visually-hidden" type="file" multiple accept=".pdf,.doc,.docx,.txt" onChange={handleFiles} />
          {files.length > 0 ? <div className="file-list"><p className="list-label">UPLOADED DOCUMENTS</p>{files.map((file) => <div className="file-row" key={file.name}><FileIcon /><span className="file-details"><strong>{file.name}</strong><small>{formatSize(file.size)}</small></span><span className="uploaded-check">✓</span><button className="remove-button" type="button" onClick={() => removeFile(file.name)} aria-label={`Remove ${file.name}`}>×</button></div>)}</div> : <p className="empty-state">No documents uploaded yet.</p>}
        </section>}
      </section>
      <footer>INSIGNIA <span>•</span> Built for better decisions</footer>
    </main>
  )
}

export default App
