# Lossless Validator

Lossless Validator checks FLAC integrity, runs spectral forensic analysis and
keeps releases requiring human review in quarantine.

## Features

- FLAC integrity validation with `flac -t`
- vendored, reproducible audio-forensic engine
- sequential background analysis queue
- release and track reports
- human approve/reject decisions kept separate from automatic verdicts
- uploads and browsing of explicitly mounted source directories
- recovery of interrupted jobs after a worker restart

## Requirements

- Docker Engine with Docker Compose v2
- read access to the music library
- writable local directories for the database, reports and uploads

## Production deployment

Copy the example configuration and adjust UID, GID and media paths:

```sh
cp .env.example .env
mkdir -p data reports uploads
docker compose pull
docker compose up -d
docker compose ps
```

Open `http://HOST:8090` or the port configured in `.env`.

Upgrade to a published version by changing
`LOSSLESS_VALIDATOR_VERSION`, then run:

```sh
docker compose pull
docker compose up -d --remove-orphans
```

The mutable state is stored only in `data/`, `reports/` and `uploads/`.
Back up those directories before major upgrades.

## Local development build

```sh
cp .env.example .env
docker compose -f compose.yml -f compose.dev.yml build
docker compose -f compose.yml -f compose.dev.yml up -d
```

## Publishing an image manually

Authenticate to the Forgejo container registry and publish a semantic version:

```sh
docker login git.spacechips.it
./scripts/publish-image.sh 0.1.0
```

The script publishes immutable `0.1.0`, compatibility tag `0.1`, and
`latest`. Do not reuse an existing release version.

## Exit statuses

The validator CLI returns:

- `0`: all tracks passed
- `10`: at least one track requires quarantine/review
- `20`: at least one FLAC failed integrity validation
- `2`: invalid input or no supported audio files

## Vendored forensic engine

The tested forensic implementation and its Python dependencies are stored under
`vendor/audio-forensic`. See `vendor/audio-forensic/SOURCE.md` for provenance.
