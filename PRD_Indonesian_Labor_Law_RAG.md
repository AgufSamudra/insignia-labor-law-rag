# PRD — Indonesian Labor Law RAG

## 1. Product Overview

Membangun **Retrieval-Augmented Generation (RAG)** yang menjawab pertanyaan tentang hukum ketenagakerjaan Indonesia berdasarkan dua dokumen sumber:

1. `UU No. 13 Tahun 2003 tentang Ketenagakerjaan`
2. `PP No. 35 Tahun 2021 tentang PKWT, Alih Daya, Waktu Kerja, dan PHK`

Sistem harus berjalan end-to-end dari **raw PDF → extraction/OCR → indexing → retrieval → answer generation**, dengan jawaban dalam **Bahasa Indonesia** dan sumber yang dapat ditelusuri.

---

## 2. Goal

Sistem harus:

- Menjawab pertanyaan berdasarkan isi dokumen, bukan pengetahuan bebas model.
- Menghasilkan jawaban yang relevan dan akurat dalam Bahasa Indonesia.
- Menampilkan minimal **nama dokumen dan nomor halaman** pada setiap sumber jawaban.
- Menangani pertanyaan ambigu atau tidak lengkap dengan baik.
- Memproses PDF native maupun scanned.
- Mudah dijalankan oleh evaluator melalui interface yang sederhana.
- Menyatakan keterbatasan ketika bukti dari dokumen tidak cukup.

---

## 3. Non-Goals

Tidak termasuk scope utama:

- Legal advice atau keputusan hukum final.
- Crawling regulasi dari internet.
- Authentication / user management.
- Dashboard admin.
- Fine-tuning LLM.
- Multi-tenant architecture.
- Penyimpanan conversation history jangka panjang.

---

## 4. Users

### Primary User
Evaluator technical test yang akan memberikan pertanyaan terkait dua dokumen ketenagakerjaan.

### User Need
User ingin mendapatkan jawaban yang:

- cepat dipahami;
- langsung menjawab pertanyaan;
- memiliki dasar dokumen;
- dapat diverifikasi melalui citation.

---

## 5. Functional Requirements

### FR-01 — PDF Ingestion

Sistem harus menerima kedua PDF sebagai source document.

Pipeline ingestion:

```text
PDF
↓
Detect native/scanned page
↓
Text extraction / OCR
↓
Text normalization
↓
Structure detection
↓
Chunking
↓
Embedding + lexical indexing
↓
Vector / search database
```

Metadata minimal setiap chunk:

```text
document_id
document_name
page_number
section
article_number
chunk_id
text
```

---

### FR-02 — Document Extraction

Sistem harus:

- menggunakan text extraction untuk native PDF;
- menggunakan OCR untuk halaman scanned;
- mempertahankan informasi nomor halaman;
- membersihkan noise seperti repeated header/footer jika diperlukan;
- tidak menghilangkan struktur hukum penting seperti:
  - BAB
  - Bagian
  - Pasal
  - Ayat

---

### FR-03 — Chunking

Chunking harus **structure-aware**, bukan hanya fixed token splitting.

Prioritas boundary:

```text
BAB → Bagian → Pasal → Ayat
```

Chunk boleh memiliki overlap kecil apabila konteks antarbagian diperlukan.

Setiap chunk wajib tetap memiliki metadata sumber dan halaman.

---

### FR-04 — Query Processing

Input utama:

```json
{
  "query": "Berapa lama maksimal PKWT?"
}
```

Sistem harus:

1. menerima pertanyaan Bahasa Indonesia;
2. mendeteksi query yang ambigu / terlalu pendek;
3. melakukan query normalization atau rewrite bila membantu retrieval;
4. mempertahankan intent asli user.

Query rewrite tidak boleh mengubah makna pertanyaan.

---

### FR-05 — Hybrid Retrieval

Retrieval menggunakan kombinasi:

- **Dense semantic retrieval**
- **Sparse retrieval**

Dense dan sparse representation dihasilkan menggunakan **BGE-M3** dan disimpan pada Qdrant sebagai hybrid index.
Fusion hasil retrieval menggunakan metode seperti **Reciprocal Rank Fusion (RRF)** sebelum reranking.

Tujuannya:

- dense search menangkap kemiripan makna;
- sparse retrieval menangkap keyword penting, istilah hukum, nomor pasal, singkatan, dan exact-term matching.

Hasil dense dan sparse retrieval digabungkan sebelum reranking.

---

### FR-06 — Reranking

Candidate hasil hybrid retrieval harus direrank sebelum dikirim ke LLM.

Target:

```text
Hybrid retrieval
→ top 20–30 candidates
→ reranker
→ top 5–8 context chunks
```

Reranker harus mendukung Bahasa Indonesia / multilingual.

---

### FR-07 — Answer Generation

LLM hanya menjawab menggunakan retrieved context.

Jawaban harus:

- menggunakan Bahasa Indonesia;
- langsung menjawab pertanyaan;
- tidak mengarang informasi yang tidak ditemukan;
- menjelaskan konflik atau ketidakcukupan informasi apabila terjadi;
- menyertakan citation.

Contoh:

```text
PKWT berdasarkan jangka waktu dapat dibuat paling lama 5 tahun,
termasuk perpanjangannya.

Sumber:
- PP No. 35 Tahun 2021, Pasal 8, halaman 7
```

---

### FR-08 — Citation

Setiap klaim utama yang berasal dari dokumen harus traceable.

Citation minimal:

```text
document_name
page_number
```

Jika tersedia, sertakan juga:

```text
BAB
Pasal
Ayat
```

Contoh response API:

```json
{
  "answer": "....",
  "sources": [
    {
      "document": "PP No. 35 Tahun 2021",
      "page": 7,
      "article": "Pasal 8",
      "chunk_id": "pp35-p7-pasal8-01"
    }
  ]
}
```

---

### FR-09 — Insufficient Evidence

Jika retrieved context tidak cukup mendukung jawaban, sistem harus mengatakan bahwa informasi tidak ditemukan atau bukti belum cukup.

Sistem **tidak boleh melakukan hallucination untuk mengisi gap**.

Contoh:

```text
Saya tidak menemukan dasar yang cukup pada dua dokumen yang tersedia
untuk menjawab pertanyaan tersebut.
```

---

### FR-10 — Query Interface

Interface minimum menggunakan **REST API**.

Required endpoint:

```http
POST /v1/query
```

Optional operational endpoint:

```http
GET /health
```

Contoh response:

```json
{
  "answer": "...",
  "sources": [...],
  "retrieval": {
    "retrieved_chunks": 6
  }
}
```

---

## 6. Proposed Technical Stack

| Component | Choice |
|---|---|
| Language | Python |
| API | FastAPI |
| PDF extraction | PyMuPDF |
| OCR fallback | OCR engine for scanned pages |
| Embedding | BGE-M3 |
| Sparse retrieval | BGE-M3 sparse embeddings |
| Vector / search store | Qdrant |
| Reranker | BGE Reranker v2 M3 |
| LLM | Configurable via environment variable |
| Containerization | Docker / Docker Compose |

### Why

**BGE-M3**
- multilingual;
- cocok untuk semantic retrieval Bahasa Indonesia;
- mendukung retrieval use case dengan dokumen panjang dan terminologi spesifik.

**BGE Reranker v2 M3**
- multilingual;
- cross-encoder reranking meningkatkan precision kandidat setelah retrieval.

**Hybrid Dense + Sparse**
- dense retrieval menangkap semantic similarity dan paraphrase;
- sparse retrieval memperkuat exact-term matching seperti nomor pasal, istilah hukum, dan singkatan;
- BGE-M3 dipakai untuk menghasilkan dense dan sparse representation dalam satu model, sehingga pipeline lebih konsisten dan sederhana.

---

## 7. High-Level Architecture

```text
                ┌─────────────────┐
                │   Source PDFs   │
                └────────┬────────┘
                         │
                ┌────────▼────────┐
                │ PDF Processor   │
                │ Extract / OCR   │
                └────────┬────────┘
                         │
                ┌────────▼────────┐
                │ Structure-aware │
                │    Chunking     │
                └────────┬────────┘
                         │
              ┌──────────▼──────────┐
              │ Dense + Sparse Index │
              │      Indexing       │
              └──────────┬──────────┘
                         │
                    ┌────▼────┐
                    │ Qdrant  │
                    └────┬────┘
                         │
User Query               │
    │                    │
    ▼                    │
Query Processing         │
    │                    │
    └──────► Hybrid Retrieval
                    │
                    ▼
                 Reranker
                    │
                    ▼
              Context Builder
                    │
                    ▼
                   LLM
                    │
                    ▼
          Answer + Source Citation
```

---

## 8. Retrieval Pipeline

```text
User Query
↓
Normalize query
↓
Optional query rewrite
↓
Dense retrieval ─────┐
                     ├─→ Fusion
Sparse retrieval ─────┘
↓
Top candidates
↓
Cross-encoder reranking
↓
Top relevant chunks
↓
Context assembly
↓
LLM answer generation
↓
Citation validation
↓
Final response
```

---

## 9. Quality Requirements

### Retrieval

Target utama adalah **relevance**, bukan sekadar mengambil banyak chunk.

Evaluasi retrieval minimal dilakukan menggunakan kumpulan pertanyaan manual yang mencakup:

- pertanyaan exact;
- pertanyaan paraphrase;
- pertanyaan berbasis Pasal;
- pertanyaan ambigu;
- pertanyaan yang membutuhkan lebih dari satu chunk;
- pertanyaan yang tidak memiliki jawaban.

### Generation

Jawaban dinilai dari:

- correctness;
- groundedness;
- relevance;
- citation correctness;
- kemampuan menyatakan keterbatasan.

---

## 10. Non-Functional Requirements

### NFR-01 — Reproducibility

Project harus dapat dijalankan dari repository dengan setup minimal.

Target:

```bash
docker compose up
```

atau langkah yang setara dan terdokumentasi dengan jelas.

### NFR-02 — Configuration

Secret dan konfigurasi tidak boleh hardcoded.

Gunakan:

```text
.env
.env.example
```

### NFR-03 — Observability

Minimal log:

```text
request_id
query
retrieval latency
generation latency
retrieved chunk ids
errors
```

### NFR-04 — Maintainability

Pisahkan minimal:

```text
ingestion
retrieval
reranking
generation
api
config
```

### NFR-05 — Failure Handling

Sistem harus menangani:

- PDF extraction gagal;
- OCR gagal;
- embedding service gagal;
- vector database unavailable;
- LLM unavailable;
- empty retrieval result.

---

## 11. Suggested Repository Structure

```text
.
├── app/
│   ├── api/
│   ├── ingestion/
│   ├── retrieval/
│   ├── reranking/
│   ├── generation/
│   ├── models/
│   └── config/
│
├── data/
│   └── raw/
│
├── scripts/
│   └── ingest.py
│
├── tests/
│   ├── unit/
│   └── evaluation/
│
├── docs/
│   └── architecture.md
│
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── README.md
└── pyproject.toml
```

---

## 12. Acceptance Criteria

Project dianggap selesai ketika:

- [ ] Kedua PDF dapat diproses dari raw document.
- [ ] Native PDF dan scanned PDF memiliki handling yang jelas.
- [ ] Dokumen berhasil di-chunk dan di-index.
- [ ] Metadata halaman tetap tersedia setelah indexing.
- [ ] Dense retrieval berjalan.
- [ ] Sparse retrieval berjalan.
- [ ] Hybrid dense + sparse retrieval berjalan.
- [ ] Reranking berjalan.
- [ ] REST API dapat menerima query.
- [ ] Jawaban dihasilkan dalam Bahasa Indonesia.
- [ ] Jawaban menyertakan nama dokumen dan nomor halaman.
- [ ] Sistem tidak menjawab secara yakin ketika evidence tidak cukup.
- [ ] README menjelaskan architecture, decisions, trade-offs, limitations, next improvements, dan cara menjalankan sistem.
- [ ] Architecture diagram tersedia.
- [ ] Repository dapat dijalankan evaluator dengan setup minimal.

---

## 13. Required Deliverables

### 1. Public GitHub Repository

Berisi source code dan seluruh file yang dibutuhkan untuk menjalankan sistem.

### 2. README

README wajib menjelaskan:

- system architecture;
- design decisions;
- trade-offs;
- limitations;
- next improvements;
- cara menjalankan project;
- cara menggunakan query interface;
- penggunaan agentic coding tools jika digunakan.

### 3. Architecture Diagram

Format bebas, misalnya Mermaid atau draw.io.

Diagram harus sesuai dengan implementasi aktual.

---

## 14. Definition of Done

Sistem dapat dijalankan oleh evaluator, menerima pertanyaan terhadap dua regulasi yang diberikan, mengambil evidence yang relevan, menghasilkan jawaban Bahasa Indonesia yang grounded, dan memberikan citation yang dapat diverifikasi hingga minimal nama dokumen dan halaman.
