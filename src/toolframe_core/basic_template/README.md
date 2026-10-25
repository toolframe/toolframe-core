# Service plugin

A minimal Toolframe plugin with one bounded echo service and no provider requirements.

```sh
uv sync --group dev
uv run pytest -q
uv build --wheel
toolframe --home /path/to/app plugin add path:. --enable
```

Other plugins can declare a dependency on `basic.echo` and call `echo` with
`{"message":"Hello"}`. The internal method is not exposed as a visitor HTTP API. Add explicit business scopes and entrance policy before
exposing sensitive operations. SQL, Web, Dashboard and MCP are opt-in capabilities.
The generated source belongs to this project; template upgrades do not rewrite it.
