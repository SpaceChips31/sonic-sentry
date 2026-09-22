# Third-party notices

SonicSentry is distributed under the MIT License. That license applies to the
SonicSentry source code, not automatically to third-party software used by the
project or installed in its container image.

This document is an attribution and dependency overview. The license files and
notices shipped by each upstream project remain authoritative.

## Vendored source

### audio-forensic

- Upstream: https://github.com/spideyonmoon/audio-forensic
- Copyright: Copyright (c) 2026 Bishal Das
- License: MIT
- Local license: [vendor/audio-forensic/LICENSE](vendor/audio-forensic/LICENSE)
- Snapshot provenance: [vendor/audio-forensic/SOURCE.md](vendor/audio-forensic/SOURCE.md)

The tested source is vendored to keep image builds reproducible. Its original
copyright and license notice are preserved.

## Direct Python dependencies

| Component | Version used by SonicSentry | License | Upstream |
| --- | --- | --- | --- |
| FastAPI | 0.116.1 | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | 0.35.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| Jinja | 3.1.6 | BSD-3-Clause | https://github.com/pallets/jinja |
| SQLAlchemy | 2.0.43 | MIT | https://github.com/sqlalchemy/sqlalchemy |
| python-multipart | 0.0.20 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| NumPy | version selected from `numpy>=1.21.0` | BSD-3-Clause | https://github.com/numpy/numpy |
| SciPy | version selected from `scipy>=1.9.0` | BSD-3-Clause | https://github.com/scipy/scipy |
| pytest | 8.4.2, development only | MIT | https://github.com/pytest-dev/pytest |

These packages may install transitive dependencies under additional compatible
licenses. Their installed distributions contain the applicable metadata and
notices.

## Container runtime and system tools

The published image is based on the official Python 3.13 slim image and Debian.
It installs the following separate command-line tools through Debian packages:

| Component | Upstream licensing summary | Upstream |
| --- | --- | --- |
| Python | Python Software Foundation License Version 2 and bundled component notices | https://docs.python.org/3/license.html |
| FFmpeg | LGPL-2.1-or-later by default; GPL-2.0-or-later when GPL components are enabled | https://ffmpeg.org/legal.html |
| FLAC | BSD-licensed libraries; GPL-licensed command-line utilities | https://xiph.org/flac/license.html |
| SoX | GPL-2.0-or-later, with separately licensed components | https://sourceforge.net/p/sox/code/ci/master/tree/COPYING |
| MediaInfo | BSD-2-Clause | https://github.com/MediaArea/MediaInfo/blob/master/LICENSE |

The exact Debian package build and its copyright file determine the terms for
the binaries in a particular image. SonicSentry invokes these tools as separate
processes and does not relicense them.

## Build and CI tooling

GitHub Actions used to test and publish the project are maintained in their
respective upstream repositories. They are build infrastructure and are not
included in the SonicSentry application image:

- https://github.com/actions/checkout
- https://github.com/actions/setup-python
- https://github.com/docker/setup-buildx-action
- https://github.com/docker/login-action
- https://github.com/docker/metadata-action
- https://github.com/docker/build-push-action

## Reporting an omission

If an attribution or license notice appears to be missing, please open an issue
in the SonicSentry repository with the component name and upstream source.
