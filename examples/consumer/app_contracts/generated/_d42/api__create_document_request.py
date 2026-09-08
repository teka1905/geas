"""Generated d42-схемы операции ``api.createDocument`` (request).

Файл сгенерирован автоматически. Не редактируйте его руками: правки будут
потеряны при следующей генерации. Источник истины — OpenAPI-спецификация.
"""

from __future__ import annotations

from d42 import optional, schema


GeneratedDocumentDraftSchema = schema.dict({
    "title": schema.str.len(1, 120),
    optional("visibility"): schema.any(
        schema.str("private"),
        schema.str("workspace"),
        schema.str("public"),
    ),
    ...: ...,
})

__all__ = [
    "GeneratedDocumentDraftSchema",
]
