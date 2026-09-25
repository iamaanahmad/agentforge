# Instance branding

agentforge supports one owner per installation. Branding changes its presentation, not its security or account model.

Set these values in your private `.env`:

```dotenv
A4G_BRAND_NAME="Acme operator"
A4G_BRAND_TAGLINE="Your team's work, in one place."
A4G_BRAND_ACCENT="#263d61"
# Optional absolute path to a PNG, JPEG, or WebP file:
# A4G_BRAND_LOGO_FILE=/opt/agentforge/brand/logo.png
```

Restart the web process after changes. With Compose, recreate the web service so it reads the updated environment:

```sh
docker compose up -d --force-recreate web
```

For a custom logo in Docker, mount the file read-only into the web container.
Set `A4G_BRAND_LOGO_FILE` to that container path. The file must remain available when the web process starts.

Names allow 40 characters; taglines allow 160. Text is escaped before display.
The accent must be a six-digit hexadecimal color with 4.5:1 contrast against white.
Logos must be PNG, JPEG, or WebP, at most 256 KiB. Remote URLs, SVG, and HTML are rejected.
Only use a logo you intend to serve publicly. Never point this setting at a private file.

The settings cover the login and workspace names, browser title, footer tagline, chat assistant label, accent, and logo.
They do not rewrite editorial comparison pages, legal notices, support contacts, or source attribution.
Review those separately for your deployment. Changing a logo does not grant trademark or redistribution rights.
The project license remains undecided.

Existing Python imports, CLI commands, `A4G_` environment variables, cookies, and data paths remain unchanged.
To restore defaults, remove the branding settings and restart the web process.
