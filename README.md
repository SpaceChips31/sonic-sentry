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
- optionally protects the interface with local administrator and operator accounts
- provides selectable, pausable, resumable, and cancellable analysis batches
- supports quick multi-track review, album decisions, notes, and audit history
- compares uncertain tracks with their album and generates spectrograms on demand

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

Available release tags include the exact version (`0.1.1`), the minor release
(`0.1`), and `latest`. Production deployments should prefer the exact
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

## Updating

Set the desired version in `.env`, then run:

```sh
docker compose pull
docker compose up -d --remove-orphans
```

For SonicSentry 0.1.1:

```env
LOSSLESS_VALIDATOR_IMAGE=ghcr.io/spacechips31/sonic-sentry
LOSSLESS_VALIDATOR_VERSION=0.1.1
```

## Local development

```sh
cp .env.example .env
docker compose -f compose.yml -f compose.dev.yml build
docker compose -f compose.yml -f compose.dev.yml up -d
```

Builds from the `dev` branch are published as
`ghcr.io/spacechips31/sonic-sentry:dev`. They are intended for isolated test
deployments and should not replace the stable container without a backup.

## Configuration ownership

Workflow settings can be managed from the web interface. If a corresponding
environment variable is explicitly supplied by Docker Compose or the runtime,
SonicSentry displays a lock and treats that value as read-only. Variables that
are absent from the container environment use application defaults and remain
editable in the database.

Authentication is disabled by default. Enable it in Settings, or lock it at
deployment time with `SONIC_SENTRY_AUTH_ENABLED=true`. SonicSentry then guides
you through creating the first administrator.

## Releasing

Pushing a semantic version tag such as `v0.1.1` runs the GitHub Actions
publication workflow. It builds the image once and publishes the corresponding
`0.1.1`, `0.1`, and `latest` tags to GHCR. Release tags must not be reused.

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
