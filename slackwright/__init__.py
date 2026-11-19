"""Building blocks for AI agents that live in Slack."""

import importlib.metadata

from slackwright.render import Stream, post_markdown
from slackwright.toolkit import Toolkit, ToolResult

version = importlib.metadata.version('slackwright')

__all__ = ['Stream', 'ToolResult', 'Toolkit', 'post_markdown', 'version']
