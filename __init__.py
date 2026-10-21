"""Standalone Home Assistant Recorder statistics plugin for Hermes."""
from .statistics_tools import register_tools


def register(ctx) -> None:
    register_tools(ctx)
