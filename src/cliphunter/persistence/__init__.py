"""Candidate persistence interfaces and implementations."""

from .base import CandidateStore
from .sqlite import SQLiteCandidateStore

__all__ = ["CandidateStore", "SQLiteCandidateStore"]
