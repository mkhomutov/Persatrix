"""
MCP Bridge — placeholder for the planned link to external MCP servers.

Not built yet. The plan: start MCP (Model Context Protocol) tool servers
over stdio or SSE, list their tools, and pass agents' tool calls to them.
Today nothing here runs, nothing reads ``config/mcp-servers.yaml``, and an
``mcp:`` entry in an agent's ``tools`` list gives that agent no tools.
"""

# TODO: Implement MCPClient (stdio transport)
# TODO: Implement MCPClient (SSE transport)
# TODO: Implement server lifecycle (lazy start, health check, restart, shutdown)
# TODO: Implement tool discovery (list tools from MCP server)
# TODO: Implement tool invocation (forward call, return result)
# TODO: Implement connection pooling (share server across agents)
