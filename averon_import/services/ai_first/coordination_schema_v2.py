"""B3.1 registered schemas. No queue, scheduler or reconciliation implementation."""
from .coordination_contracts import CoordinationError
from .coordination_schema import SCHEMA_SQL

V1_META_SQL = "CREATE TABLE meta (" + SCHEMA_SQL.split("CREATE TABLE meta (", 1)[1].split(") STRICT;", 1)[0] + ") STRICT;"
V2_META_SQL = V1_META_SQL.replace("schema_version=1", "schema_version=2")
V1_CONTRACT_SQL = "CREATE VIEW schema_contract" + SCHEMA_SQL.split("CREATE VIEW schema_contract", 1)[1].split(";", 1)[0] + ";"
V2_CONTRACT_SQL = """CREATE VIEW schema_contract AS SELECT
 2 AS schema_version, 3 AS reconciliation_min_schema_version,
 'sealed_history' AS history_policy,
 'explicit_transactional_migration' AS reconciliation_upgrade;"""

# Only financial preimages belong here. Execution prompts/bodies/OCR are absent.
V2_ADDITIONS_SQL = """
CREATE VIEW schema_capabilities AS SELECT
 1 AS financial_model_version, 1 AS durable_jobs, 0 AS b6_reconciliation;
CREATE TABLE durable_job_snapshots (
 snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id)=32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
 run_id TEXT NOT NULL UNIQUE CHECK(length(run_id)=32 AND run_id NOT GLOB '*[^0-9a-f]*'),
 format_version INTEGER NOT NULL CHECK(format_version=1),
 binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL CHECK(binding_revision>0),
 run_json TEXT NOT NULL CHECK(json_valid(run_json) AND json_type(run_json)='object'
  AND length(CAST(run_json AS BLOB))<=262144
  AND json_extract(run_json,'$.run_id') IS run_id
  AND json_remove(run_json,'$.run_id','$.tenant_id','$.document_id','$.generation',
   '$.source_digest','$.config_digest','$.budget_units','$.request_cap')='{}'),
 config_json TEXT NOT NULL CHECK(json_valid(config_json) AND json_type(config_json)='object'
  AND length(CAST(config_json AS BLOB))<=262144
  AND json_type(config_json,'$.pricing') IS 'object'
  AND json_remove(config_json,'$.concurrency','$.dpi','$.max_requests','$.requests_per_minute',
   '$.tokens_per_minute','$.max_budget_rub','$.pricing','$.max_image_bytes','$.max_image_pixels',
   '$.max_pages','$.page_regions')='{}'
  AND json_remove(json_extract(config_json,'$.pricing'),'$.profile_id','$.input_rub_per_1k',
   '$.cached_input_rub_per_1k','$.output_rub_per_1k')='{}'),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND json_type(binding_json)='object'
  AND length(CAST(binding_json AS BLOB))<=262144
  AND json_extract(binding_json,'$.binding_id') IS binding_id
  AND json_extract(binding_json,'$.revision') IS binding_revision
  AND json_remove(binding_json,'$.binding_id','$.revision','$.credential_generation','$.credential_tag',
   '$.billing_scope_id','$.quota_scope_ids','$.target_digest')='{}'),
 pricing_digest TEXT NOT NULL CHECK(length(pricing_digest)=64 AND pricing_digest NOT GLOB '*[^0-9a-f]*'),
 snapshot_digest TEXT NOT NULL CHECK(length(snapshot_digest)=64 AND snapshot_digest NOT GLOB '*[^0-9a-f]*'),
 UNIQUE(snapshot_id,run_id),
 FOREIGN KEY(binding_id,binding_revision) REFERENCES credential_bindings(binding_id,revision)
) STRICT;
CREATE TABLE durable_jobs (
 job_id TEXT PRIMARY KEY CHECK(length(job_id)=35 AND substr(job_id,1,3)='dj_'
  AND substr(job_id,4) NOT GLOB '*[^0-9a-f]*'),
 tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 96),
 owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 128),
 document_id TEXT NOT NULL CHECK(length(document_id) BETWEEN 1 AND 96),
 run_id TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL CHECK(generation>0),
 snapshot_id TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64 AND request_digest NOT GLOB '*[^0-9a-f]*'),
 execution_id TEXT NOT NULL CHECK(length(execution_id)=64 AND execution_id NOT GLOB '*[^0-9a-f]*'),
 execution_digest TEXT NOT NULL CHECK(length(execution_digest)=64 AND execution_digest NOT GLOB '*[^0-9a-f]*'),
 execution_path_version INTEGER NOT NULL CHECK(execution_path_version=1),
 execution_bytes INTEGER NOT NULL CHECK(execution_bytes BETWEEN 1 AND 262144),
 execution_state TEXT NOT NULL CHECK(execution_state IN ('pending','ready','blocked','archived')),
 state TEXT NOT NULL CHECK(state IN ('preparing_input','queued','running','cancelling','paused_restart',
  'blocked','completed','completed_with_errors','cancelled','failed')),
 revision INTEGER NOT NULL CHECK(revision>0), cancel_revision INTEGER NOT NULL CHECK(cancel_revision>=0),
 authorized_owner_epoch INTEGER CHECK(authorized_owner_epoch>0),
 last_document_turn INTEGER NOT NULL CHECK(last_document_turn>=0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(tenant_id,document_id,generation), UNIQUE(job_id,run_id),
 CHECK(state NOT IN ('queued','running') OR execution_state='ready'),
 FOREIGN KEY(snapshot_id,run_id) REFERENCES durable_job_snapshots(snapshot_id,run_id)
) STRICT;
CREATE UNIQUE INDEX durable_active_document ON durable_jobs(tenant_id,document_id)
 WHERE state IN ('preparing_input','queued','running','cancelling','paused_restart','blocked');
CREATE INDEX durable_owner_status ON durable_jobs(tenant_id,owner_id,state,job_id);
CREATE TABLE durable_job_pages (
 job_id TEXT NOT NULL, run_id TEXT NOT NULL, page_number INTEGER NOT NULL CHECK(page_number>0),
 candidate_attempt_id TEXT NOT NULL UNIQUE CHECK(length(candidate_attempt_id)=32
  AND candidate_attempt_id NOT GLOB '*[^0-9a-f]*'),
 attempt_id TEXT UNIQUE REFERENCES attempts(attempt_id),
 state TEXT NOT NULL CHECK(state IN ('queued','claimed','quota_wait','intent','send_committed',
  'settlement_pending','completed','incomplete','failed','cancelled','unknown_outcome','blocked')),
 claim_seq INTEGER NOT NULL CHECK(claim_seq>=0), owner_epoch INTEGER CHECK(owner_epoch>0),
 revision INTEGER NOT NULL CHECK(revision>0),
 evidence_digest TEXT CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
 result_bytes INTEGER NOT NULL CHECK(result_bytes BETWEEN 0 AND 16000000),
 safe_code TEXT CHECK(length(safe_code) BETWEEN 1 AND 64 AND safe_code NOT GLOB '*[^a-z0-9_]*'),
 PRIMARY KEY(job_id,page_number),
 CHECK(attempt_id IS NULL OR attempt_id=candidate_attempt_id),
 CHECK(state NOT IN ('claimed','intent','send_committed','settlement_pending')
  OR (owner_epoch IS NOT NULL AND claim_seq>0)),
 CHECK(state NOT IN ('intent','send_committed','settlement_pending') OR attempt_id IS NOT NULL),
 FOREIGN KEY(job_id,run_id) REFERENCES durable_jobs(job_id,run_id)
) STRICT;
CREATE INDEX durable_ready_pages ON durable_job_pages(state,job_id,page_number);
CREATE TABLE durable_job_commands (
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 operation_id TEXT NOT NULL CHECK(length(operation_id)=32 AND operation_id NOT GLOB '*[^0-9a-f]*'),
 kind TEXT NOT NULL CHECK(kind IN ('submit','submit_begin','submit_ready','cancel','resume')),
 job_id TEXT NOT NULL REFERENCES durable_jobs(job_id),
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64 AND request_digest NOT GLOB '*[^0-9a-f]*'),
 receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND json_type(receipt_json)='object'
  AND length(CAST(receipt_json AS BLOB))<=16384),
 PRIMARY KEY(tenant_id,owner_id,operation_id)
) STRICT;
CREATE TABLE durable_scheduler_users (
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 last_turn INTEGER NOT NULL CHECK(last_turn>=0), PRIMARY KEY(tenant_id,owner_id)
) STRICT;
CREATE TABLE durable_scheduler_state (
 id INTEGER PRIMARY KEY CHECK(id=1), turn_seq INTEGER NOT NULL CHECK(turn_seq>=0)
) STRICT;
CREATE TABLE schema_migrations (
 migration_id TEXT PRIMARY KEY CHECK(length(migration_id)=32 AND migration_id NOT GLOB '*[^0-9a-f]*'),
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
 receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND json_type(receipt_json)='object'
  AND length(CAST(receipt_json AS BLOB))<=16384),
 receipt_digest TEXT NOT NULL CHECK(length(receipt_digest)=64)
) STRICT;
"""

for _table in ("durable_job_snapshots", "durable_job_commands", "schema_migrations"):
    V2_ADDITIONS_SQL += f"""
CREATE TRIGGER {_table}_no_update BEFORE UPDATE ON {_table} BEGIN
 SELECT RAISE(ABORT,'immutable_b3_history'); END;
CREATE TRIGGER {_table}_no_delete BEFORE DELETE ON {_table} BEGIN
 SELECT RAISE(ABORT,'immutable_b3_history'); END;
"""
V2_ADDITIONS_SQL += """
CREATE TRIGGER durable_jobs_identity BEFORE UPDATE ON durable_jobs
 WHEN NEW.job_id IS NOT OLD.job_id OR NEW.tenant_id IS NOT OLD.tenant_id
 OR NEW.owner_id IS NOT OLD.owner_id OR NEW.document_id IS NOT OLD.document_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.generation IS NOT OLD.generation
 OR NEW.snapshot_id IS NOT OLD.snapshot_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.execution_id IS NOT OLD.execution_id OR NEW.execution_digest IS NOT OLD.execution_digest
 OR NEW.execution_path_version IS NOT OLD.execution_path_version
 OR NEW.execution_bytes IS NOT OLD.execution_bytes OR NEW.created_at IS NOT OLD.created_at
 BEGIN SELECT RAISE(ABORT,'immutable_job_identity'); END;
CREATE TRIGGER durable_jobs_no_delete BEFORE DELETE ON durable_jobs BEGIN
 SELECT RAISE(ABORT,'immutable_job_history'); END;
CREATE TRIGGER durable_page_identity BEFORE UPDATE ON durable_job_pages
 WHEN NEW.job_id IS NOT OLD.job_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.page_number IS NOT OLD.page_number OR NEW.candidate_attempt_id IS NOT OLD.candidate_attempt_id
 OR (OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS NOT OLD.attempt_id)
 OR NEW.claim_seq<OLD.claim_seq OR NEW.revision<=OLD.revision
 BEGIN SELECT RAISE(ABORT,'immutable_page_identity'); END;
CREATE TRIGGER durable_pages_no_delete BEFORE DELETE ON durable_job_pages BEGIN
 SELECT RAISE(ABORT,'immutable_page_history'); END;
"""

SCHEMA_V2_SQL = SCHEMA_SQL.replace(V1_CONTRACT_SQL, V2_CONTRACT_SQL, 1).replace(
    V1_META_SQL, V2_META_SQL, 1) + V2_ADDITIONS_SQL


def schema_sql(version):
    if type(version) is not int or version not in (1, 2):
        raise CoordinationError("unsupported_schema_version")
    return SCHEMA_SQL if version == 1 else SCHEMA_V2_SQL


def capabilities(version):
    schema_sql(version)
    # Explicit allowlist; neither version arithmetic nor a DB flag grants B6.
    return frozenset({"financial_core_v1"} if version == 1 else {"financial_core_v1", "durable_jobs_v1"})
