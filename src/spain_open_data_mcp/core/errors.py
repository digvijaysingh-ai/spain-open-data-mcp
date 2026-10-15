"""Errors that tell the caller the problem, the cause and the fix."""

from __future__ import annotations


class DataError(Exception):
    """A request could not be answered. Rendered as 'problem / cause / fix' for the model."""

    def __init__(self, problem: str, cause: str, fix: str) -> None:
        self.problem = problem
        self.cause = cause
        self.fix = fix
        super().__init__(self.render())

    def render(self) -> str:
        return f"Problem: {self.problem}\nCause: {self.cause}\nFix: {self.fix}"


class UpstreamError(DataError):
    """An upstream API failed, timed out or returned something unusable."""


class PlaceNotFound(DataError):
    """A place name or code could not be resolved."""


class AmbiguousPlace(DataError):
    """A place name matches several places; the caller must pick one code."""
