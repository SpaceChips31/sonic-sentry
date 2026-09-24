# SonicSentry roadmap

This roadmap records likely directions after the 0.1 release. Items are not
promises and may be reordered as the real music workflow evolves.

## Access and security

- Optional authentication inspired by the *arr applications (implemented on `dev`)
- Local user accounts with administrator and operator roles (implemented on `dev`)
- Login form, session management, logout, and password changes (implemented on `dev`)
- CSRF protection for forms that modify settings, reviews, jobs, or files
- Optional trusted-proxy and single-sign-on support for homelab deployments
- First-run setup that remains disabled when authentication is not requested

## Configuration

- Sectioned settings workspace with sidebar and one global save action (implemented on `dev`)
- Environment-owned settings shown as locked and database-owned settings editable (implemented on `dev`)
- Unsaved-change warning and optional advanced settings view (implemented on `dev`)
- Additional policy flags for routing, review thresholds, retention, and integrations
- Import and export of configuration for backup or migration

Authentication must remain optional so SonicSentry can still run safely on a
trusted private network without adding unnecessary setup.

## Analysis workflow

- Resumable and scheduled library scans
- Better duplicate-release detection
- Configurable quality policies and per-source defaults
- Batch selection, pause, resume, cancellation, and concurrency controls (implemented on `dev`)
- Comparison between different editions of the same album

## Sources and integrations

- Native source adapters for slskd, qBittorrent, and NZB clients
- Webhooks or API endpoints for automated ingestion
- Notifications for completed, quarantined, and failed releases
- Picard hand-off status and post-tagging reconciliation

## Review and reporting

- Review audit history, notes, latest-decision undo, and reversible moves (implemented on `dev`)
- Exportable summaries for releases and batches
- Album-relative forensic comparison and spectrograms (implemented on `dev`)
- Optional comments and review notes at album level (implemented on `dev`)

## Operations

- Database migrations and documented backup/restore procedures
- Health and metrics endpoints suitable for monitoring
- Multi-architecture container images
- Configurable retention for reports, uploads, and completed jobs
- Display the application version embedded in the container image rather than a runtime version variable (implemented on `dev`)
