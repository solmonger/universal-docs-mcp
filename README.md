# Universal Docs MCP

MCP server that fetches latest **stable release** documentation for any programming language, framework, or package. Keeps AI coding agents up-to-date with current APIs.

## Features

- **Multi-ecosystem**: Python (PyPI), JavaScript/TypeScript (npm), Rust (crates.io)
- **Stable releases only**: Skips alpha, beta, RC, dev versions
- **Cached**: SQLite cache with 24h TTL
- **MCP native**: Works with Claude Code, Claude Desktop, or any MCP client

## Tools

| Tool | Description |
|------|-------------|
| `get_package_info` | Get metadata: latest stable version, docs URL, description, license |
| `get_package_docs` | Fetch actual documentation content (README/description) |
| `cache_stats` | View cache statistics |

## Usage with Claude Code

```json
{
  "mcpServers": {
    "universal-docs": {
      "command": "python3",
      "args": ["-m", "universal_docs_mcp.server"],
      "cwd": "/path/to/universal-docs-mcp"
    }
  }
}
```

## Install

```bash
pip install universal-docs-mcp
# or
git clone https://github.com/solmonger/universal-docs-mcp
cd universal-docs-mcp
pip install -e .
```

## License

MIT
