# SonicSentry roadmap

This roadmap records remaining directions after the 0.2 release. Items are not
promises and may be reordered as the real music workflow evolves.

## Delivered in 0.2

- Configurable folder watching with persistent stability checks
- Exact-file duplicate detection without deleting duplicates
- Verified, journaled album transfers with restart recovery and operator resolution
- Track progress, cancellation, retry and explicit result links
- Bulk album review, ingestion history and file-transfer activity
- Additive database upgrades and a real-FLAC container smoke test

## Access and security

- Optional authentication inspired by the *arr applications
- Local user accounts with administrator and operator roles
- Login form, session management, logout, and password changes
- CSRF protection for forms that modify settings, reviews, jobs, or files
- Optional trusted-proxy and single-sign-on support for homelab deployments
- First-run setup that remains disabled when authentication is not requested

Authentication must remain optional so SonicSentry can still run safely on a
trusted private network without adding unnecessary setup.

## Analysis workflow

- Resumable and scheduled library scans
- Audio-level duplicate comparison beyond exact FLAC-file hashes
- Configurable quality policies and per-source defaults
- Richer batch grouping and scheduling beyond track progress, cancellation and retry
- Comparison between different editions of the same album

## Sources and integrations

- Native source adapters for slskd, qBittorrent, and NZB clients
- Webhooks or API endpoints for automated ingestion
- Notifications for completed, quarantined, and failed releases
- Picard hand-off status and post-tagging reconciliation

## Review and reporting

- Searchable audit history
- Exportable summaries for releases and batches
- Side-by-side forensic explanations for uncertain tracks
- Optional comments and review notes at album level

## Operations

- Versioned migration tooling and guided backup/restore procedures beyond additive upgrades
- Health and metrics endpoints suitable for monitoring
- Multi-architecture container images
- Configurable retention for reports, uploads, and completed jobs
