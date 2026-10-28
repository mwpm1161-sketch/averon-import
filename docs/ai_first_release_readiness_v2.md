# AI-first pilot release package and rollback checklist

This document is an operator-run procedure, not authorization to change
production. No VPS was accessed and no release, migration, service restart, or
data backup was performed by this task. Values shown below came from the
project's supplied deployment context and must be re-verified on the VPS.

## Required release inputs

- Reviewed commit SHA: `<REVIEWED_SHA>` (never use a moving branch name).
- Current deployed SHA and code path: confirm on host; historical values are
  not evidence of current state.
- Service/runtime/data paths: confirm using `systemctl show` and service
  configuration metadata. Never `cat`, source, or print `/etc/averon/averon.env`.
- Confirmed service account, authentication mode, free disk space, SQLite
  database locations, user-data inventory, AI-first workspace inventory, and
  tested rollback target.
- A maintenance window and explicit approval for the service switch.

Do not proceed if the working tree/release artifact is not tied to the reviewed
SHA, a verified backup cannot be produced, disk space is insufficient, or the
data compatibility review is incomplete.

## Read-only preflight

Run as an authorized operator. These commands do not read secret values:

```sh
sudo systemctl show averon.service --property=ActiveState --property=SubState \
  --property=User --property=WorkingDirectory --property=ExecStart --no-pager
sudo systemctl show averon.service --property=EnvironmentFiles --no-pager
sudo systemctl status averon.service --no-pager
df -h /opt/averon /var/lib/averon /root/averon-backups
git -C /opt/averon/pilot-stage rev-parse HEAD
git -C /opt/averon/pilot-stage status --short
curl --fail --silent --show-error http://127.0.0.1:8765/health
```

Check Caddy's loaded configuration and service state without exposing its
environment or credentials:

```sh
sudo systemctl is-active caddy
sudo caddy validate --config /etc/caddy/Caddyfile
```

Use the read-only workspace inventory instructions in
`ai_first_workspace_inventory.md`. Record only the aggregate results required
for the release decision. Do not print document contents or identity values.

## Consistent backup

1. Record the exact current code SHA and service configuration metadata.
2. Ensure adequate space and create a private, timestamped backup directory
   owned by the authorized operator. Do not change permissions on user data.
3. For every SQLite database, use SQLite's online backup API/CLI while the
   service is running (or stop the service only under an approved maintenance
   plan). Do **not** copy an active database file with `cp`/`tar` and assume it
   is consistent. Example, after confirming the actual database path:

   ```sh
   sqlite3 /var/lib/averon/<database>.sqlite \
     ".backup '/root/averon-backups/<timestamp>/<database>.sqlite'"
   sqlite3 /root/averon-backups/<timestamp>/<database>.sqlite 'PRAGMA integrity_check;'
   ```

4. Archive file-based document/workspace data using a filesystem snapshot or
   quiesced service so files and SQLite state represent one consistent point.
   Keep the archive private and outside the application checkout.
5. Verify archive listing, checksums, SQLite integrity, and a restore into a
   separate staging directory. Do not restore over live data during this check.
6. Record backup path and checksum in the release record without recording
   document names or secret configuration.

## Staging and validation

1. Fetch the authorized repository source and verify the exact approved SHA and
   signed/expected commit ancestry. Do not reset the live checkout.
2. Create a separate staging checkout at `<REVIEWED_SHA>` and install/verify
   dependencies using the same Python 3.12 runtime, without changing the live
   virtual environment.
3. Run the approved Python and JavaScript test profiles and AI-first API smoke
   against a synthetic private staging data root. Keep fake OCR enabled only
   there; keep live Qwen disabled.
4. Verify staging HTTP/auth, a synthetic upload, draft save/reload, page
   completeness guard, partial pilot XLSX, formula safety, and legacy routes.
5. Review schema changes. No migration is permitted unless the migration,
   backup/restore path, compatibility behavior, and rollback data plan are
   separately approved and tested.

## Controlled service switch (owner action only)

No service switch was performed. After a separate release approval, the owner
may use the project's established deployment procedure to point the service
at the reviewed release. Before switching, ensure a code rollback and a
compatible data restore point are both available. Do not run `git reset
--hard`, delete data, alter ACLs, or edit `.env` as part of an ad hoc release.

After switching, check local health, login/session behavior, legacy Yandex
Vision, saved documents, Human Review, XLSX, and Caddy routing. Do not make a
paid Qwen request as a smoke test. If Qwen has not been tested on the deployed
release with an explicitly authorized request, report it as unverified.

## Rollback

1. Stop the rollout if any required post-switch check fails.
2. Restore the exact previous code revision using the established release
   mechanism; do not force-update Git history.
3. If the new version changed data format, restore the matching verified
   snapshot only after confirming that newer user edits will not be lost.
   Preserve the failed-release data separately for investigation.
4. Restart the service, confirm previous SHA, service health, authentication,
   Caddy, and read-only access to existing documents.
5. Record only safe error codes and aggregate results.

Do not roll back by restoring an old database blindly over newer user work.
