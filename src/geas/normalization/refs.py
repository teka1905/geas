"""Загрузка спецификаций и разрешение ``$ref``.

Правила безопасности, из которых нельзя выйти без изменения кода:

* внешние HTTP(S)/file `$ref` запрещены всегда — сеть не используется вообще;
* межфайловые локальные ``$ref`` разрешены только внутри явно заданного корня
  источника (``sources.<name>.root`` в manifest);
* путь проверяется после ``resolve()``, поэтому symlink, уводящий за корень,
  отсекается вместе с ``..``;
* окно TOCTOU на последнем компоненте пути закрыто ``O_NOFOLLOW`` + сверкой
  ``st_dev``/``st_ino``;
* каждый файл читается ровно один раз, дальше работает in-memory реестр —
  подмена файла между проверкой и повторным чтением невозможна.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from ..errors import RefResolutionError, SpecLoadError
from ..models import Origin

__all__ = [
    "EXPANDED_KEY",
    "RECURSION_KEY",
    "ResolvedRef",
    "SpecRegistry",
    "expand_refs",
    "resolve_json_pointer",
    "unescape_pointer_token",
]

#: Маркер цикла в развёрнутом фрагменте: значение — индекс цели в стеке развёртки.
RECURSION_KEY = "$recursion"
#: Ключ для цели ``$ref``, которая не является объектом и не сливается с соседями.
EXPANDED_KEY = "$expanded"

#: Ключи, которые допускается видеть рядом с ``$ref``: они всё равно отбрасываются
#: как несемантические. Любой другой сосед ``$ref`` — ошибка, потому что OpenAPI 3.0
#: молча его игнорирует, а мы не имеем права терять контракт молча.
_ALLOWED_REF_SIBLINGS = frozenset({"description", "title", "example", "summary"})

_MAX_DOCUMENTS = 256


def unescape_pointer_token(token: str) -> str:
    """Раскодировать сегмент JSON Pointer по RFC 6901."""
    return token.replace("~1", "/").replace("~0", "~")


def resolve_json_pointer(document: Any, pointer: str, *, origin: Origin) -> Any:
    """Пройти по JSON Pointer внутри уже загруженного документа."""
    if pointer in ("", "/"):
        # Пустой указатель и одиночный слэш одинаково означают корень документа.
        return document
    if not pointer.startswith("/"):
        raise RefResolutionError(
            f"JSON Pointer {pointer!r} должен начинаться с '/'",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    current = document
    walked = ""
    for raw_token in pointer[1:].split("/"):
        token = unescape_pointer_token(raw_token)
        walked = f"{walked}/{raw_token}"
        if isinstance(current, dict):
            if token not in current:
                raise RefResolutionError(
                    f"$ref не разрешается: в документе нет {walked!r}",
                    source=origin.source,
                    json_pointer=origin.pointer,
                )
            current = current[token]
        elif isinstance(current, list):
            if not token.lstrip("-").isdigit():
                raise RefResolutionError(
                    f"$ref не разрешается: {walked!r} — не индекс массива",
                    source=origin.source,
                    json_pointer=origin.pointer,
                )
            index = int(token)
            if index < 0 or index >= len(current):
                raise RefResolutionError(
                    f"$ref не разрешается: индекс {walked!r} вне массива",
                    source=origin.source,
                    json_pointer=origin.pointer,
                )
            current = current[index]
        else:
            raise RefResolutionError(
                f"$ref не разрешается: {walked!r} упирается в скаляр",
                source=origin.source,
                json_pointer=origin.pointer,
            )
    return current


@dataclass(frozen=True, kw_only=True, slots=True)
class ResolvedRef:
    """Результат разрешения ``$ref``."""

    value: Any
    #: Документ, в котором лежит цель (для дальнейших относительных ``$ref``).
    document_path: Path
    #: JSON Pointer цели внутри её документа.
    pointer: str
    #: Стабильное имя цели: последний сегмент указателя (плюс префикс файла при коллизии).
    name: str | None
    origin: Origin


class SpecRegistry:
    """In-memory реестр документов одного источника.

    Реестр — единственная точка чтения файлов. Все ``$ref`` разрешаются через него,
    поэтому политику «читаем один раз, только внутри корня, без сети» невозможно
    обойти из вызывающего кода.
    """

    def __init__(self, *, source_name: str, entry_path: Path, root: Path) -> None:
        self.source_name = source_name
        self.root = root.resolve()
        self.entry_path = self._validate_path(entry_path.resolve(), origin=None)
        self._documents: dict[Path, Any] = {}
        self._document_ids: dict[Path, str] = {}

    # ---------------------------------------------------------------- загрузка

    def _validate_path(self, path: Path, *, origin: Origin | None) -> Path:
        """Проверить, что путь лежит внутри корня и указывает на обычный файл."""
        source = origin.source if origin else self.source_name
        pointer = origin.pointer if origin else None
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SpecLoadError(
                f"файл спецификации не найден: {path} ({exc.strerror})",
                source=source,
                json_pointer=pointer,
            ) from exc
        if not resolved.is_relative_to(self.root):
            raise RefResolutionError(
                f"$ref уводит за корень источника: {resolved} не внутри {self.root}. "
                f"Расширьте sources.<name>.root, если это действительно нужно",
                source=source,
                json_pointer=pointer,
            )
        if not resolved.is_file():
            raise SpecLoadError(
                f"{resolved} не является обычным файлом", source=source, json_pointer=pointer
            )
        return resolved

    def _read_bytes(self, path: Path, *, origin: Origin | None) -> bytes:
        """Прочитать файл, закрыв окно подмены последнего компонента пути."""
        source = origin.source if origin else self.source_name
        try:
            expected = os.lstat(path)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise SpecLoadError(
                f"не удалось открыть {path}: {exc.strerror}",
                source=source,
                json_pointer=origin.pointer if origin else None,
            ) from exc
        try:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise SpecLoadError(
                    f"файл {path} подменён во время чтения",
                    source=source,
                    json_pointer=origin.pointer if origin else None,
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def document(self, path: Path, *, origin: Origin | None = None) -> Any:
        """Получить разобранный документ, прочитав файл не более одного раза."""
        resolved = self._validate_path(path, origin=origin)
        if resolved in self._documents:
            return self._documents[resolved]
        if len(self._documents) >= _MAX_DOCUMENTS:
            raise SpecLoadError(
                f"источник {self.source_name!r} тянет больше {_MAX_DOCUMENTS} файлов — "
                f"похоже на цикл или на слишком широкий root",
                source=self.source_name,
            )
        raw = self._read_bytes(resolved, origin=origin)
        # Файл читается по чьей-то ссылке, поэтому указатель на неё — часть
        # диагностики: без него непонятно, какой $ref притащил битый документ.
        pointer = origin.pointer if origin else None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpecLoadError(
                f"{resolved} не в UTF-8: {exc}", source=self.source_name, json_pointer=pointer
            ) from exc
        try:
            if resolved.suffix.lower() == ".json":
                document = json.loads(text)
            else:
                document = yaml.safe_load(text)
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            raise SpecLoadError(
                f"{resolved} не разбирается: {exc}", source=self.source_name, json_pointer=pointer
            ) from exc
        if not isinstance(document, dict):
            raise SpecLoadError(
                f"{resolved}: корень спецификации должен быть объектом",
                source=self.source_name,
                json_pointer=pointer,
            )
        self._documents[resolved] = document
        self._document_ids[resolved] = self._document_id(resolved)
        return document

    def _document_id(self, path: Path) -> str:
        """Стабильный идентификатор документа: путь относительно корня, через '/'."""
        return path.relative_to(self.root).as_posix()

    def document_id(self, path: Path) -> str:
        """Идентификатор уже загруженного документа."""
        return self._document_ids.get(path) or self._document_id(path)

    def entry_document(self) -> Any:
        """Корневой документ источника."""
        return self.document(self.entry_path)

    # ------------------------------------------------------------- разрешение

    def resolve(self, ref: str, *, base: Path, origin: Origin) -> ResolvedRef:
        """Разрешить ``$ref`` относительно документа ``base``."""
        if not isinstance(ref, str) or not ref:
            raise RefResolutionError(
                f"$ref должен быть непустой строкой, получено {ref!r}",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        split = urlsplit(ref)
        if split.scheme:
            raise RefResolutionError(
                f"внешний $ref запрещён: {ref!r}. Библиотека никогда не ходит в сеть; "
                f"положите документ рядом и сошлитесь относительным путём",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        if split.netloc:
            raise RefResolutionError(
                f"$ref с сетевым адресом запрещён: {ref!r}",
                source=origin.source,
                json_pointer=origin.pointer,
            )

        file_part = unquote(split.path)
        pointer = "/" + unquote(split.fragment).lstrip("/") if split.fragment else ""

        if file_part:
            target_path = (base.parent / file_part).resolve()
            document = self.document(target_path, origin=origin)
        else:
            target_path = base
            document = self.document(base, origin=origin)

        value = resolve_json_pointer(document, pointer, origin=origin)
        name = None
        if pointer:
            name = unescape_pointer_token(pointer.rsplit("/", 1)[-1])
        return ResolvedRef(
            value=value,
            document_path=target_path,
            pointer=pointer,
            name=name,
            origin=Origin(source=self.document_id(target_path), pointer=pointer),
        )

    @staticmethod
    def check_ref_siblings(node: dict[str, Any], *, origin: Origin) -> None:
        """Убедиться, что рядом с ``$ref`` нет молча игнорируемых ключей."""
        extra = sorted(set(node) - {"$ref"} - _ALLOWED_REF_SIBLINGS)
        if extra:
            raise RefResolutionError(
                f"рядом с $ref есть ключи {extra}, которые OpenAPI 3.0 молча игнорирует. "
                f"Уберите их или разверните $ref вручную",
                source=origin.source,
                json_pointer=origin.pointer,
            )


def expand_refs(
    registry: SpecRegistry,
    node: Any,
    *,
    base: Path,
    origin: Origin,
    max_depth: int,
) -> Any:
    """Развернуть все ``$ref`` внутри фрагмента в одно самодостаточное дерево.

    Нужно там, где отпечаток обязан зависеть от **содержимого** поддерева, а не
    от текста ссылок: сам по себе ``{"$ref": "#/definitions/Thing"}`` не меняется,
    когда ``Thing`` переписали, и waiver на такое поддерево молча пережил бы
    правку контракта.

    Развёртка сохраняет семантику, которой пользуется нормализация:

    * цель ``$ref`` подставляется на место узла, а ключи-соседи (``readOnly``,
      ``writeOnly``, nullable-маркер, аннотации) накладываются поверх неё — ровно
      так их и читает :mod:`geas.normalization.schemas`;
    * имя цели в результат не попадает, поэтому переименование схемы или вынос
      фрагмента в ``$ref`` и обратно отпечаток не меняют;
    * цикл не разворачивается бесконечно: повторно встреченная цель заменяется
      маркером :data:`RECURSION_KEY` с индексом в текущем стеке — он стабилен,
      пока стабильна форма цикла.

    Порядок ключей значения не имеет: отпечаток считается по канонической
    сериализации с сортировкой ключей.
    """

    def walk(value: Any, base: Path, origin: Origin, stack: tuple[Any, ...], depth: int) -> Any:
        if depth > max_depth:
            raise RefResolutionError(
                f"глубина развёртки $ref превысила {max_depth} — похоже на рекурсию, "
                f"собранную в обход $ref",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        if isinstance(value, list):
            return [walk(item, base, origin, stack, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        siblings = {
            key: walk(item, base, origin, stack, depth + 1)
            for key, item in value.items()
            if key != "$ref"
        }
        if not isinstance(ref, str):
            return siblings
        resolved = registry.resolve(ref, base=base, origin=origin)
        marker = (registry.document_id(resolved.document_path), resolved.pointer)
        if marker in stack:
            return {**siblings, RECURSION_KEY: stack.index(marker)}
        target = walk(
            resolved.value,
            resolved.document_path,
            resolved.origin,
            (*stack, marker),
            depth + 1,
        )
        if not isinstance(target, dict):
            # ``$ref`` на не-объект нормализация всё равно отвергнет; отпечатку
            # достаточно сохранить значение, не притворяясь Schema Object.
            return {**siblings, EXPANDED_KEY: target}
        return {**target, **siblings}

    return walk(node, base, origin, (), 0)
