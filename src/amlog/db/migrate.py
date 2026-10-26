"""Apply versioned SQL migrations when a database has been configured.

The runner records each applied file in amlog.schema_migrations. It refuses to
continue if a previously applied file has been edited. Nothing in this module
opens a connection unless main() is called with AMLOG_DATABASE_URL set.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from amlog.config import load_settings

MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")


class MigrationError(Exception):
    """A migration could not be discovered or applied safely."""


class ChecksumMismatch(MigrationError):
    """An applied migration file no longer matches the recorded checksum."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def file_checksum(path: Path) -> str:
    """Checksum the file bytes so a line-ending change is visible."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Return migrations in version order.

    Every file in the directory must match NNNN_name.sql. Gaps are allowed.
    Duplicate version numbers are not.
    """

    if not migrations_dir.is_dir():
        raise MigrationError(f"Migration directory does not exist: {migrations_dir}")

    discovered: list[Migration] = []
    seen: dict[str, Path] = {}
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        match = MIGRATION_FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                "Migration file names must look like 0001_initial_schema.sql. "
                f"Found: {path.name}"
            )
        version = match.group("version")
        if version in seen:
            raise MigrationError(
                f"Duplicate migration version {version}: {seen[version].name} and {path.name}"
            )
        seen[version] = path
        discovered.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                checksum=file_checksum(path),
            )
        )
    return discovered


def split_sql(script: str) -> list[str]:
    """Split a PostgreSQL script on semicolons outside strings, comments, and dollar quotes.

    PostgreSQL drivers used by this project send one statement at a time.
    Function bodies use dollar quoting, so a naive split on ';' would break them.
    """

    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    dollar_tag: str | None = None
    in_single = False
    in_line_comment = False
    in_block_comment = False

    while index < length:
        character = script[index]
        following = script[index + 1] if index + 1 < length else ""

        if in_line_comment:
            buffer.append(character)
            index += 1
            if character == "\n":
                in_line_comment = False
            continue

        if in_block_comment:
            buffer.append(character)
            if character == "*" and following == "/":
                buffer.append(following)
                index += 2
                in_block_comment = False
                continue
            index += 1
            continue

        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(character)
            index += 1
            continue

        if in_single:
            buffer.append(character)
            if character == "'":
                if following == "'":
                    buffer.append(following)
                    index += 2
                    continue
                in_single = False
            index += 1
            continue

        if character == "-" and following == "-":
            buffer.append("--")
            index += 2
            in_line_comment = True
            continue

        if character == "/" and following == "*":
            buffer.append("/*")
            index += 2
            in_block_comment = True
            continue

        if character == "'":
            buffer.append(character)
            in_single = True
            index += 1
            continue

        if character == "$":
            match = _DOLLAR_TAG.match(script, index)
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                continue

        if character == ";":
            _append_statement(statements, "".join(buffer))
            buffer = []
            index += 1
            continue

        buffer.append(character)
        index += 1

    if in_single or in_block_comment or dollar_tag is not None:
        raise MigrationError("The SQL script ended inside a string, comment, or function body.")

    _append_statement(statements, "".join(buffer))
    return statements


def _append_statement(statements: list[str], raw_statement: str) -> None:
    if _without_comments(raw_statement).strip():
        statements.append(raw_statement.strip())


def _without_comments(statement: str) -> str:
    """Remove comments so a comment-only fragment is not sent as a statement."""

    characters: list[str] = []
    index = 0
    length = len(statement)
    dollar_tag: str | None = None
    in_single = False
    while index < length:
        character = statement[index]
        following = statement[index + 1] if index + 1 < length else ""
        if dollar_tag is not None:
            if statement.startswith(dollar_tag, index):
                characters.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            characters.append(character)
            index += 1
            continue
        if in_single:
            characters.append(character)
            if character == "'" and following == "'":
                characters.append(following)
                index += 2
                continue
            if character == "'":
                in_single = False
            index += 1
            continue
        if character == "-" and following == "-":
            index += 2
            while index < length and statement[index] != "\n":
                index += 1
            continue
        if character == "/" and following == "*":
            index += 2
            while index + 1 < length and not (
                statement[index] == "*" and statement[index + 1] == "/"
            ):
                index += 1
            index = min(index + 2, length)
            continue
        if character == "'":
            in_single = True
            characters.append(character)
            index += 1
            continue
        if character == "$":
            match = _DOLLAR_TAG.match(statement, index)
            if match:
                dollar_tag = match.group(0)
                characters.append(dollar_tag)
                index += len(dollar_tag)
                continue
        characters.append(character)
        index += 1
    return "".join(characters)


def apply_migrations(connection, migrations_dir: Path) -> list[str]:
    """Apply pending migrations and return the versions applied in this call.

    The connection is any DB-API connection with cursor(), commit(), and
    rollback(). Each migration and its bookkeeping row commit together.
    """

    migrations = discover_migrations(migrations_dir)
    applied = _fetch_applied(connection)
    completed: list[str] = []

    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise ChecksumMismatch(
                    f"Migration {migration.version} was already applied with checksum "
                    f"{recorded}, but {migration.path.name} now has checksum "
                    f"{migration.checksum}. Applied files must not be edited. "
                    "Add a new migration instead."
                )
            continue
        _apply_one(connection, migration)
        applied[migration.version] = migration.checksum
        completed.append(migration.version)

    return completed


def _fetch_applied(connection) -> dict[str, str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'amlog'
              AND table_name = 'schema_migrations'
            """
        )
        if cursor.fetchone() is None:
            return {}
        cursor.execute("SELECT version, checksum FROM amlog.schema_migrations")
        return {version: checksum for version, checksum in cursor.fetchall()}
    finally:
        cursor.close()


def _apply_one(connection, migration: Migration) -> None:
    statements = split_sql(migration.sql)
    if not statements:
        raise MigrationError(f"Migration {migration.path.name} does not contain any SQL.")

    cursor = connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
        cursor.execute(
            """
            INSERT INTO amlog.schema_migrations (version, name, checksum)
            VALUES (%s, %s, %s)
            """,
            (migration.version, migration.name, migration.checksum),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise MigrationError(
            f"Migration {migration.version}_{migration.name} failed: {exc}"
        ) from exc
    finally:
        cursor.close()


def main(argv: list[str] | None = None) -> int:
    """Apply migrations if this workstation has been given a database URL.

    argv is accepted so the console entry point and tests share one function.
    The foundation has no command-line flags.
    """

    del argv
    settings = load_settings()
    if settings.database_url is None:
        print(
            "AMLOG_DATABASE_URL is not set. No database was contacted and no schema was applied.",
            file=sys.stderr,
        )
        return 2

    try:
        import psycopg
    except ImportError:
        print(
            "The optional psycopg package is not installed. No database was contacted "
            "and no schema was applied.",
            file=sys.stderr,
        )
        return 2

    with psycopg.connect(settings.database_url) as connection:
        completed = apply_migrations(connection, settings.migrations_dir)

    if completed:
        print("Applied migrations: " + ", ".join(completed))
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
