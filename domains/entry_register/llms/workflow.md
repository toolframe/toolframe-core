# Entry Register workflow

1. Register same-origin `/mcp` as a Streamable HTTP MCP server.
2. Prove the connection by listing tools.
3. Call `entry_register_list_entries` only when the requested workflow needs
   the current register.
4. Present or transform the bounded result without inventing write access.
5. If the tool is missing, its contract rejects the request, or the app reports
   a fatal source error, preserve the exact message and trace ID and ask the
   owner to route it to the master development agent.

The peer agent must not edit this app, mutate schema, or synthesize missing
business behavior from raw SQL.
