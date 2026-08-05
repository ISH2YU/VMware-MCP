"""vSphere access layer: session handling, queries, mapping and task control."""

from .client import VSphereClient
from .query import InventoryPathIndex, ObjectRecord
from .session import VSphereSession

__all__ = ["InventoryPathIndex", "ObjectRecord", "VSphereClient", "VSphereSession"]
