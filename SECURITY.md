# Security Policy

## Sensitive Data

Do not commit real credentials, tokens, cookies, account data, media URLs, or
private server addresses.

The following files and directories are intentionally ignored:

- `.env`
- `.env.*`
- `data/`
- `strm-output/`
- `__pycache__/`
- `*.pyc`
- `*.log`

## Deployment Notes

- Change the default admin password immediately after first login.
- Set a stable `APP_SECRET_KEY` in production.
- Set `SESSION_COOKIE_SECURE=true` when serving the admin UI over HTTPS.
- Avoid exposing the admin port to the public internet. Prefer VPN, IP
  allowlists, or an authenticated reverse proxy.
- Treat OpenList tokens and Emby API keys as secrets.

## Admin UI Security Scope

The admin UI is intended for local, LAN, or VPN maintenance.

The project provides basic protection only:

- login with username and password
- PBKDF2 password hashing
- signed session cookies
- `HttpOnly` cookies
- optional HTTPS-only cookies through `SESSION_COOKIE_SECURE=true`

The project does not provide a complete internet-facing admin security stack.
It does not include multi-factor authentication, CAPTCHA, account lockout,
fine-grained RBAC, audit logging, or abuse detection.

Users are responsible for the network security boundary. Use a firewall, VPN,
IP allowlist, or an authenticated reverse proxy before exposing the admin UI.

## Playback Security Modes

The playback security mode controls how STRM links can be exchanged for playable
links. It does not replace network access control.

- `strict`: most conservative. Static STRM entries cannot be played directly.
  Playback links are obtained through the Emby Web external player flow. This is
  better for public gateways or multi-user environments, but less compatible
  with clients that read STRM files directly.
- `compatible`: compatibility and security trade-off. Static STRM tokens can be
  exchanged for short-lived `/play` links. This is useful for Infuse, VLC, and
  similar clients, but leaked STRM contents or tokens may allow link exchange
  until the token is rotated.
- `private`: most permissive. Static STRM tokens redirect directly to the
  OpenList/115 direct link. Use only for private LAN or VPN scenarios. Do not
  use this mode for public or shared environments.

If an attacker can access the gateway and obtain a valid token, they may consume
direct-link retrieval quota or trigger remote storage risk controls regardless of
the selected mode.

## Reporting Issues

If you find a security issue, please avoid posting real secrets or direct media
links in public issues. Provide a minimal reproduction with redacted values.

