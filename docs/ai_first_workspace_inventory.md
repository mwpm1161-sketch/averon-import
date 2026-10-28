# AI-first workspace inventory (read-only)

This tool inventories existing AI-first document workspaces without changing
them. It emits document IDs and privacy-preserving presence/count fields; it
never prints document text, uploader/reviewer IDs, absolute paths, credentials,
or OCR values. Run it only as an operator authorized to inspect the configured
data root, under the service account that normally reads the workspaces.

## Run on the VPS

First verify the deployed code path and service identity without printing the
environment file:

```sh
sudo systemctl show averon.service --property=User --property=WorkingDirectory --no-pager
sudo systemctl show averon.service --property=EnvironmentFiles --no-pager
```

Then, from the deployed application directory, invoke the tool with an explicit
data root using the service account reported above (replace `averon` only if
the service reports another account):

```sh
cd /opt/averon/pilot-stage
sudo -u averon /opt/averon/venv/bin/python -m averon_import.tools.ai_first_workspace_inventory \
  --data-dir /var/lib/averon
```

The output contains one record per eligible 32-hex document directory. Treat
`security_state != verified_for_current_process`, null checkpoint counts, or
`ordinary_access_basis == administrator_recovery_required` as a stop condition
for ordinary-user access. Do not infer ownership from a document ID, filename,
timestamps, or an OCR result.

## Interpreting legacy access

- `access_file_present` indicates whether an explicit owner/access ledger exists.
- `uploader_identity_present` reports only whether protected metadata contains
  a server-recorded uploader identity; it does not disclose that identity.
- `ordinary_access_basis` reports whether access can be grounded in the
  explicit access ledger or server-authored review history.
- Missing access provenance and missing review history require an administrator
  to verify ownership independently before recovery. Do not let the first
  ordinary user who opens a legacy document claim it.
- Never create `access.json` by copying an identity from another workspace.
  Recovery must preserve existing review history and add only an explicitly
  approved owner/access mapping through the application's supported process.

The command is strictly read-only. It does not repair ACLs, rewrite access
records, or recover workspaces. Its counts are inventory indicators, not proof
that a review decision is valid or exportable.
