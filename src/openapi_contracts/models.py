"""Нейтральное промежуточное представление контракта (IR).

IR — единственный язык, на котором ядро разговаривает само с собой. Диалектные
адаптеры (Swagger 2.0, OpenAPI 3.0.x) обязаны привести документ к этим типам;
ниже по стеку — генератор JSON Schema, конвертер d42, рендер артефактов и runtime —
про исходный диалект уже ничего не знают.

Свойства IR:

* **точность** — в IR нет узла «что угодно», который появлялся бы молча;
  :class:`AnyNode` создаётся только по явному waiver;
* **provenance** — у каждого узла есть :class:`Origin` с источником и JSON Pointer;
* **направленность** — ``readOnly``/``writeOnly`` уже применены, поэтому IR
  строится отдельно для ``request`` и ``response`` и общая исходная схема при этом
  не мутирует;
* **детерминированность** — все коллекции упорядочены (кортежи, отсортированные
  по имени), чтобы сериализация была байт-в-байт воспроизводимой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

__all__ = [
    "STATUS_DEFAULT",
    "AdditionalProperties",
    "AllOfNode",
    "AnyNode",
    "ArrayNode",
    "BodyContract",
    "BooleanNode",
    "Direction",
    "Discriminator",
    "IntegerNode",
    "JsonValue",
    "NullNode",
    "NumberNode",
    "ObjectNode",
    "OperationContract",
    "Origin",
    "ParameterContract",
    "ParameterLocation",
    "PropertySpec",
    "RefNode",
    "RequestContract",
    "ResponseContract",
    "SchemaNode",
    "StringNode",
    "UnionKind",
    "UnionNode",
    "iter_nodes",
]

JsonValue = Union[None, bool, int, float, str, "list[JsonValue]", "dict[str, JsonValue]"]

#: Псевдо-статус для ``responses.default``.
STATUS_DEFAULT = "default"


class Direction(str, Enum):
    """Направление контракта.

    ``readOnly``-поля исключаются из ``REQUEST``, ``writeOnly`` — из ``RESPONSE``.
    """

    REQUEST = "request"
    RESPONSE = "response"


class ParameterLocation(str, Enum):
    """Место параметра в HTTP-запросе."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class UnionKind(str, Enum):
    """Тип композиции-объединения.

    ``ONE_OF`` никогда не заменяется на ``ANY_OF``: эксклюзивность — часть контракта.
    """

    ONE_OF = "oneOf"
    ANY_OF = "anyOf"


class AdditionalProperties(str, Enum):
    """Политика дополнительных ключей объекта, когда она не задана схемой."""

    #: ``additionalProperties`` не указан — OpenAPI разрешает лишние ключи.
    ALLOWED = "allowed"
    #: ``additionalProperties: false``.
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, kw_only=True, slots=True)
class Origin:
    """Происхождение узла: файл-источник и JSON Pointer внутри него."""

    source: str
    pointer: str

    def child(self, *segments: str | int) -> Origin:
        """Указатель на вложенный элемент.

        Экранирование по RFC 6901: ``~`` → ``~0``, ``/`` → ``~1``.
        """
        pointer = self.pointer
        for segment in segments:
            token = str(segment).replace("~", "~0").replace("/", "~1")
            pointer = f"{pointer}/{token}"
        return Origin(source=self.source, pointer=pointer)


@dataclass(frozen=True, kw_only=True, slots=True)
class SchemaNode:
    """Базовый узел IR.

    ``nullable`` вынесен в базу, потому что в OpenAPI 3.0 и Swagger 2.0 это
    ортогональный флаг (``nullable`` / ``x-nullable``), а не отдельный тип.
    """

    origin: Origin
    nullable: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class BooleanNode(SchemaNode):
    """``type: boolean``."""

    enum: tuple[bool, ...] | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class IntegerNode(SchemaNode):
    """``type: integer`` с точными границами."""

    format: str | None = None
    enum: tuple[int, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    exclusive_minimum: int | None = None
    exclusive_maximum: int | None = None
    multiple_of: int | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class NumberNode(SchemaNode):
    """``type: number``."""

    format: str | None = None
    enum: tuple[float, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None
    multiple_of: float | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class StringNode(SchemaNode):
    """``type: string`` вместе с ``format``, ``pattern`` и границами длины."""

    format: str | None = None
    enum: tuple[str, ...] | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class NullNode(SchemaNode):
    """Значение, которое может быть только ``null``."""


@dataclass(frozen=True, kw_only=True, slots=True)
class PropertySpec:
    """Свойство объекта вместе с признаком обязательности."""

    name: str
    schema: SchemaNode
    required: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class ObjectNode(SchemaNode):
    """``type: object``.

    ``properties`` всегда отсортированы по имени — это часть детерминизма артефактов.
    """

    properties: tuple[PropertySpec, ...] = ()
    additional_properties: AdditionalProperties | SchemaNode = AdditionalProperties.ALLOWED
    min_properties: int | None = None
    max_properties: int | None = None

    @property
    def required_names(self) -> tuple[str, ...]:
        """Имена обязательных свойств в стабильном порядке."""
        return tuple(prop.name for prop in self.properties if prop.required)

    def property(self, name: str) -> PropertySpec | None:
        """Найти свойство по имени."""
        for prop in self.properties:
            if prop.name == name:
                return prop
        return None


@dataclass(frozen=True, kw_only=True, slots=True)
class ArrayNode(SchemaNode):
    """``type: array`` с однородными элементами."""

    items: SchemaNode
    min_items: int | None = None
    max_items: int | None = None
    unique_items: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class Discriminator:
    """OpenAPI ``discriminator``.

    В отличие от «подсказки генератору», в JSON Schema он разворачивается в
    настоящие ограничения ``if``/``then``, поэтому теряет семантику только вместе
    с самим контрактом.
    """

    property_name: str
    #: Значение → имя определения в ``definitions`` бандла.
    mapping: tuple[tuple[str, str], ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class UnionNode(SchemaNode):
    """``oneOf`` или ``anyOf``. Вид композиции сохраняется точно."""

    kind: UnionKind
    variants: tuple[SchemaNode, ...]
    discriminator: Discriminator | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class AllOfNode(SchemaNode):
    """``allOf``, который не удалось доказуемо слить в один объект.

    Композиция сохраняется точно. JSON Schema отдаёт её как ``allOf``; d42
    отказывается генерировать такую схему (fail closed), потому что не умеет
    выражать пересечение.
    """

    parts: tuple[SchemaNode, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class RefNode(SchemaNode):
    """Ссылка на именованное определение внутри того же бандла."""

    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class AnyNode(SchemaNode):
    """Узел «что угодно».

    Создаётся **только** по явному waiver и всегда несёт причину, чтобы её было
    видно в generated-артефактах и в semantic diff.
    """

    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ParameterContract:
    """Контракт одного параметра запроса или заголовка ответа."""

    name: str
    location: ParameterLocation
    required: bool
    schema: SchemaNode
    #: Стиль сериализации OpenAPI (``simple``, ``form``, ``spaceDelimited``, ...).
    style: str
    explode: bool
    origin: Origin


@dataclass(frozen=True, kw_only=True, slots=True)
class BodyContract:
    """Контракт тела запроса или ответа для одного content type."""

    content_type: str
    schema: SchemaNode
    required: bool
    origin: Origin


@dataclass(frozen=True, kw_only=True, slots=True)
class RequestContract:
    """Полный контракт запроса операции.

    ``definitions`` изолированы на уровне (операция, направление): waiver или
    direction-specific преобразование общего ``$ref`` не может ослабить этот
    ``$ref`` для других операций.
    """

    path_parameters: tuple[ParameterContract, ...] = ()
    query_parameters: tuple[ParameterContract, ...] = ()
    header_parameters: tuple[ParameterContract, ...] = ()
    cookie_parameters: tuple[ParameterContract, ...] = ()
    bodies: tuple[BodyContract, ...] = ()
    definitions: tuple[tuple[str, SchemaNode], ...] = ()

    def body(self, content_type: str) -> BodyContract | None:
        """Тело запроса для конкретного content type."""
        for item in self.bodies:
            if item.content_type == content_type:
                return item
        return None

    def parameters(self) -> tuple[ParameterContract, ...]:
        """Все параметры запроса в стабильном порядке location → name."""
        return (
            self.path_parameters
            + self.query_parameters
            + self.header_parameters
            + self.cookie_parameters
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class ResponseContract:
    """Контракт одного варианта ответа (статус + content type)."""

    status: int | str
    content_type: str | None
    body: SchemaNode | None
    headers: tuple[ParameterContract, ...] = ()
    definitions: tuple[tuple[str, SchemaNode], ...] = ()
    origin: Origin = field(default_factory=lambda: Origin(source="", pointer=""))


@dataclass(frozen=True, kw_only=True, slots=True)
class OperationContract:
    """Нормализованная операция целиком."""

    key: str
    source: str
    operation_id: str
    method: str
    path: str
    python_path: tuple[str, ...]
    request: RequestContract
    responses: tuple[ResponseContract, ...]
    origin: Origin

    def response(self, status: int | str, content_type: str | None) -> ResponseContract | None:
        """Найти вариант ответа по статусу и content type."""
        for item in self.responses:
            if item.status == status and item.content_type == content_type:
                return item
        return None


def iter_nodes(node: SchemaNode) -> list[SchemaNode]:
    """Обойти дерево узла сверху вниз, включая сам узел.

    ``RefNode`` не разворачивается: обход именованных определений — забота
    вызывающего, иначе на рекурсивных бандлах обход не завершится.
    """
    collected: list[SchemaNode] = [node]
    if isinstance(node, ObjectNode):
        for prop in node.properties:
            collected.extend(iter_nodes(prop.schema))
        if isinstance(node.additional_properties, SchemaNode):
            collected.extend(iter_nodes(node.additional_properties))
    elif isinstance(node, ArrayNode):
        collected.extend(iter_nodes(node.items))
    elif isinstance(node, UnionNode):
        for variant in node.variants:
            collected.extend(iter_nodes(variant))
    elif isinstance(node, AllOfNode):
        for part in node.parts:
            collected.extend(iter_nodes(part))
    return collected
