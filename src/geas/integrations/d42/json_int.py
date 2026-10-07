"""Целое в смысле JSON Schema: ``int`` без ``bool``.

В Python ``bool`` — подкласс ``int``, поэтому d42 ``schema.int`` принимает
``True`` и подставляет его через ``%``. JSON Schema ``true`` числом не считает.
У ``integer`` d42-схема так и остаётся мягче контракта — ``bool`` отсекает
JSON-Schema-путь. Целочисленная ветка ``number`` —
``schema.any(schema.float, json_int)`` — отвергает ``bool`` сама: иначе объединение
было бы шире ``number``.

:class:`JsonIntSchema` — подкласс :class:`d42.declaration.types.IntSchema`: всё, что
проверяет ``isinstance(..., IntSchema)`` (overlay'и, проекция фикстур), видит в нём
обычное целое. ``.min()``, ``.max()``, литерал и ``%`` в d42 пересобирают схему
через ``self.__class__``, поэтому запрет ``bool`` переживает любую цепочку вызовов.
"""

from __future__ import annotations

from typing import Any, cast

from d42.declaration import SchemaVisitor, SchemaVisitorReturnType
from d42.declaration.types import IntSchema
from d42.representation import Representor
from d42.validation import Validator
from d42.validation.errors import TypeValidationError
from niltype import Nil

__all__ = ["JsonIntSchema", "json_int"]


class JsonIntSchema(IntSchema):
    """``schema.int``, который не принимает ``bool``."""

    def __accept__(
        self, visitor: SchemaVisitor[SchemaVisitorReturnType], **kwargs: Any
    ) -> SchemaVisitorReturnType:
        """Отвергнуть ``bool`` при проверке; остальное — как у ``schema.int``."""
        if isinstance(visitor, Validator) and isinstance(kwargs.get("value", Nil), bool):
            path = kwargs.get("path", Nil)
            result = visitor.make_validation_result()
            result.add_error(
                TypeValidationError(
                    visitor.make_path() if path is Nil else path, kwargs["value"], int
                )
            )
            return cast(SchemaVisitorReturnType, result)
        represented = visitor.visit_int(self, **kwargs)
        if isinstance(visitor, Representor):
            # Представитель d42 печатает «<имя>.int...»; хвост с литералом и
            # границами тот же, меняется только голова.
            text = cast(str, represented)
            return cast(SchemaVisitorReturnType, "json_int" + text[text.index(".int") + 4 :])
        return represented


#: Аналог ``schema.int`` для целочисленной ветки ``number``.
json_int = JsonIntSchema()
