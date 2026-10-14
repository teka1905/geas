"""Generated d42-схемы параметров запроса операции ``api.createDocument``.

Файл сгенерирован автоматически. Не редактируйте его руками: правки будут
потеряны при следующей генерации. Источник истины — OpenAPI-спецификация.
"""

from __future__ import annotations

from d42 import optional, schema
from geas.integrations.d42.serialized import serialized


GeneratedApiCreateDocumentPathParamsSchema = schema.dict({
    "workspaceId": serialized(schema.str.len(1, ...), location="path"),
})

__all__ = [
    "GeneratedApiCreateDocumentPathParamsSchema",
]
