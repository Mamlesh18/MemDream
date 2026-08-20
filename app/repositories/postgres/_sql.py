"""Row mapping and filter building for the Postgres repositories.

Two small pieces of machinery, kept here so the repositories read as SQL rather
than as string plumbing.

## Why the columns are named after the Python fields

`Memory.memory_id` maps to `memory_id`, `Memory.valid_from` to `valid_from`, and
so on for all thirty-odd fields. That is not a coincidence, it is the whole
mapping layer: a row is built with `getattr` and a model is rebuilt with
`model_validate(dict(record))`, because Pydantic populates by field name.

The alternative — camelCase columns matching the wire format — would have meant
a translation table that has to be kept in step with the models by hand, and the
failure mode of forgetting an entry is a field that silently stops persisting.
Here, adding a field to `Memory` and to the schema is enough; forgetting the
schema half fails loudly at the first insert.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    """``embeddingModel`` -> ``embedding_model``."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def column_value(value: Any) -> Any:
    """Prepare one attribute for asyncpg.

    Enums become their value; everything else asyncpg handles natively — lists
    map to arrays, dicts to JSONB via the codec registered on the pool, and
    aware datetimes to TIMESTAMPTZ.
    """
    if isinstance(value, Enum):
        return value.value
    return value


def to_row(model: BaseModel, columns: tuple[str, ...]) -> list[Any]:
    """Positional values for an INSERT, in column order."""
    return [column_value(getattr(model, column)) for column in columns]


def from_row(record: Any, model_cls: type[T]) -> T:
    """Rebuild a model from an asyncpg record.

    Columns are field names, so validation by name does the whole job. Nothing
    is coerced by hand here — Pydantic already knows every field's type, and a
    hand-written coercion is one more place for the two to drift apart.
    """
    return model_cls.model_validate(dict(record))


def insert_sql(table: str, columns: tuple[str, ...]) -> str:
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"


def upsert_sql(table: str, columns: tuple[str, ...], key: str) -> str:
    """INSERT that overwrites on primary-key conflict.

    Used where the Mongo implementation calls `replace_one`, so a replayed write
    converges on the same row rather than raising.
    """
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns if column != key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {assignments}"
    )


class Filter:
    """Accumulates WHERE clauses and their parameters with correct numbering.

    asyncpg uses positional `$1`, `$2` placeholders, which makes composing a
    filter from optional pieces error-prone by hand — insert a clause in the
    middle and every later number is wrong. Clauses are written here with `?`
    and renumbered on the way in, so the repositories never count placeholders.
    """

    def __init__(self) -> None:
        self._clauses: list[str] = []
        self._params: list[Any] = []

    def add(self, template: str, *values: Any) -> Filter:
        for value in values:
            self._params.append(value)
            template = template.replace("?", f"${len(self._params)}", 1)
        self._clauses.append(template)
        return self

    def bind(self, value: Any) -> str:
        """Register a value outside the WHERE clause (LIMIT, SET, ...) and get
        back the placeholder that refers to it."""
        self._params.append(value)
        return f"${len(self._params)}"

    @property
    def sql(self) -> str:
        return " AND ".join(self._clauses) if self._clauses else "TRUE"

    @property
    def params(self) -> list[Any]:
        return self._params
