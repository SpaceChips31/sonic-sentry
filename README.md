# SonicSentry

SonicSentry is an audio quality gate for FLAC libraries. It verifies file
integrity, performs spectral forensic analysis, presents uncertain results for
human review, and routes complete albums to Picard staging, quarantine, or a
rejected area.

## What it does

- checks FLAC integrity with `flac -t`
- runs a reproducible, vendored forensic engine
- analyzes individual releases, artist folders, or mixed libraries
- groups conventional multi-disc folders as one release
- explains results in English or Italian
- keeps automatic verdicts separate from human decisions
- routes complete album directories without overwriting existing destinations
- recovers interrupted queue jobs after a worker restart
- stores its database, reports, uploads, and settings in persistent volumes

## Production deployment

Requirements:

- Docker Engine with Docker Compose v2
- a media directory mounted on the Docker host
- read/write access for the configured UID and GID

Download `compose.yml` and `.env.example`, then prepare the deployment:

```sh
cp .env.example .env
mkdir -p data reports uploads
```

Edit `.env` and verify the media paths, UID, GID, and workflow destinations.
Then start SonicSentry:

```sh
docker compose pull
docker compose up -d
docker compose ps
```

Open `http://HOST:8090`, or the port selected in `.env`.

The production `compose.yml` is self-contained. Development overrides are not
required on a production host.

## Container image

Stable images are published to:

```text
ghcr.io/spacechips31/sonic-sentry
```

Available release tags include the exact version (`0.2.0`), the minor release
(`0.2`), and `latest`. Production deployments should prefer the exact
version.

For a private fork or private package, authenticate before pulling:

```sh
docker login ghcr.io
```

Use your GitHub username and a personal access token with `read:packages`.

## Persistent data

Back up these directories:

- `data/`: SQLite database and application settings
- `reports/`: complete forensic JSON reports
- `uploads/`: releases uploaded through the browser

The music library remains outside the container under `MEDIA_ROOT`.

## Workflow settings and automatic hand-off

Open **Settings > Album destinations and automation** to authorize source
folders, select destinations, and enable album moves. Paths are absolute paths
inside the container, must already exist, and must be writable by its UID/GID.
Use one authorized source folder per line. The source roots themselves cannot
be moved or deleted. Destination folders must be separate from one another.
Quarantine/rejected destinations may also be authorized source roots so their
albums can later be reviewed and moved to staging.

Choose one routing policy:

- **Manual**: analysis records results and leaves files in place.
- **Approved albums only**: conforming albums go directly to Picard staging
  without a confirmation; uncertain and rejected albums stay in place.
- **Every verdict**: conforming albums go to staging, uncertain albums to
  quarantine, and rejected albums to the rejected folder.

Automation runs after worker analyses, including queued uploads and batches.
Importing an existing JSON report does not move files. No policy automatically
deletes files or approves integrity failures. Existing destinations are never
overwritten. Routing failures appear as warnings on completed analyses; the
analysis result remains available from the queue and the release library.

Settings are stored in the database under `data/`, shared immediately with the
worker and preserved after restarts. Environment variables are the defaults
until settings are saved in the UI; saved values then take precedence. Disable
automatic routing in Settings to stop subsequent hand-offs. An operation already
in progress is allowed to finish.

The **Releases** page provides search, status filters and pagination. Failed
analyses can be retried from the queue. Reanalysis preserves the release link
and file-operation history, replaces track results and resets human reviews.

## Automatic ingestion and recovery (0.2)

Enable **Watch folder** on each desired source in Settings. Monitoring starts
only after you opt in for that source. The worker checks sources between jobs;
the scan interval defaults to 60 seconds and the stability wait to 300 seconds.
Both values are editable without a restart. Large watched libraries are processed
in groups of up to 25 new jobs per scan; subsequent scans continue with the
remaining albums. An album must remain unchanged
across observations for the full wait, with no known partial-download files.
Source roots, hidden working directories and all configured output folders are
excluded. Organize files into album subdirectories below the watched root.

This is a filesystem readiness heuristic, not a download-client completion
notification. Files changing during analysis are rejected for retry, and never
routed. For clients that preallocate files or pause downloads for long periods,
watch their completed-download directory and increase the stability wait.

Exact FLAC-file hashes prevent repeated automatic analyses of identical sets of
files, even if names differ. Different tags, encodings or masters are not treated
as identical. Duplicates stay where they are; Activity links to the existing
result. Failed or cancelled observations require an explicit retry rather than
retrying forever. Manual analysis remains available for intentional reanalysis.

File moves now use a persistent journal: copy into a hidden temporary directory,
verify every file with SHA-256, publish without replacing an existing destination,
update the release and audit entry, then remove verified originals. This requires
free space for a complete extra album copy and additional disk I/O, including
when source and destination share a filesystem. Interrupted transfers resume
when the worker starts. Changed originals or ambiguous destinations stop for
operator review; the remaining files are preserved.

Use **Activity** to inspect transfers and ingestion. **Verify and resume** retries
a stopped transfer. **Close and keep all files** explicitly closes an unresolved
transfer once the release's referenced files exist, preserving all remaining
source, destination and temporary files; any retained temporary directory is
shown for manual inspection. No automatic cleanup deletes those preserved files.

The queue shows completed/total tracks and offers cancellation and retry.
Cancellation stops the validator and its child processes; a file transfer that
has already started is allowed to finish. Release links are stored explicitly.
Clearing terminal queue history hides entries while preserving ingestion links.
Bulk review on the Releases page changes human decisions without moving files.
Generated spectrograms are stored under `reports/` and displayed on track pages;
analysis does not write derived images into source albums. The small vendored
engine I/O patch is documented in `vendor/audio-forensic/SOURCE.md`.

Existing SQLite databases are upgraded automatically with additive migrations;
back up `data/` before upgrading. Keep web and worker on the same version and
run one worker per database. An OS lock prevents a second worker from reclaiming
another worker's live analyses. Do not downgrade an upgraded deployment while
transfers remain unfinished. CI also exercises a real FLAC analysis and verified
routing inside the built container.

## Updating

Set the desired version in `.env`, then run:

```sh
docker compose pull
docker compose up -d --remove-orphans
```

For SonicSentry 0.2.0:

```env
LOSSLESS_VALIDATOR_IMAGE=ghcr.io/spacechips31/sonic-sentry
LOSSLESS_VALIDATOR_VERSION=0.2.0
```

## Local development

```sh
cp .env.example .env
docker compose -f compose.yml -f compose.dev.yml build
docker compose -f compose.yml -f compose.dev.yml up -d
```

## Releasing

Pushing a semantic version tag such as `v0.2.0` runs the GitHub Actions
publication workflow. It builds the image once and publishes the corresponding
`0.2.0`, `0.1`, and `latest` tags to GHCR. Release tags must not be reused.

## Exit statuses

The command-line validator returns:

- `0`: every track passed
- `10`: at least one track needs review
- `20`: at least one FLAC failed integrity validation
- `2`: invalid input or no supported audio files

## Forensic engine

The tested engine and its Python dependencies are stored under
`vendor/audio-forensic`. Provenance is documented in
`vendor/audio-forensic/SOURCE.md`.

## License and attribution

SonicSentry is released under the [MIT License](LICENSE). You may use, modify,
redistribute, and use it commercially, provided that the copyright and license
notice are retained.

The vendored `audio-forensic` engine is copyright Bishal Das and is also
licensed under MIT. SonicSentry additionally uses third-party Python packages
and separate audio command-line tools under their own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for sources, licenses, and
attributions.

## Roadmap

Planned post-0.1 work, including optional local authentication, is tracked in
[ROADMAP.md](ROADMAP.md).
