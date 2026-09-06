"""Exercise the third-party provider factory against a disposable SQLite database."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
PROVIDER_DIRECTORY = Path(__file__).resolve().parent

for directory in (CORE_DIRECTORY, PROVIDER_DIRECTORY):
    directory_text = str(directory)
    if directory_text in sys.path:
        sys.path.remove(directory_text)
    sys.path.insert(0, directory_text)

from toolframe_database_config import DatabaseSettings
from toolframe_database_service import create_provider


def main() -> None:
    """Run the provider contract example using only temporary data."""

    with TemporaryDirectory() as temporary_directory:
        settings = DatabaseSettings(
            provider="observed_sqlite:create",
            sqlite_path=Path(temporary_directory) / "example.db",
        )
        provider = create_provider(settings)
        try:
            with provider.connection() as connection:
                connection.execute(
                    "CREATE TABLE example_records "
                    "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                ).close()
                connection.commit()

            identifier = provider.insert("example_records", {"value": "hello"})
            assert isinstance(identifier, int) and identifier > 0

            with provider.transaction() as transaction:
                result = transaction.execute(
                    "UPDATE example_records SET value=:value WHERE id=:id",
                    {"value": "changed", "id": identifier},
                )
            assert result.affected_rows == 1

            returned_rows = provider.query(
                "SELECT id, value FROM example_records WHERE id=:id",
                {"id": identifier},
            ).to_dict(orient="records")
            assert returned_rows == [{"id": identifier, "value": "changed"}]
            assert provider.public_info()["implementation"] == "example.observed_sqlite"
        finally:
            provider.close()

    print("Third-party provider contract passed.")


if __name__ == "__main__":
    main()
