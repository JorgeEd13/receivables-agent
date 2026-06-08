# MCP server — the receivables ledger as a tool

An [MCP](https://modelcontextprotocol.io) server that exposes the synthetic
receivables ledger to any MCP-aware client (Claude Code, Claude Desktop, other
agents). It runs the **same guardrail** as the in-app tool: a read-only DuckDB
connection plus the `guard_query` filter (SELECT/WITH only, allow/deny lists,
single statement, row cap). See ADR-008.

## Exposes

- **Tool `query_ledger(sql)`** — run a single guarded read-only SELECT; returns
  JSON rows (or a JSON error with the reason).
- **Resource `schema://ledger`** — the schema (views, tables, columns) to write
  SQL against.

## Prerequisites

```bash
pip install -e ".[mcp,data]"
python data/generate.py          # build data/ledger.duckdb if you haven't
```

## Run (stdio)

```bash
python -m mcp_server.server
```

## Register with Claude Code

```bash
claude mcp add receivables-ledger -- python -m mcp_server.server
```

Or add it to your MCP client config:

```json
{
  "mcpServers": {
    "receivables-ledger": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/receivables-agent"
    }
  }
}
```

Then ask the client questions like *"how many customers are 90+ days overdue?"*
— it will call `query_ledger` with SQL it writes against `schema://ledger`,
and the guardrail keeps every call read-only and scoped.
