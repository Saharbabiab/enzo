"""A tiny MCP server for tests/test_mcp.py (stdio). Not part of Enzo."""
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("testsrv")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


@server.tool()
def write_note(text: str) -> str:
    """Save a note somewhere."""
    return f"saved: {text}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def env_report() -> str:
    """Which environment variables this server can see."""
    return ",".join(sorted(os.environ))


if __name__ == "__main__":
    server.run("stdio")
