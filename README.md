# insignia-labor-law-rag

## Backend ingestion

Backend menerima PDF melalui endpoint upload, langsung mengembalikan `job_id`, lalu menjalankan parsing, embedding, dan indexing di background. Progress tiap dokumen dikirim melalui SSE, sehingga frontend dapat tetap digunakan selama proses berjalan.

Salin konfigurasi dan isi kredensialnya:

```bash
cd backend
cp .env.example .env
uv sync
uv run uvicorn src.main:app --reload
```

Pastikan `QDRANT_HOST` menunjuk ke REST API Qdrant. Port REST default adalah `6333`; port `6334` adalah gRPC.

Upload satu atau beberapa PDF:

```bash
curl -X POST http://localhost:8000/documents/upload \
  -F 'files=@/path/to/peraturan.pdf'
```

Pantau progress job melalui SSE:

```bash
curl -N http://localhost:8000/documents/<job_id>/events
```

Event `document_status` memakai stage yang sama dengan log console frontend dan log backend: `upload`, `parsing`, `embedding`, `indexing`, `completed`, atau `failed`.

Setiap point Qdrant berisi:

- `dense`: vector semantic 1024 dimensi BGE-M3;
- `sparse`: coordinate vector `{indices, values}` untuk lexical matching;
- payload: `filename`, `bab`, `pasal`, `ayat`, `part`, `page_start`, `page_end`, `pages`, `text`, `chunk_id`, dan metadata ingestion.

Batch diatur oleh `EMBEDDING_BATCH_SIZE` agar respons sparse BGE-M3 tidak membebani memori.

## Query user

Setelah dokumen selesai di-index, ajukan pertanyaan melalui pipeline hybrid:
normalisasi/rewrite query pendek, dense + sparse search, RRF fusion, rerank top 7,
parent context expansion, lalu generative answer dengan citation.

```bash
curl -X POST http://localhost:8000/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"Berapa lama maksimal PKWT?"}'
```

Response memiliki `answer`, `sources` (nama dokumen, halaman, Pasal/Ayat, dan
`chunk_id`), serta metadata `retrieval`. Model reranker dan generatif dibaca dari
`RERANKED_MODEL` dan `GENERATIVE_MODEL` di `.env` melalui DeepInfra.
