"""Create a private local configuration. Never prints generated secrets."""

import os
import secrets
from pathlib import Path

path = Path(".env")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write("A4G_ADMIN_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    stream.write("A4G_SESSION_SECRET=" + secrets.token_urlsafe(48) + "\n")
    stream.write("A4G_SECURE_COOKIES=false\nA4G_PUBLIC_ORIGIN=http://localhost:8000\n")
    stream.write('A4G_ALLOWED_HOSTS=["localhost","127.0.0.1"]\nA4G_OPENAI_API_KEY=\n')
print(
    "Created .env with private permissions. Read it locally for your owner password. Configure TLS before public deployment."
)
