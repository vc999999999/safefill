# Encrypted Private Collection Workflow

## Trust boundaries

- The employee's browser handles plaintext fields and attachments locally.
- The shared transport receives only `.yintian` ciphertext.
- The Agent and MCP layer receive ciphertext metadata, aggregate status, or data-minimized reports. Reports retain `employee_id` and name, so they are not anonymous.
- The authorized handler's terminal may reveal plaintext only through the explicit `reveal` command, in a terminal not controlled or recorded by an Agent. The `export-clear` command is the single sanctioned plaintext-exit path: it runs only on that human TTY (getpass password plus typed task-ID confirmation), applies a `--fields` whitelist and `--mask last4|mid4` redaction, and writes the plaintext XLSX locally with 0600 permissions.
- Plaintext exists only on authorized terminals: the employee's own machine (browser or the employee-side yintian-fill Skill) and the handler's `review`/`reveal`/`export-clear` session. Once an export-clear file leaves memory and lands on disk, the system cannot govern its further propagation — state this honestly to the user.
- Installing the Skill is not authorization. Decryption requires both the encrypted task private key and its one-time displayed task password.

## Required workflow

1. Create a task from `employee_id,name` CSV and a reviewed collection config.
2. Distribute the form: in `directed` mode, privately send each employee their own invite (HTML plus `INV-*.yintian-form` JSON) containing a unique secret invitation token and the task public key; in `group` mode, share the single tokenless `FORM.yintian-form` JSON with the whole roster.
3. The employee fills the form locally, reviews masked values, confirms the notice, and exports a `.yintian` file.
4. Receive files by a private channel and run `ingest`.
5. Run `review` in a terminal outside the Agent session and enter the task password interactively.
6. Use only `status` and redacted XLSX/JSON reports for coordination.
7. Use `reveal` for one submission only when an authorized human needs plaintext.
8. At retention expiry, stop ingest, review, report, export, and reveal, then run the confirmed `purge` flow.

## Forbidden behavior

- Do not pass the task password through prompts, command arguments, environment variables, or files.
- Do not let an Agent start or observe `create`, `review`, `reveal`, `purge`, `export-task`, or `import-task`; a PTY/TTY check is not proof of a human-only terminal. Export displays a one-time handoff password and import prompts for it interactively.
- The handoff password is shown once at export and is unrecoverable; send it through a separate secure channel from the package, never through prompts, command arguments, or files. The task password never travels with the package either.
- Do not store decrypted JSON, attachments, OCR text, or complete identity packets.
- Do not place phone suffixes, ID suffixes, addresses, invitation tokens, or OCR evidence in shared reports. Treat names and employee IDs that remain in reports as personal data.
- Do not claim identity authentication or electronic signature: private invite delivery is a workflow convention, not cryptographic proof of the submitter. This applies with full force to `group` mode: submissions carry `GRP-<employee_id>` with no invitation token at all, so anyone holding the shared `FORM.yintian-form` can submit under any employee_id; identity rests solely on roster-name matching flags at review plus human confirmation. State this limit honestly and recommend `directed` mode (per-invite token) for high-sensitivity collection.
- Treat `export-clear` output as authorized plaintext at its destination: the command belongs to the human-terminal set (never started, observed, or captured by an Agent; the task password is entered via getpass and never passes through the Agent). The exported XLSX is confined to the authorized handler's machine with 0600 permissions and a prominent plaintext warning; it must be deleted after delivery. The Agent never runs it, never touches the password, and never opens or forwards the output.
- Treat legacy plaintext `yintian-task/1` and `yintian-task/2` packages as untrusted: they are importable for compatibility only, and the importer prints a prominent warning that they are unencrypted and unauthenticated, then requires the operator to type the task ID to confirm on an interactive TTY. New exports are always the encrypted, authenticated `yintian-task/3` format.
- Do not use cleanup tooling (`cleanup.py` / `cleanup_temp_files`) to delete rosters, invites, `.yintian` ciphertext, reports, state databases, or task packages. Cleanup only removes orphaned atomic-write temp files, dead `.write.lock` files, and `__pycache__` caches; user data removal goes through `purge` or explicit user confirmation.

## Field and OCR policy

- Normalize before validation.
- Validate Chinese phone numbers and GB 11643 identity numbers deterministically.
- Required missing fields, invalid values, mismatched roster names, changed notice/template hashes, OCR disagreement, and low OCR confidence require review.
- OCR never overwrites an employee-entered value automatically.
- Decrypted image bytes and rendered PDF pages remain in memory; no plaintext temporary files are created.

## Cryptography and envelope format

- Invite HTML carries the task public key (RSA-OAEP-3072/SHA-256) and, in `directed` mode, a per-invite random authentication token; the token is stored server-side only as a SHA-256 hash and compared in constant time. `group` mode envelopes use `invite_id = GRP-<employee_id>` with no token field, and the per-invite version cap is counted per employee_id.
- The browser encrypts fields, token, and attachments with AES-256-GCM (128-bit tag); the data key is wrapped with RSA-OAEP-3072/SHA-256. The AAD binds format version, task ID, invite ID, schema hash, and key ID.
- The envelope's `algorithms` field is validated strictly at ingest; mismatches are rejected as invalid submissions.
- The task private key is stored as a `yintian-key/1` JSON envelope: scrypt (n=32768, r=8, p=1, random 16-byte salt) derives a 32-byte key that wraps the PKCS#8 DER with AES-256-GCM. Legacy PKCS#8 PBES2 PEM files (`BestAvailableEncryption`) from existing tasks remain readable.
- Exported `.yintian-task` packages are `yintian-task/3` encrypted envelopes: the task ZIP is built in memory and encrypted whole with AES-256-GCM under a scrypt-derived key (same parameters, independent salt) from a one-time handoff password. The AAD binds the format name and task ID, so a wrong password or any tampering is rejected by the GCM tag before anything is written to disk. KDF parameters are validated against fixed upper bounds before any key derivation, so envelopes with malformed or inflated scrypt parameters are rejected before decryption. Legacy plaintext `yintian-task/1` and `yintian-task/2` ZIPs remain importable with a prominent warning plus an interactive re-confirmation (typing the task ID) on the human TTY.
- Ingest stores at most `MAX_VERSIONS_PER_INVITE` versions per invite (default 10, override with `YINTIAN_MAX_VERSIONS_PER_INVITE`); submissions beyond the cap are rejected without touching existing versions.
