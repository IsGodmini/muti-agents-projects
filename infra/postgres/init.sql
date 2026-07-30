CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS travel_resources (
  id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  location GEOGRAPHY(POINT, 4326),
  attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_uri TEXT,
  valid_until TIMESTAMPTZ,
  embedding VECTOR(1024),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS travel_resources_location_idx
  ON travel_resources USING GIST (location);

CREATE INDEX IF NOT EXISTS travel_resources_embedding_idx
  ON travel_resources USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS plans (
  id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  request JSONB NOT NULL,
  current_stage TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan_versions (
  id UUID PRIMARY KEY,
  plan_id UUID NOT NULL REFERENCES plans(id),
  version INTEGER NOT NULL,
  snapshot JSONB NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(plan_id, version)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id UUID PRIMARY KEY,
  plan_id UUID NOT NULL REFERENCES plans(id),
  thread_id TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  model_name TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER,
  input_summary JSONB,
  output_summary JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tool_invocations (
  id UUID PRIMARY KEY,
  agent_run_id UUID REFERENCES agent_runs(id),
  tool_name TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  input_payload JSONB NOT NULL,
  output_payload JSONB,
  status TEXT NOT NULL,
  latency_ms INTEGER,
  idempotency_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
