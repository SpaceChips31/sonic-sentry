#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 VERSION (example: 0.1.0)" >&2
    exit 2
fi

version=$1

case "$version" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *)
        echo "VERSION must use the MAJOR.MINOR.PATCH form" >&2
        exit 2
        ;;
esac

registry_image=${LOSSLESS_VALIDATOR_IMAGE:-ghcr.io/spacechips31/sonic-sentry}
minor=${version%.*}
revision=$(git rev-parse HEAD 2>/dev/null || printf unknown)
build_date=$(date -u +%Y-%m-%dT%H:%M:%SZ)

docker build \
    --build-arg VERSION="$version" \
    --build-arg VCS_REF="$revision" \
    --build-arg BUILD_DATE="$build_date" \
    --tag "$registry_image:$version" \
    --tag "$registry_image:$minor" \
    --tag "$registry_image:latest" \
    .

docker push "$registry_image:$version"
docker push "$registry_image:$minor"
docker push "$registry_image:latest"

echo "Published $registry_image:$version"
