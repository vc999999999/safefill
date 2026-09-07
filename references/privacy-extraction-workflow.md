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
- Do not let an Agent start or observe `create`, `review`, `reveal`, or `purge`; a PTY/TTY check is not proof of a human-only terminal.
- Do not store decrypted JSON, attachments, OCR text, or complete identity packets.
- Do not place phone suffixes, ID suffixes, addresses, invitation tokens, or OCR evidence in shared reports. Treat names and employee IDs that remain in reports as personal data.
- Do not claim identity authentication or electronic signature: private invite delivery is a workflow convention, not cryptographic proof of the submitter.
- Do not call `.yintian-task` encrypted or authenticated. It contains plaintext roster metadata and must travel through an approved authenticated and encrypted channel.

## Field and OCR policy

- Normalize before validation.
- Validate Chinese phone numbers and GB 11643 identity numbers deterministically.
- Required missing fields, invalid values, mismatched roster names, changed notice/template hashes, OCR disagreement, and low OCR confidence require review.
- OCR never overwrites an employee-entered value automatically.
- Decrypted image bytes and rendered PDF pages remain in memory; no plaintext temporary files are created.
