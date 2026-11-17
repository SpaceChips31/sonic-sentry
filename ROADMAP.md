# SonicSentry roadmap

## Delivered in 0.4.3

- Plain-language result summaries, evidence explanations and recommended next actions
- Problem-first track order, direct album review and next-pending-track navigation
- Separate review/file actions, expandable technical detail, spectrogram and history

## Delivered in 0.4.2

- Previewed single/bulk analysis forgetting with explicit confirmation, retaining physical files
- Atomic forget-and-rescan with source routing policy and active/recoverable-work protection
- Scoped duplicate/observation cleanup and stale reconciliation proposal removal

## Delivered in 0.4.1

- Explicit destination media GID, 0664 files and setgid 2775 directories
- Source modes/ACLs no longer cloned; destination ACL group access is verified
- Linux regression with separate SonicSentry/Picard UIDs and restrictive sources
- Administrator history reset with metadata backup, activity guards and preserved media/config/accounts

## Delivered in 0.4

- Persistent background file batches and queued transfer recovery
- Signed preflight preview with space, permissions, collision and source-change checks
- Confirmed database-only reconciliation of moved/tagged FLAC albums
- Independent scan/stability/routing policies per source
- Review-reason filters, album evidence summaries and side-by-side track comparison

## Delivered in 0.3.1

- Recursive FLAC-only folder lists with destination diagnostics toggle
- Bulk quarantine-to-staging with explicit optional approval and per-album results
- Verified portable publication for filesystems without exclusive atomic rename

## Delivered in 0.3

- Physical destination inventory, independent of analysis verdict
- Verified recoverable trash, original-path restore and confirmed permanent purge
- Preview-first retention cleanup preserving referenced files and transfer remnants
- Worker heartbeat, independent source scanning, bounded timeouts and transient retries
- Review notes and actor audit history
- Consistent metadata backup and guided offline restore
- Optional local accounts (viewer/operator/admin), signed sessions and CSRF protection
- Full Docker image references and tested dev/latest/numeric publication

## Remaining directions

- Scheduled/resumable library scans and per-source quality policies
- Audio-level duplicate comparisons across tags and encodings
- Comparisons across album editions and clearer guidance on uncertain forensic evidence
- Exportable release/batch summaries and searchable global audit history
- Explicit Picard hand-off tracking, building on existing post-tagging path reconciliation
- Guided account administration and optional trusted-proxy/SSO support
- Multi-architecture images and richer operational metrics

Notifications and incoming download-client events are outside the current scope.
