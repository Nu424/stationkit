"""Server wrapper for the Sequence Web App."""

from apps.sequence_app.server.app import create_sequence_app_server

__all__ = ["create_sequence_app_server"]
