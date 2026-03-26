import asyncio
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


'''
This is used to test connecting to the MCP server
'''

load_dotenv()

async def connect_and_list_tools(server_url: str):
    async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            resp = await session.list_tools()
            for tool in resp.tools:
                print(tool.name)
                print(tool.description)
                print(tool.inputSchema)  

async def use_add_tool(server_url: str):
    async with streamablehttp_client(server_url) as (rs,ws,_):
        async with ClientSession(rs,ws) as session:
            result = await session.call_tool("add", arguments={'a': 10, 'b': 20})
            print(f"✅ Result: ", [int(r.text) for r in result.content])
    

SERVER_URL = 'http://localhost:8000/mcp'
session = asyncio.run(connect_and_list_tools(SERVER_URL))