from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import os
import sys


MCP_URL = os.getenv("MCP_HTTP_URL", "http://192.168.15.102:8000/mcp")


def _build_client() -> MultiServerMCPClient:
    url = MCP_URL
    return MultiServerMCPClient(
        {
            "http_server": {
                "url": url,
                "transport": "streamable_http",
            }
        }
    )


async def load_tools() -> list:
    return await client.get_tools()


def _load_tools_sync() -> list:
    try:
        return asyncio.run(load_tools())
    except Exception as exc:
        print(
            "Warning: Failed to load MCP tools. Ensure the MCP server is running and set "
            f"MCP_HTTP_URL (current default: {MCP_URL}). "
            f"Continuing without MCP tools. Underlying error: {exc}",
            file=sys.stderr,
        )
        return []


client = _build_client()
tools = _load_tools_sync()
