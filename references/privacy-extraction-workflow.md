# Encrypted Private Collection Workflow

## Trust boundaries

- The employee's browser handles plaintext fields and attachments locally.
- The shared transport receives only `.yintian` ciphertext.
- The Agent and MCP layer receive ciphertext metadata, aggregate status, or data-minimized reports. Reports retain `employee_id` and name, so they are not anonymous.
- The authorized handler's terminal may reveal plaintext only through the explicit `reveal` command, in a terminal not controlled or recorded by an Agent.
- Installing the Skill is not authorization. Decryption requires both the encrypted task private key and its one-time displayed task password.

## Required workflow

1. Create a task from `employee_id,name` CSV and a reviewed collection config.
2. Privately send each employee their own invite HTML; it contains a unique secret invitation token as well as the task public key.
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
- Do not claim identity authentication or electronic signature: private invite delivery is a workflow convention, not cryptographic proof of the submitter.
- Treat legacy plaintext `yintian-task/1` and `yintian-task/2` packages as untrusted: they are importable for compatibility only, and the importer prints a prominent warning that they are unencrypted and unauthenticated, then requires the operator to type the task ID to confirm on an interactive TTY. New exports are always the encrypted, authenticated `yintian-task/3` format.
- Do not use cleanup tooling (`cleanup.py` / `cleanup_temp_files`) to delete rosters, invites, `.yintian` ciphertext, reports, state databases, or task packages. Cleanup only removes orphaned atomic-write temp files, dead `.write.lock` files, and `__pycache__` caches; user data removal goes through `purge` or explicit user confirmation.

## Field and OCR policy

- Normalize before validation.
- Validate Chinese phone numbers and GB 11643 identity numbers deterministically.
- Required missing fields, invalid values, mismatched roster names, changed notice/template hashes, OCR disagreement, and low OCR confidence require review.
- OCR never overwrites an employee-entered value automatically.
- Decrypted image bytes and rendered PDF pages remain in memory; no plaintext temporary files are created.

## Cryptography and envelope format

- Invite HTML carries the task public key (RSA-OAEP-3072/SHA-256) and a per-invite random authentication token; the token is stored server-side only as a SHA-256 hash and compared in constant time.
- The browser encrypts fields, token, and attachments with AES-256-GCM (128-bit tag); the data key is wrapped with RSA-OAEP-3072/SHA-256. The AAD binds format version, task ID, invite ID, schema hash, and key ID.
- The envelope's `algorithms` field is validated strictly at ingest; mismatches are rejected as invalid submissions.
- The task private key is stored as a `yintian-key/1` JSON envelope: scrypt (n=32768, r=8, p=1, random 16-byte salt) derives a 32-byte key that wraps the PKCS#8 DER with AES-256-GCM. Legacy PKCS#8 PBES2 PEM files (`BestAvailableEncryption`) from existing tasks remain readable.
- Exported `.yintian-task` packages are `yintian-task/3` encrypted envelopes: the task ZIP is built in memory and encrypted whole with AES-256-GCM under a scrypt-derived key (same parameters, independent salt) from a one-time handoff password. The AAD binds the format name and task ID, so a wrong password or any tampering is rejected by the GCM tag before anything is written to disk. KDF parameters are validated against fixed upper bounds before any key derivation, so envelopes with malformed or inflated scrypt parameters are rejected before decryption. Legacy plaintext `yintian-task/1` and `yintian-task/2` ZIPs remain importable with a prominent warning plus an interactive re-confirmation (typing the task ID) on the human TTY.
- Ingest stores at most `MAX_VERSIONS_PER_INVITE` versions per invite (default 10, override with `YINTIAN_MAX_VERSIONS_PER_INVITE`); submissions beyond the cap are rejected without touching existing versions.
