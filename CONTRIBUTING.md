# Contributing

Thanks for considering a contribution.

## Development

This project is intentionally small:

- `app/` contains the FastAPI app, admin UI, sync logic, and link resolver.
- `nginx/` contains the gateway template.
- `web/` contains the Emby Web external player injection script.

Before submitting changes:

```bash
python -m py_compile app/main.py
```

For Docker testing:

```bash
cp .env.example .env
docker compose up -d --build
```

## Pull Request Guidelines

- Do not include credentials, tokens, private URLs, cookies, or media links.
- Keep changes focused.
- Update `README.md` when behavior or configuration changes.
- Keep the project license and attribution notices intact.
- Prefer conservative defaults that do not contact OpenList or remote storage
  until the user explicitly configures and triggers sync.

## Legal and Usage Scope

This project is designed for personal media library management. Contributions
should not add hardcoded credentials, account-sharing behavior, DRM bypassing,
or bundled copyrighted media.

