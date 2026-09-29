"""FastAPI application package."""

from researchpilot.api.app import create_app
from researchpilot.api.service import ServiceContainer

__all__ = ["ServiceContainer", "create_app"]
