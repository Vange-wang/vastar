"""schema 定义；迁移模块的唯一 DDL 来源（B16：schema 3 增 artifacts 元数据表）。"""

SCHEMA_VERSION = 11

ARTIFACTS_DDL = """CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  scope TEXT NOT NULL,
  uri TEXT NOT NULL,
  hash TEXT NOT NULL,
  size INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_hash TEXT,
  owner TEXT NOT NULL,
  retention TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (artifact_id, version)
);"""


MEMORY_REVISIONS_DDL = """CREATE TABLE IF NOT EXISTS memory_revisions (
  revision INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value TEXT,
  operation TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL,
  origin TEXT NOT NULL,
  source_message_id TEXT,
  previous_revision INTEGER,
  created_at TEXT NOT NULL
);"""


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'queue',
  config_version TEXT,
  agent_version TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  submit_seq INTEGER,
  run_id TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  reasoning_content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'complete',
  origin TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  input_message_id TEXT NOT NULL,
  state TEXT NOT NULL,
  terminal_state TEXT,
  finish_reason TEXT,
  retry_of_run_id TEXT,
  config_snapshot TEXT,
  result_json TEXT,
  durability TEXT,
  latencies_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  attempt_index INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  request_params TEXT NOT NULL,
  request_hash TEXT,
  usage_json TEXT,
  error_json TEXT,
  response_frames TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  request_epoch INTEGER NOT NULL,
  type TEXT NOT NULL,
  channel TEXT NOT NULL,
  producer TEXT NOT NULL,
  origin TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  monotonic_ns INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
{MEMORY_REVISIONS_DDL}
{ARTIFACTS_DDL}
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""


