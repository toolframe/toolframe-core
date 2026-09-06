# Third-party provider example

`observed_sqlite.py` shows a third-party factory that extends Toolframe's
supported SQLite dialect. The factory returns an `SQLiteProvider` subclass and
keeps the provider contract unchanged while adding an implementation marker to
`public_info()`.

Configure an application with:

```sh
TOOLFRAME_DB_PROVIDER=observed_sqlite:create
```

The module must be importable from the application's installed dependencies
when `create_provider()` starts. Installing a provider module in application
dependencies is separate from the `fastapi-cordis` lifecycle; this example
covers the factory and provider contract only. No Rust edits are needed.

Run the disposable contract check from this checkout with:

```sh
.toolframe/revamp-test-venv/bin/python examples/providers/run.py
```

When the runtime dependencies are installed in the active Python environment,
the equivalent command is:

```sh
python examples/providers/run.py
```

The example creates its table and data inside a temporary directory, closes the
provider, and removes that temporary data. It does not move or modify existing
data. The host now loads the same factory during `check`, `invoke`, and `serve`,
and the database plugin owns schema preparation and provider cleanup.
