"""Experimental application integration primitives (contract 0.1).

Opt-in building blocks only. The production session runtime does not yet
load these adapters or expose integration RPCs. No hardware lease is implied.
"""
from .contract import Adapter, Context, CONTRACT_VERSION
from .payload import IntegrationError, object_data, plain_data
from .registry import Manifest, Registry

__all__ = ["Adapter", "Context", "CONTRACT_VERSION", "IntegrationError",
           "Manifest", "Registry", "object_data", "plain_data"]
