"""Тесты загрузки спецификаций и разрешения ``$ref``.

Проверяется реестр :class:`SpecRegistry` — единственная точка чтения файлов. От
него зависят три свойства, которые нельзя проверить «на глаз»:

* **граница источника** — ни ``..``, ни symlink, ни абсолютный путь не должны
  вывести чтение за пределы ``sources.<name>.root``;
* **отсутствие сети** — ``https://``-ссылка обязана падать до всякого сокета;
* **чтение ровно один раз** — иначе появляется окно подмены файла между
  проверкой пути и повторным чтением.

Отдельно проверяется относительность межфайловых ссылок: путь внутри фрагмента
разрешается относительно **этого фрагмента**, а не корневого документа.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from geas.errors import RefResolutionError, SpecLoadError
from geas.models import Origin
from geas.normalization.refs import (
    SpecRegistry,
    resolve_json_pointer,
    unescape_pointer_token,
)
from support import FIXTURES, SPECS, make_project, spec

#: Корень многофайлового источника из фикстур.
MULTIFILE = SPECS / "multifile"

#: Указатель «откуда пришла ссылка» — координата, которая обязана попасть в ошибку.
CALLER = Origin(source="root.yaml", pointer="/paths/~1labels/get")


def multifile_registry() -> SpecRegistry:
    """Реестр многофайлового источника с корнем в ``specs/multifile``."""
    return SpecRegistry(source_name="main", entry_path=MULTIFILE / "root.yaml", root=MULTIFILE)


def isolated_registry(root: Path, entry: str = "entry.yaml") -> SpecRegistry:
    """Реестр над каталогом, собранным тестом во временной директории."""
    return SpecRegistry(source_name="main", entry_path=root / entry, root=root)


def write_root_spec(root: Path, ref: str) -> Path:
    """Минимальная спецификация, единственная ссылка которой задана тестом."""
    root.mkdir(parents=True, exist_ok=True)
    entry = root / "entry.yaml"
    entry.write_text(
        "openapi: '3.0.3'\n"
        "info: {title: Runtime fixture, version: '1.0.0'}\n"
        "paths:\n"
        "  /items:\n"
        "    get:\n"
        "      operationId: getItem\n"
        "      responses:\n"
        "        '200':\n"
        "          description: ok\n"
        "          content:\n"
        "            application/json:\n"
        f"              schema:\n                $ref: {ref!r}\n",
        encoding="utf-8",
    )
    return entry


def build_from_fixture(tmp_path: Path, *parts: str) -> Any:
    """Собрать контракты источника, лежащего прямо в каталоге фикстур."""
    project = make_project(tmp_path / "project")
    project.write_manifest(
        sources={"main": {"path": str(spec(*parts)), "selection": "all"}},
    )
    return project.build()


# --------------------------------------------------------------- разрешение


def test_entry_document_is_read_and_identified() -> None:
    """Корневой документ читается, а его идентификатор — путь относительно корня."""
    registry = multifile_registry()
    document = registry.entry_document()

    assert document["openapi"] == "3.0.3"
    assert registry.document_id(registry.entry_path) == "root.yaml"


def test_local_ref_stays_in_the_same_document() -> None:
    """Ссылка без файловой части разрешается внутри того же документа."""
    registry = SpecRegistry(
        source_name="main",
        entry_path=spec("features", "ref_siblings.yaml"),
        root=SPECS / "features",
    )

    resolved = registry.resolve(
        "#/components/schemas/Label", base=registry.entry_path, origin=CALLER
    )

    assert resolved.document_path == registry.entry_path
    assert resolved.pointer == "/components/schemas/Label"
    assert resolved.name == "Label"
    assert resolved.value["required"] == ["name"]
    assert resolved.origin == Origin(
        source="ref_siblings.yaml", pointer="/components/schemas/Label"
    )


def test_cross_file_ref_resolves_to_neighbour_document() -> None:
    """Ссылка на соседний файл даёт значение и путь к документу-цели."""
    registry = multifile_registry()

    resolved = registry.resolve(
        "./schemas/common.yaml#/components/schemas/Label",
        base=registry.entry_path,
        origin=CALLER,
    )

    assert resolved.document_path == MULTIFILE / "schemas" / "common.yaml"
    assert resolved.name == "Label"
    assert resolved.origin.source == "schemas/common.yaml"
    assert sorted(resolved.value["properties"]) == ["color", "detail", "name"]


def test_nested_directory_ref_resolves_from_root() -> None:
    """Ссылка на файл на два уровня глубже разрешается от корневого документа."""
    registry = multifile_registry()

    resolved = registry.resolve(
        "./schemas/deep/more.yaml#/components/schemas/Detail",
        base=registry.entry_path,
        origin=CALLER,
    )

    assert resolved.document_path == MULTIFILE / "schemas" / "deep" / "more.yaml"
    assert resolved.origin.source == "schemas/deep/more.yaml"
    assert resolved.value["required"] == ["id"]


def test_relative_ref_is_resolved_against_the_referencing_document() -> None:
    """``./deep/more.yaml`` внутри ``schemas/common.yaml`` — это ``schemas/deep/more.yaml``."""
    registry = multifile_registry()
    common = MULTIFILE / "schemas" / "common.yaml"

    resolved = registry.resolve(
        "./deep/more.yaml#/components/schemas/Detail", base=common, origin=CALLER
    )

    assert resolved.document_path == MULTIFILE / "schemas" / "deep" / "more.yaml"


def test_relative_ref_is_not_resolved_against_the_entry_document() -> None:
    """Тот же путь от корневого документа указывает в другое место — и не находится.

    Это негативная половина предыдущего теста: если бы база бралась от входного
    файла, ``multifile/deep/more.yaml`` пришлось бы искать там, где его нет.
    """
    registry = multifile_registry()

    with pytest.raises(SpecLoadError) as info:
        registry.resolve(
            "./deep/more.yaml#/components/schemas/Detail",
            base=registry.entry_path,
            origin=CALLER,
        )

    assert "не найден" in str(info.value)
    assert info.value.json_pointer == CALLER.pointer


def test_multifile_source_builds_and_bundles_reachable_definitions(tmp_path: Path) -> None:
    """Сквозная сборка многофайлового источника: обе операции и вложенный ``$ref``."""
    project = make_project(tmp_path / "project")
    for relative in ("root.yaml", "schemas/common.yaml", "schemas/deep/more.yaml"):
        project.write_spec(f"api/{relative}", MULTIFILE / relative)
    project.write_manifest(sources={"main": {"path": "api/root.yaml", "selection": "all"}})

    result = project.build()

    label = result.by_key("main.getLabel").document["responses"][0]["schema"]
    detail = result.by_key("main.getLabelDetail").document["responses"][0]["schema"]
    assert sorted(label["$defs"]) == ["Detail", "Label"]
    assert label["$defs"]["Label"]["properties"]["detail"] == {"$ref": "#/$defs/Detail"}
    assert sorted(detail["$defs"]) == ["Detail"]


# ------------------------------------------------------------ границы корня


def test_ref_escaping_source_root_is_rejected(tmp_path: Path) -> None:
    """``../../../outside.yaml`` уводит за корень источника — и обязан быть отклонён.

    Файл-цель существует специально, поэтому ошибка не может быть «файл не найден».
    """
    assert (FIXTURES / "outside.yaml").is_file()

    with pytest.raises(RefResolutionError) as info:
        build_from_fixture(tmp_path, "multifile", "escape", "root.yaml")

    error = info.value
    assert "уводит за корень источника" in str(error)
    assert "outside.yaml" in str(error)
    assert error.source == "root.yaml"
    assert error.json_pointer == (
        "/paths/~1outside/get/responses/200/content/application~1json/schema"
    )


def test_https_ref_is_rejected_without_touching_the_network(tmp_path: Path) -> None:
    """Сетевая ссылка падает на разборе URL — до любой попытки соединения.

    Автоиспользуемая фикстура ``no_outbound_network`` превратила бы реальный
    выход наружу в отдельную ошибку с другим текстом.
    """
    with pytest.raises(RefResolutionError) as info:
        build_from_fixture(tmp_path, "multifile", "remote", "root.yaml")

    error = info.value
    assert "внешний $ref запрещён" in str(error)
    assert "https://example.invalid/x.yaml#/y" in str(error)
    assert error.source == "root.yaml"
    assert error.json_pointer.endswith("/content/application~1json/schema")


@pytest.mark.parametrize(
    "ref",
    [
        "http://example.invalid/x.yaml#/y",
        "https://example.invalid/x.yaml#/y",
        "file:///etc/hosts#/y",
        "//example.invalid/x.yaml#/y",
    ],
)
def test_refs_with_scheme_or_authority_are_rejected(ref: str) -> None:
    """Любая ссылка со схемой или сетевым адресом отклоняется реестром."""
    registry = multifile_registry()

    with pytest.raises(RefResolutionError):
        registry.resolve(ref, base=registry.entry_path, origin=CALLER)


def test_path_traversal_ref_built_at_runtime_is_rejected(tmp_path: Path) -> None:
    """``..`` в ссылке отсекается по фактическому пути, а не по тексту ссылки."""
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.yaml").write_text(
        "components:\n  schemas:\n    Leak: {type: string}\n", encoding="utf-8"
    )
    write_root_spec(root, "./nested/../../outside/secret.yaml#/components/schemas/Leak")
    registry = isolated_registry(root)

    with pytest.raises(RefResolutionError) as info:
        registry.resolve(
            "./nested/../../outside/secret.yaml#/components/schemas/Leak",
            base=registry.entry_path,
            origin=CALLER,
        )

    assert "уводит за корень источника" in str(info.value)
    assert info.value.json_pointer == CALLER.pointer


def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    """Symlink внутри корня, указывающий наружу, тоже считается выходом за корень.

    Проверка идёт после ``resolve()``, поэтому ссылка не спасает: реестр видит
    реальный путь цели.
    """
    root = tmp_path / "source"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.yaml").write_text(
        "components:\n  schemas:\n    Leak: {type: string}\n", encoding="utf-8"
    )
    try:
        (root / "alias.yaml").symlink_to(outside / "secret.yaml")
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
        pytest.skip(f"symlink недоступен на этой платформе: {exc}")
    write_root_spec(root, "./alias.yaml#/components/schemas/Leak")
    registry = isolated_registry(root)

    with pytest.raises(RefResolutionError) as info:
        registry.resolve(
            "./alias.yaml#/components/schemas/Leak",
            base=registry.entry_path,
            origin=CALLER,
        )

    error = info.value
    assert "уводит за корень источника" in str(error)
    # В сообщении стоит именно реальная цель, а не имя ссылки: иначе непонятно,
    # куда на самом деле смотрит symlink.
    assert "secret.yaml" in str(error)
    assert "alias.yaml" not in str(error)


def test_symlink_pointing_inside_root_is_allowed(tmp_path: Path) -> None:
    """Отвергается выход за корень, а не сам факт symlink."""
    root = tmp_path / "source"
    (root / "real").mkdir(parents=True)
    (root / "real" / "fragment.yaml").write_text(
        "components:\n  schemas:\n    Inside: {type: string}\n", encoding="utf-8"
    )
    try:
        (root / "alias.yaml").symlink_to(root / "real" / "fragment.yaml")
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
        pytest.skip(f"symlink недоступен на этой платформе: {exc}")
    write_root_spec(root, "./alias.yaml#/components/schemas/Inside")
    registry = isolated_registry(root)

    resolved = registry.resolve(
        "./alias.yaml#/components/schemas/Inside", base=registry.entry_path, origin=CALLER
    )

    assert resolved.value == {"type": "string"}
    # Документ учтён под реальным путём — иначе один и тот же файл читался бы дважды.
    assert resolved.origin.source == "real/fragment.yaml"


def test_ref_to_directory_is_rejected(tmp_path: Path) -> None:
    """Каталог — не обычный файл, читать его нельзя."""
    registry = multifile_registry()

    with pytest.raises(SpecLoadError) as info:
        registry.resolve("./schemas#/components", base=registry.entry_path, origin=CALLER)

    assert "не является обычным файлом" in str(info.value)


def test_empty_ref_is_rejected() -> None:
    """Пустой ``$ref`` — это ошибка, а не ссылка на текущий документ."""
    registry = multifile_registry()

    with pytest.raises(RefResolutionError) as info:
        registry.resolve("", base=registry.entry_path, origin=CALLER)

    assert "непустой строкой" in str(info.value)


# ------------------------------------------------------------- JSON Pointer


def test_broken_json_pointer_names_the_missing_step() -> None:
    """Битый указатель называет первый шаг, которого нет в документе."""
    registry = multifile_registry()

    with pytest.raises(RefResolutionError) as info:
        registry.resolve("#/components/schemas/Missing", base=registry.entry_path, origin=CALLER)

    error = info.value
    assert "в документе нет '/components'" in str(error)
    assert error.source == CALLER.source
    assert error.json_pointer == CALLER.pointer


def test_pointer_into_scalar_is_rejected() -> None:
    """Указатель, упирающийся в скаляр, не молчит, а объясняет, где остановился."""
    registry = multifile_registry()

    with pytest.raises(RefResolutionError) as info:
        registry.resolve("#/openapi/nested", base=registry.entry_path, origin=CALLER)

    assert "упирается в скаляр" in str(info.value)


def test_pointer_must_start_with_slash() -> None:
    """Относительный указатель без ведущего ``/`` не принимается."""
    with pytest.raises(RefResolutionError) as info:
        resolve_json_pointer({"a": 1}, "a", origin=CALLER)

    assert "должен начинаться с '/'" in str(info.value)


def test_empty_pointer_addresses_the_whole_document() -> None:
    """Пустой указатель — это весь документ."""
    document = {"a": 1}

    assert resolve_json_pointer(document, "", origin=CALLER) is document


def test_pointer_walks_into_arrays_by_index() -> None:
    """Числовой сегмент — это индекс элемента массива."""
    registry = multifile_registry()

    resolved = registry.resolve(
        "#/paths/~1labels~1{labelId}/get/parameters/0",
        base=registry.entry_path,
        origin=CALLER,
    )

    assert resolved.value["name"] == "labelId"


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/paths/~1labels~1{labelId}/get/parameters/9", "вне массива"),
        ("/paths/~1labels~1{labelId}/get/parameters/-1", "вне массива"),
        ("/paths/~1labels~1{labelId}/get/parameters/first", "не индекс массива"),
    ],
)
def test_bad_array_pointers_are_rejected(pointer: str, expected: str) -> None:
    """Несуществующий индекс и нечисловой сегмент внутри массива — ошибки."""
    registry = multifile_registry()

    with pytest.raises(RefResolutionError) as info:
        registry.resolve(f"#{pointer}", base=registry.entry_path, origin=CALLER)

    assert expected in str(info.value)


def test_pointer_tokens_are_unescaped_by_rfc_6901() -> None:
    """``~1`` — это ``/``, ``~0`` — это ``~``; порядок замен важен."""
    assert unescape_pointer_token("a~1b") == "a/b"
    assert unescape_pointer_token("a~0b") == "a~b"
    assert unescape_pointer_token("~01") == "~1"

    document = {"a~b": {"c/d": 42}}
    assert resolve_json_pointer(document, "/a~0b/c~1d", origin=CALLER) == 42


# ---------------------------------------------------------- соседи у $ref


def test_ref_siblings_readonly_and_nullable_are_accepted(tmp_path: Path) -> None:
    """``readOnly`` и ``nullable`` рядом с ``$ref`` библиотека применяет сама."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "ref_siblings.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.legal": {"source": "main", "operation_id": "getLegalCard"}},
    )

    schema = project.build().by_key("api.legal").document["responses"][0]["schema"]

    assert schema["properties"]["label"] == {"$ref": "#/$defs/Label"}
    assert schema["properties"]["owner"] == {
        "anyOf": [{"$ref": "#/$defs/Member"}, {"type": "null"}]
    }


def test_semantic_ref_sibling_is_rejected(tmp_path: Path) -> None:
    """``minLength`` рядом с ``$ref`` OpenAPI 3.0 игнорирует — значит генерация падает."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "ref_siblings.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.illegal": {"source": "main", "operation_id": "getIllegalCard"}},
    )

    with pytest.raises(RefResolutionError) as info:
        project.build()

    error = info.value
    assert "minLength" in str(error)
    assert "молча игнорирует" in str(error)
    assert error.json_pointer.endswith("/schema/properties/label")


def test_ref_sibling_check_lists_every_offending_key() -> None:
    """Проверка соседей перечисляет все лишние ключи разом, а не первый попавшийся."""
    with pytest.raises(RefResolutionError) as info:
        SpecRegistry.check_ref_siblings(
            {"$ref": "#/x", "minLength": 1, "pattern": "^a$", "description": "ok"},
            origin=CALLER,
        )

    assert "['minLength', 'pattern']" in str(info.value)


# ------------------------------------------------------------ чтение файлов


def test_each_file_is_read_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторные ссылки на тот же файл берутся из реестра, а не с диска.

    Это не оптимизация: повторное чтение открывало бы окно, в котором файл можно
    подменить между проверкой пути и разбором содержимого.
    """
    reads: list[Path] = []
    original = SpecRegistry._read_bytes

    def counting(self: SpecRegistry, path: Path, *, origin: Origin | None = None) -> bytes:
        reads.append(path)
        return original(self, path, origin=origin)

    monkeypatch.setattr(SpecRegistry, "_read_bytes", counting)

    registry = multifile_registry()
    registry.entry_document()
    for _ in range(3):
        registry.resolve(
            "./schemas/common.yaml#/components/schemas/Label",
            base=registry.entry_path,
            origin=CALLER,
        )
        registry.resolve(
            "./deep/more.yaml#/components/schemas/Detail",
            base=MULTIFILE / "schemas" / "common.yaml",
            origin=CALLER,
        )
        registry.entry_document()

    assert sorted(path.name for path in reads) == ["common.yaml", "more.yaml", "root.yaml"]


def test_repeated_lookup_returns_the_same_document_object() -> None:
    """Один и тот же файл отдаётся тем же объектом — доказательство единственного чтения."""
    registry = multifile_registry()
    common = MULTIFILE / "schemas" / "common.yaml"

    first = registry.document(common, origin=CALLER)
    second = registry.resolve(
        "./schemas/common.yaml#/components/schemas/Label",
        base=registry.entry_path,
        origin=CALLER,
    )

    assert registry.document(common, origin=CALLER) is first
    assert second.value is first["components"]["schemas"]["Label"]


def test_document_root_must_be_a_mapping(tmp_path: Path) -> None:
    """Документ, корень которого не объект, не принимается."""
    root = tmp_path / "source"
    root.mkdir(parents=True)
    write_root_spec(root, "./list.yaml#/0")
    (root / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    registry = isolated_registry(root)

    with pytest.raises(SpecLoadError) as info:
        registry.resolve("./list.yaml#/0", base=registry.entry_path, origin=CALLER)

    assert "корень спецификации должен быть объектом" in str(info.value)
    assert info.value.json_pointer == CALLER.pointer


def test_unparsable_referenced_file_reports_the_referring_pointer(tmp_path: Path) -> None:
    """Битый YAML цели называет указатель ссылки, которая его притащила."""
    root = tmp_path / "source"
    root.mkdir(parents=True)
    write_root_spec(root, "./broken.yaml#/x")
    (root / "broken.yaml").write_text("a: [1, 2\nb: {", encoding="utf-8")
    registry = isolated_registry(root)

    with pytest.raises(SpecLoadError) as info:
        registry.resolve("./broken.yaml#/x", base=registry.entry_path, origin=CALLER)

    assert "не разбирается" in str(info.value)
    assert info.value.json_pointer == CALLER.pointer
