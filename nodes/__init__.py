"""Service nodes. Importing this package registers every endpoint Doxa serves."""
from __future__ import annotations

from nodes.content_nodes import ALL_NODES as CONTENT_NODES
from nodes.engagement_nodes import ALL_NODES as ENGAGEMENT_NODES
from nodes.market_nodes import ALL_NODES as MARKET_NODES
from nodes.page_nodes import ALL_NODES as PAGE_NODES
from nodes.proof_nodes import ALL_NODES as PROOF_NODES
from nodes.site_nodes import ALL_NODES as SITE_NODES
from nodes.visibility_nodes import ALL_NODES as VISIBILITY_NODES
from runtime import NodeRegistry

ALL_NODES = [*PAGE_NODES, *SITE_NODES, *MARKET_NODES, *VISIBILITY_NODES,
             *PROOF_NODES, *CONTENT_NODES, *ENGAGEMENT_NODES]


def build_registry() -> NodeRegistry:
    registry = NodeRegistry()
    for node in ALL_NODES:
        registry.register(node)
    return registry


__all__ = ["ALL_NODES", "build_registry"]
