"""Test-only stand-in for the retired LangChain community docstore class."""

from __future__ import annotations

import sys
from types import ModuleType


class InMemoryDocstore:
    def __init__(self, documents: dict[str, object]):
        self._dict = documents


InMemoryDocstore.__module__ = "langchain_community.docstore.in_memory"

community = ModuleType("langchain_community")
community.__path__ = []
docstore = ModuleType("langchain_community.docstore")
docstore.__path__ = []
in_memory = ModuleType("langchain_community.docstore.in_memory")
in_memory.InMemoryDocstore = InMemoryDocstore
community.docstore = docstore
docstore.in_memory = in_memory
sys.modules.setdefault("langchain_community", community)
sys.modules.setdefault("langchain_community.docstore", docstore)
sys.modules.setdefault("langchain_community.docstore.in_memory", in_memory)


__all__ = ["InMemoryDocstore"]
