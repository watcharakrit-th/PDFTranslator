-- pdftr schema v1. Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          serial PRIMARY KEY,
    file_hash   text UNIQUE NOT NULL,          -- sha256 of the PDF bytes
    file_name   text NOT NULL,
    file_path   text NOT NULL,
    title       text,
    page_count  int  NOT NULL,
    stage       text NOT NULL DEFAULT 'r1_chunks_en',
    meta        jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id      int  NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     int  NOT NULL,                 -- 1-based PDF page index
    label       text,                          -- printed page number ('xv', '301')
    kind        text NOT NULL DEFAULT 'body',  -- body|cover|copyright|toc|index|bibliography|blank
    width       real NOT NULL,
    height      real NOT NULL,
    flags       text[] NOT NULL DEFAULT '{}',
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, page_no)
);

-- A block is one piece of text on one page (the unit we redraw in Thai).
CREATE TABLE IF NOT EXISTS blocks (
    id            bigserial PRIMARY KEY,
    doc_id        int  NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no       int  NOT NULL,
    ord           int  NOT NULL,               -- page_no * 1000 + position on page
    role          text NOT NULL,               -- heading|paragraph|list_item|callout|caption|
                                               -- footnote|code|table|picture|formula|toc|header|footer|other
    docling_label text,
    level         int,                         -- heading level or list nesting depth
    x0 real NOT NULL, y0 real NOT NULL, x1 real NOT NULL, y1 real NOT NULL,  -- points, top-left origin
    markup_en     text NOT NULL DEFAULT '',    -- text with <c> code, <k> key term, <b> bold tags
    text_en       text NOT NULL DEFAULT '',
    markup_th     text,
    style         jsonb NOT NULL DEFAULT '{}', -- font size, color, indent, line pitch, line boxes
    translate     boolean NOT NULL DEFAULT true,
    flags         text[] NOT NULL DEFAULT '{}',
    UNIQUE (doc_id, ord)
);
CREATE INDEX IF NOT EXISTS blocks_page_idx ON blocks (doc_id, page_no);

-- A chunk is one unit for embedding; it can span blocks and pages.
CREATE TABLE IF NOT EXISTS chunks (
    id            bigserial PRIMARY KEY,
    doc_id        int  NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord           int  NOT NULL,               -- ord of its first block
    part          int  NOT NULL DEFAULT 0,     -- index when a long paragraph is split
    kind          text NOT NULL,               -- paragraph|list_item|callout|caption|footnote|code|table|question|answer
    section_path  text[] NOT NULL DEFAULT '{}',
    context_en    text NOT NULL DEFAULT '',
    text_en       text NOT NULL,
    context_th    text,
    text_th       text,
    block_ids     bigint[] NOT NULL DEFAULT '{}',
    page_start    int  NOT NULL,
    page_end      int  NOT NULL,
    list_level    int,
    embed         boolean NOT NULL DEFAULT true,
    flags         text[] NOT NULL DEFAULT '{}',
    review_en     text NOT NULL DEFAULT 'pending',  -- R1: pending|approved|rejected
    review_th     text NOT NULL DEFAULT 'pending',  -- R4
    embedding_en  vector(1024),
    embedding_th  vector(1024),
    embedded_at   timestamptz,
    UNIQUE (doc_id, ord, part)
);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id, ord);
CREATE INDEX IF NOT EXISTS chunks_emb_en_idx ON chunks USING hnsw (embedding_en vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_emb_th_idx ON chunks USING hnsw (embedding_th vector_cosine_ops);

-- Glossary shared by all documents.
CREATE TABLE IF NOT EXISTS terms (
    id          serial PRIMARY KEY,
    norm        text UNIQUE NOT NULL,           -- normalized key (lowercase, singular)
    term_en     text NOT NULL,                  -- display form
    kind        text NOT NULL DEFAULT 'term',   -- term|acronym|code
    decision    text NOT NULL DEFAULT 'undecided',  -- keep_en|translate|ignore|undecided
    term_th     text,
    source      text NOT NULL DEFAULT 'auto',   -- auto|seed|user|csv
    review_en   text NOT NULL DEFAULT 'pending',    -- R2
    review_th   text NOT NULL DEFAULT 'pending',    -- R3
    notes       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doc_terms (
    doc_id      int NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    term_id     int NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    freq        int NOT NULL,
    first_page  int,
    sources     text[] NOT NULL DEFAULT '{}',   -- styled|acronym|ngram|code
    example     text,
    PRIMARY KEY (doc_id, term_id)
);

CREATE TABLE IF NOT EXISTS translation_memory (
    id          bigserial PRIMARY KEY,
    src_hash    text NOT NULL,
    src_text    text NOT NULL,
    tgt_text    text NOT NULL,
    engine      text NOT NULL,
    glossary_rev int NOT NULL DEFAULT 0,
    origin      text NOT NULL DEFAULT 'mt',     -- mt|human
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (src_hash, engine, glossary_rev, origin)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          bigserial PRIMARY KEY,
    doc_id      int REFERENCES documents(id) ON DELETE CASCADE,
    kind        text NOT NULL,                  -- ingest|translate_terms|translate|render|embed
    status      text NOT NULL,                  -- running|done|failed
    params      jsonb NOT NULL DEFAULT '{}',
    progress    jsonb NOT NULL DEFAULT '{}',
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

-- v2 (M2): review support. "ADD COLUMN IF NOT EXISTS" keeps this file re-runnable
-- and upgrades an existing database in place.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS markup_en   text NOT NULL DEFAULT '';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE terms  ADD COLUMN IF NOT EXISTS th_source   text;          -- seed|user|csv|mt
ALTER TABLE terms  ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

-- Every review action, for auditing and (later) undo.
CREATE TABLE IF NOT EXISTS review_log (
    id      bigserial PRIMARY KEY,
    doc_id  int REFERENCES documents(id) ON DELETE CASCADE,
    step    text NOT NULL,                  -- r1|r2|r3
    action  text NOT NULL,                  -- approve|edit|skip|merge|split|kind|decision|thai|complete
    target  text,                           -- 'chunk:123', 'term:45', 'page:18'
    detail  jsonb NOT NULL DEFAULT '{}',
    at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_log_doc_idx ON review_log (doc_id, at);

-- v3 (M3): translation. Thai markup, checks and provenance per chunk and block.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS markup_th     text;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tr_flags      text[] NOT NULL DEFAULT '{}';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tr_source     text;    -- copy|tm|mt|human
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tr_key        text;    -- source + rules + engine hash
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS translated_at timestamptz;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS cells_th      jsonb;   -- table cells: {"row,col": thai}
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS tr_flags      text[] NOT NULL DEFAULT '{}';
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS tr_source     text;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS tr_key        text;

-- v4 (M4): the book's outline (PDF bookmarks), with Thai titles for the bilingual PDF.
CREATE TABLE IF NOT EXISTS outline_entries (
    id        bigserial PRIMARY KEY,
    doc_id    int NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq       int NOT NULL,                 -- order in the outline
    level     int NOT NULL,                 -- 1 = top level
    page_no   int NOT NULL,
    title_en  text NOT NULL,
    title_th  text,
    tr_source text,                         -- heading|tm|mt|human
    tr_key    text,
    tr_flags  text[] NOT NULL DEFAULT '{}',
    UNIQUE (doc_id, seq)
);
