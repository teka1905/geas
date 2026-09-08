"""Точный d42-контракт для объектов с типизированными дополнительными полями.

Обычный :class:`d42.declaration.types.DictSchema` умеет либо запретить
необъявленные ключи, либо принять их без проверки. OpenAPI дополнительно
разрешает задать схему для каждого такого значения через
``additionalProperties: <schema>``. :class:`TypedDictSchema` заполняет этот
пробел, оставаясь совместимым с ``fake()``, ``%``, ``make_required()`` и
обходом структуры в generator overlays.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from d42.custom_type.visitors import Generator, Representor, Substitutor, Validator
from d42.declaration import GenericSchema, SchemaVisitor, SchemaVisitorReturnType
from d42.declaration.types import DictSchema, Schema, is_absent, is_present, optional
from d42.substitution.errors import SubstitutionError, make_substitution_error
from d42.utils import is_ellipsis
from niltype import Nil

__all__ = ["TypedDictSchema", "typed_dict"]


class TypedDictSchema(DictSchema):
    """Словарь с обычными полями и одной схемой для всех остальных значений."""

    @property
    def additional(self) -> GenericSchema:
        """Схема значения каждого необъявленного ключа."""
        return cast(GenericSchema, self.props.get("additional"))

    def __accept__(
        self, visitor: SchemaVisitor[SchemaVisitorReturnType], **kwargs: Any
    ) -> SchemaVisitorReturnType:
        """Передать управление generic visitor для вызова custom hooks ниже."""
        return cast(SchemaVisitorReturnType, visitor.visit(self, **kwargs))

    def __d42_generate__(self, visitor: Generator, **kwargs: Any) -> Any:
        """Служебный мост к протоколу custom type d42."""
        return self.__generate__(visitor, **kwargs)

    def __d42_validate__(
        self, visitor: Validator, *, value: Any = Nil, path: Any = Nil, **kwargs: Any
    ) -> Any:
        """Служебный мост к протоколу custom type d42."""
        return self.__validate__(
            visitor,
            value=value,
            path=visitor.make_path() if path is Nil else path,
            **kwargs,
        )

    def __d42_substitute__(
        self, visitor: Substitutor, *, value: Any = Nil, **kwargs: Any
    ) -> GenericSchema:
        """Служебный мост к протоколу custom type d42."""
        return self.__substitute__(visitor, value=value, **kwargs)

    def __d42_represent__(self, visitor: Representor, *, indent: int = 0, **kwargs: Any) -> str:
        """Служебный мост к протоколу custom type d42."""
        return self.__represent__(visitor, indent=indent, **kwargs)

    def __generate__(self, visitor: Generator, **kwargs: Any) -> dict[Any, Any]:
        """Сгенерировать объявленные обязательные поля; дополнительные не выдумывать."""
        return cast(dict[Any, Any], DictSchema(self.props).__accept__(visitor, **kwargs))

    def __validate__(
        self,
        visitor: Validator,
        *,
        value: Any,
        path: Any,
        **kwargs: Any,
    ) -> Any:
        """Проверить обычные поля и каждое значение необъявленного ключа."""
        keys = {} if self.props.keys is Nil else dict(self.props.keys)
        open_keys = {**keys, ...: (..., False)}
        result = DictSchema(self.props.update(keys=open_keys)).__accept__(
            visitor, value=value, path=path, **kwargs
        )
        if not isinstance(value, dict):
            return result
        for key, item in value.items():
            if key in keys:
                continue
            nested_path = deepcopy(path)[key]
            nested = self.additional.__accept__(visitor, value=item, path=nested_path, **kwargs)
            result.add_errors(nested.get_errors())
        return result

    def __substitute__(self, visitor: Substitutor, *, value: Any, **kwargs: Any) -> GenericSchema:
        """Закрепить переданные значения, сохранив тип остальных ключей."""
        result = self.__accept__(visitor.validator, value=value)
        if result.has_errors():
            raise make_substitution_error(result, visitor.formatter)
        if not isinstance(value, dict):  # pragma: no cover — отсечено валидатором
            raise SubstitutionError("Ожидался словарь")
        if ... in value:
            raise SubstitutionError("Нельзя подставить многоточие")

        original = {} if self.props.keys is Nil else dict(self.props.keys)
        substituted: dict[Any, tuple[Any, Any]] = {}
        for key, (child, is_optional) in original.items():
            if key not in value:
                substituted[key] = (child, is_optional)
                continue
            item = value[key]
            if is_absent(item):
                if not is_optional:
                    raise SubstitutionError(
                        f"Нельзя использовать optional.absent для обязательного ключа {key!r}"
                    )
                substituted[key] = (child, optional.absent)
            elif is_present(item) or is_ellipsis(item):
                substituted[key] = (child, False)
            else:
                substituted[key] = (child.__accept__(visitor, value=item, **kwargs), False)

        for key, item in value.items():
            if key in original:
                continue
            substituted[key] = (
                ...
                if is_ellipsis(item)
                else self.additional.__accept__(visitor, value=item, **kwargs),
                False,
            )
        return type(self)(self.props.update(keys=substituted))

    def __represent__(self, visitor: Representor, *, indent: int = 0, **kwargs: Any) -> str:
        """Показать обе части контракта, не маскируя тип дополнительных значений."""
        regular = DictSchema(self.props).__accept__(visitor, indent=indent, **kwargs)
        additional = self.additional.__accept__(visitor, indent=indent, **kwargs)
        return f"typed_dict({regular}, additional={additional})"


def typed_dict(contract: DictSchema, *, additional: GenericSchema) -> TypedDictSchema:
    """Добавить к d42-словарю точную схему дополнительных значений."""
    if not isinstance(contract, DictSchema):
        raise TypeError(f"contract должен быть DictSchema, получено {type(contract).__name__}")
    if not isinstance(additional, Schema):
        raise TypeError(f"additional должен быть схемой d42, получено {type(additional).__name__}")
    if contract.props.keys is not Nil and ... in contract.props.keys:
        raise ValueError("contract не должен содержать ...: открытость задаёт typed_dict")
    return TypedDictSchema(contract.props.update(additional=additional))
