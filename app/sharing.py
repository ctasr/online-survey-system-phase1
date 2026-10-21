"""Build a share link only from an explicitly configured public origin."""

from flask import current_app


def survey_link(path):
    """Return (URL, scope); an unconfigured local server has no public URL."""
    configured = current_app.config["PUBLIC_BASE_URL"]
    if configured:
        return configured + path, "public"

    return None, "unconfigured"
