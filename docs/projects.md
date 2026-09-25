# Separate projects

Each agentforge installation has one owner workspace. Its Settings page names that workspace and stores its goal.
Tasks have separate chats, but they share the installation's memory, credentials, policies, and storage.
A task name or tenant label is not a security boundary between customers.

## Run independent projects safely

Use a separate installation for each project that needs separate memory or credentials.
Give each installation its own:

- Data directory or PostgreSQL database and object-storage location.
- Owner password, session secret, and vault key file.
- Provider credentials, permissions, and financial limits.
- Hostname, HTTPS origin, backup destination, and service configuration.

For Docker Compose, use separate checkout directories and distinct project names:

```sh
# Run inside the first project's checkout, with its own private .env.
docker compose -p agentforge-alpha up --build -d --wait
```

For another project, use a different host or configure distinct host port mappings and storage mounts first.
A different Compose project name alone does not prevent fixed ports or host bind mounts from colliding.
Never share a SQLite volume, external vault key, or provider credential by accident.

Set each project's workspace name in Settings after login.
Use [instance branding](white-label.md) for a different product name, logo, or accent.
Test login, saved work, backup, and restoration independently for each installation.

An in-app project switcher, multiple customer accounts, and shared-host tenant isolation are not implemented.
Do not sell a shared installation as isolated customer workspaces.
