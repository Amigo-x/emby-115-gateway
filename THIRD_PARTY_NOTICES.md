# Third Party Notices

This project depends on the following open source packages.

The list below is provided for convenience. Please consult each package's
published metadata for the authoritative license text and dependency tree.

## Python Packages

| Package | Version | License |
| --- | --- | --- |
| FastAPI | 0.124.0 | MIT |
| Uvicorn | 0.38.0 | BSD-3-Clause |
| Requests | 2.32.5 | Apache-2.0 |
| python-multipart | 0.0.20 | Apache-2.0 |

## Runtime Images

| Image | Purpose |
| --- | --- |
| `python:3.12-alpine` | FastAPI application runtime |
| `nginx:1.27-alpine` | Gateway and reverse proxy |

## External Services

This project can be configured to communicate with Emby and OpenList. Those
services are not bundled with this project and are governed by their own
licenses and terms.

This project does not include media files, credentials, tokens, cookies, or
third-party account data.
