-- Staging RAG schema for future PostgreSQL/pgvector ingestion.
-- Runtime MVP does not depend on these tables yet.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_documents (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetch_url TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_content_type TEXT,
    source_file TEXT,
    source_row INTEGER,
    content_hash TEXT NOT NULL,
    text_chars INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    skipped_price_chunks INTEGER NOT NULL DEFAULT 0,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS rag_document_chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    text_chars INTEGER NOT NULL DEFAULT 0,
    embedding vector(1536),
    embedding_model TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rag_documents_company_id
    ON rag_documents (company_id);

CREATE INDEX IF NOT EXISTS idx_rag_documents_content_hash
    ON rag_documents (content_hash);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_company_id
    ON rag_document_chunks (company_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_document_id
    ON rag_document_chunks (document_id);

-- Create this only after embeddings are populated and vector dimensions match.
-- CREATE INDEX idx_rag_chunks_embedding_cosine
--     ON rag_document_chunks
--     USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);
