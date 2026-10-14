"""Generated d42-схемы параметров запроса операции ``api.listDocuments``.

Файл сгенерирован автоматически. Не редактируйте его руками: правки будут
потеряны при следующей генерации. Источник истины — OpenAPI-спецификация.
"""

from __future__ import annotations

from d42 import optional, schema
from geas.integrations.d42.serialized import serialized


GeneratedApiListDocumentsPathParamsSchema = schema.dict({
    "workspaceId": serialized(schema.str.len(1, ...), location="path"),
})

GeneratedApiListDocumentsQueryParamsSchema = schema.dict({
    optional("limit"): serialized(schema.int.min(1).max(100)),
    ...: ...,
})

__all__ = [
    "GeneratedApiListDocumentsPathParamsSchema",
    "GeneratedApiListDocumentsQueryParamsSchema",
]
