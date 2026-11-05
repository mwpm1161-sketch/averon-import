"""Sealed v1 accounting; B6 requires an explicit history-preserving v2 migration."""
APPLICATION_ID = 0x41564231
SCHEMA_VERSION = 1
RECONCILIATION_MIN_SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE VIEW schema_contract AS SELECT
 1 AS schema_version, 2 AS reconciliation_min_schema_version,
 'sealed_history' AS history_policy,
 'explicit_transactional_migration' AS reconciliation_upgrade;
CREATE TABLE meta (
 id INTEGER PRIMARY KEY CHECK(id=1), installation_id TEXT NOT NULL,
 bootstrap_authorization TEXT NOT NULL, history_manifest_digest TEXT NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 epoch INTEGER NOT NULL CHECK(epoch>=0), owner_nonce TEXT,
 boot_id TEXT NOT NULL, boot_started_ms INTEGER NOT NULL CHECK(boot_started_ms>=0),
 high_water_ms INTEGER NOT NULL CHECK(high_water_ms>=0),
 lease_until_ms INTEGER NOT NULL CHECK(lease_until_ms>=0),
 global_concurrency INTEGER NOT NULL CHECK(global_concurrency BETWEEN 1 AND 10)
) STRICT;
CREATE TABLE billing_scopes (
 scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
 ceiling_units INTEGER NOT NULL CHECK(ceiling_units>=0),
 opening_spent_units INTEGER NOT NULL CHECK(opening_spent_units>=0),
 opening_held_units INTEGER NOT NULL CHECK(opening_held_units>=0),
 history_complete INTEGER NOT NULL CHECK(history_complete IN (0,1)),
 evidence_digest TEXT NOT NULL, blocked INTEGER NOT NULL CHECK(blocked IN (0,1))
) STRICT;
CREATE TABLE quota_scopes (
 scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
 requests INTEGER NOT NULL CHECK(requests>0), tokens INTEGER NOT NULL CHECK(tokens>0),
 concurrency INTEGER NOT NULL CHECK(concurrency>0), window_ms INTEGER NOT NULL CHECK(window_ms>0),
 opening_requests INTEGER NOT NULL CHECK(opening_requests>=0),
 opening_tokens INTEGER NOT NULL CHECK(opening_tokens>=0),
 opening_inflight INTEGER NOT NULL CHECK(opening_inflight>=0),
 opening_boot_id TEXT NOT NULL, opening_ended_ms INTEGER NOT NULL CHECK(opening_ended_ms>=0),
 opening_window_ms INTEGER NOT NULL CHECK(opening_window_ms>0),
 history_complete INTEGER NOT NULL CHECK(history_complete IN (0,1)),
 evidence_digest TEXT NOT NULL, blocked INTEGER NOT NULL CHECK(blocked IN (0,1))
) STRICT;
CREATE TABLE credential_bindings (
 binding_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 billing_scope_id TEXT NOT NULL REFERENCES billing_scopes(scope_id),
 body_json TEXT NOT NULL, digest TEXT NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)), PRIMARY KEY(binding_id,revision)
) STRICT;
CREATE UNIQUE INDEX active_binding ON credential_bindings(binding_id) WHERE active=1;
CREATE TABLE binding_quotas (
 binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL,
 quota_scope_id TEXT NOT NULL REFERENCES quota_scopes(scope_id),
 PRIMARY KEY(binding_id,binding_revision,quota_scope_id),
 FOREIGN KEY(binding_id,binding_revision) REFERENCES credential_bindings(binding_id,revision)
) STRICT;
CREATE TABLE documents (
 tenant_id TEXT NOT NULL, document_id TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0), active_run_id TEXT NOT NULL,
 PRIMARY KEY(tenant_id,document_id)
) STRICT;
CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, document_id TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),
 budget_units INTEGER NOT NULL CHECK(budget_units>=0),
 request_cap INTEGER NOT NULL CHECK(request_cap>0), body_json TEXT NOT NULL,
 UNIQUE(tenant_id,document_id,generation),
 FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,document_id)
) STRICT;
CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 page_number INTEGER NOT NULL CHECK(page_number>0),
 attempt_no INTEGER NOT NULL CHECK(attempt_no>0), claim_seq INTEGER NOT NULL CHECK(claim_seq>0),
 owner_epoch INTEGER NOT NULL CHECK(owner_epoch>0),
 binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL,
 billing_scope_id TEXT NOT NULL REFERENCES billing_scopes(scope_id),
 billing_revision INTEGER NOT NULL CHECK(billing_revision>0),
 request_digest TEXT NOT NULL, body_json TEXT NOT NULL,
 input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
 output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
 reserve_units INTEGER NOT NULL CHECK(reserve_units>0),
 actual_units INTEGER CHECK(actual_units>=0),
 state TEXT NOT NULL CHECK(state IN ('intent','unknown','terminal','not_sent')),
 evidence_digest TEXT, ended_boot_id TEXT, ended_ms INTEGER CHECK(ended_ms>=0),
 FOREIGN KEY(binding_id,binding_revision) REFERENCES credential_bindings(binding_id,revision),
 UNIQUE(run_id,page_number,attempt_no), UNIQUE(run_id,page_number,claim_seq),
 CHECK((state IN ('intent','unknown') AND ended_ms IS NULL AND ended_boot_id IS NULL)
    OR (state IN ('terminal','not_sent') AND ended_ms IS NOT NULL AND ended_boot_id IS NOT NULL)),
 CHECK(actual_units IS NULL OR state='terminal'),
 CHECK(state NOT IN ('terminal','not_sent') OR evidence_digest IS NOT NULL)
) STRICT;
CREATE INDEX billing_attempts ON attempts(billing_scope_id,state,actual_units,reserve_units);
CREATE INDEX run_attempts ON attempts(run_id,state,actual_units,reserve_units);
CREATE INDEX attempt_quota_state ON attempts(attempt_id,state,ended_boot_id,ended_ms);
CREATE TABLE attempt_quota_reservations (
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 quota_scope_id TEXT NOT NULL REFERENCES quota_scopes(scope_id),
 policy_revision INTEGER NOT NULL CHECK(policy_revision>0),
 window_ms INTEGER NOT NULL CHECK(window_ms>0),
 reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>=0), policy_json TEXT NOT NULL,
 PRIMARY KEY(attempt_id,quota_scope_id)
) STRICT;
CREATE INDEX quota_attempts ON attempt_quota_reservations(quota_scope_id,attempt_id,window_ms,reserved_tokens);
CREATE TABLE operations (
 operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_digest TEXT NOT NULL, request_json TEXT NOT NULL,
 result_json TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>=0),
 boot_id TEXT NOT NULL, created_ms INTEGER NOT NULL CHECK(created_ms>=0)
) STRICT;
CREATE TRIGGER operations_no_update BEFORE UPDATE ON operations BEGIN
 SELECT RAISE(ABORT,'immutable_operation'); END;
CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations BEGIN
 SELECT RAISE(ABORT,'immutable_operation'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts BEGIN
 SELECT RAISE(ABORT,'immutable_attempt'); END;
CREATE TRIGGER attempts_identity BEFORE UPDATE ON attempts
 WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.operation_id IS NOT OLD.operation_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.page_number IS NOT OLD.page_number
 OR NEW.attempt_no IS NOT OLD.attempt_no OR NEW.claim_seq IS NOT OLD.claim_seq
 OR NEW.owner_epoch IS NOT OLD.owner_epoch OR NEW.binding_id IS NOT OLD.binding_id
 OR NEW.binding_revision IS NOT OLD.binding_revision OR NEW.billing_scope_id IS NOT OLD.billing_scope_id
 OR NEW.billing_revision IS NOT OLD.billing_revision OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.body_json IS NOT OLD.body_json OR NEW.input_tokens IS NOT OLD.input_tokens
 OR NEW.output_tokens IS NOT OLD.output_tokens OR NEW.reserve_units IS NOT OLD.reserve_units
 BEGIN SELECT RAISE(ABORT,'immutable_attempt_identity'); END;
CREATE TRIGGER attempts_terminal BEFORE UPDATE ON attempts
 WHEN OLD.state IN ('terminal','not_sent') OR (OLD.state='unknown' AND NEW.state='not_sent')
 BEGIN SELECT RAISE(ABORT,'immutable_terminal'); END;
CREATE TRIGGER binding_immutable BEFORE UPDATE ON credential_bindings
 WHEN NEW.binding_id IS NOT OLD.binding_id OR NEW.revision IS NOT OLD.revision
 OR NEW.billing_scope_id IS NOT OLD.billing_scope_id OR NEW.body_json IS NOT OLD.body_json
 OR NEW.digest IS NOT OLD.digest
 BEGIN SELECT RAISE(ABORT,'immutable_binding'); END;
CREATE TRIGGER binding_no_delete BEFORE DELETE ON credential_bindings BEGIN
 SELECT RAISE(ABORT,'immutable_binding'); END;
CREATE TRIGGER binding_quota_no_update BEFORE UPDATE ON binding_quotas BEGIN
 SELECT RAISE(ABORT,'immutable_binding_quota'); END;
CREATE TRIGGER binding_quota_no_delete BEFORE DELETE ON binding_quotas BEGIN
 SELECT RAISE(ABORT,'immutable_binding_quota'); END;
CREATE TRIGGER reservation_no_update BEFORE UPDATE ON attempt_quota_reservations BEGIN
 SELECT RAISE(ABORT,'immutable_reservation'); END;
CREATE TRIGGER reservation_no_delete BEFORE DELETE ON attempt_quota_reservations BEGIN
 SELECT RAISE(ABORT,'immutable_reservation'); END;
CREATE TRIGGER billing_opening_immutable BEFORE UPDATE ON billing_scopes
 WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.opening_spent_units IS NOT OLD.opening_spent_units
 OR NEW.opening_held_units IS NOT OLD.opening_held_units OR NEW.history_complete IS NOT OLD.history_complete
 OR NEW.evidence_digest IS NOT OLD.evidence_digest OR (OLD.blocked=1 AND NEW.blocked=0)
 BEGIN SELECT RAISE(ABORT,'immutable_opening_balance'); END;
CREATE TRIGGER billing_no_delete BEFORE DELETE ON billing_scopes BEGIN
 SELECT RAISE(ABORT,'immutable_billing_scope'); END;
CREATE TRIGGER quota_opening_immutable BEFORE UPDATE ON quota_scopes
 WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.opening_requests IS NOT OLD.opening_requests
 OR NEW.opening_tokens IS NOT OLD.opening_tokens OR NEW.opening_inflight IS NOT OLD.opening_inflight
 OR NEW.opening_boot_id IS NOT OLD.opening_boot_id OR NEW.opening_ended_ms IS NOT OLD.opening_ended_ms
 OR NEW.opening_window_ms IS NOT OLD.opening_window_ms OR NEW.history_complete IS NOT OLD.history_complete
 OR NEW.evidence_digest IS NOT OLD.evidence_digest OR (OLD.blocked=1 AND NEW.blocked=0)
 BEGIN SELECT RAISE(ABORT,'immutable_opening_quota'); END;
CREATE TRIGGER quota_no_delete BEFORE DELETE ON quota_scopes BEGIN
 SELECT RAISE(ABORT,'immutable_quota_scope'); END;
"""
