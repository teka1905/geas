"""Generated d42-схемы операции ``api.listDocuments`` (response).

Файл сгенерирован автоматически. Не редактируйте его руками: правки будут
потеряны при следующей генерации. Источник истины — OpenAPI-спецификация.
"""

from __future__ import annotations

from d42 import optional, schema


GeneratedDocumentSchema = schema.dict({
    "id": schema.str.len(1, ...),
    "title": schema.str.len(1, 120),
    "visibility": schema.any(schema.str("private"), schema.str("workspace"), schema.str("public")),
    ...: ...,
})

GeneratedDocumentPageSchema = schema.dict({
    "items": schema.list(GeneratedDocumentSchema).len(1, 3),
    "total": schema.int.min(0),
    ...: ...,
})

__all__ = [
    "GeneratedDocumentPageSchema",
    "GeneratedDocumentSchema",
]
