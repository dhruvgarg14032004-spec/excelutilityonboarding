"""Entry point for hosting platforms (e.g. Vercel) that look for an `app` object in a root file.
Importing through the `app` package keeps the relative imports inside app/ working."""
from app.main import app  # noqa: F401
