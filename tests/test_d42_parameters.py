"""d42-схемы параметров запроса: строковый вид значений, генерация и runtime.

Главное свойство, которое здесь закрепляется: d42-схема места запроса описывает
значения ровно в том виде, в каком они уходят в запрос. Отсюда три проверки,
которые повторяются на разных формах параметров:

* ``fake()`` даёт значения, которые проходят проверку перехваченного запроса по
  контракту (``validate_recorded_request``) — тем же правилом разбора строк;
* ``%`` принимает и типизированное значение (``42``, ``True``), и его строковый вид;
* невыразимое место (сериализация по waiver'у, заголовки с разным регистром)
  отказывает отдельно, с причиной в ``geas update``, не задевая соседние места.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import pytest
from d42 import fake, optional, schema, validate_or_fail
from d42.utils import make_required

from geas import Direction
from geas.errors import (
    ContractMockError,
    ContractOverlayError,
    OperationLookupError,
    RequestContractError,
    ValidationFailedError,
)
from geas.fingerprints import semantic_source_digest
from geas.integrations.d42 import EACH, build_fixture, overlay_generators, serialized
from geas.integrations.d42.serialized import SerializedSchema
from geas.models import ParameterLocation
from support import Project, make_project, spec

INT64 = serialized(schema.int.min(-(2**63)).max(2**63 - 1))

#: Waiver, который пропускает deepObject над скаляром через нормализацию.
DEEP_OBJECT_WAIVER: dict[str, Any] = {
    "operation": "main.searchDeep",
    "direction": "request",
    "json_pointer": "/param/query/filter",
    "rule": "allow_unsupported_serialization",
    "reason": "клиент шлёт filter без сериализации deepObject",
    "owner": "team-api",
    "issue": "BUG-7",
    "expires_at": dt.date.today() + dt.timedelta(days=30),
    "expected_source": semantic_source_digest({"type": "string"}),
}


def parameters_project(
    root: Path, *, operations: dict[str, Any] | None = None, package: str = "demo_contracts"
) -> Project:
    """Проект над фикстурой ``parameters_d42.yaml`` со всеми её операциями.

    Пакет модульной фикстуры :func:`generated` остаётся импортированным до конца
    модуля, поэтому тесты со своим проектом берут другое имя пакета.
    """
    project = make_project(root, package=package)
    project.write_spec("api/spec.yaml", spec("features", "parameters_d42.yaml"))
    project.write_waivers([DEEP_OBJECT_WAIVER])
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "all"}},
        operations=operations,
    )
    return project


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Generated-пакет фикстуры вместе с проектом, из которого он собран."""
    project = parameters_project(tmp_path_factory.mktemp("parameters"), package="params_shared")
    project.update()
    with project.importable() as module:
        yield project, module.operations


# ------------------------------------------------------------ serialized()


def test_integer_value_is_a_decimal_string_within_bounds() -> None:
    """Строка проверяется правилом разбора запроса, границы — вложенной схемой."""
    assert INT64 == "42"
    assert INT64 == "-42"
    # Ведущие нули разбор запроса принимает, значит и схема обязана их принять.
    assert INT64 == "007"
    assert str(2**63 - 1) == INT64
    assert str(2**63) != INT64
    assert INT64 != "4.2"
    assert INT64 != "abc"
    # Значение параметра на проводе — строка: число не подходит.
    assert INT64 != 42


def test_integer_fake_stays_inside_the_declared_range() -> None:
    """Голая регулярка ``^-?\\d+$`` выдавала бы строки длиннее int64; здесь — нет."""
    positive = serialized(schema.int.min(1).max(2**31 - 1))
    random.seed(606)
    for _ in range(500):
        assert -(2**63) <= int(fake(INT64)) <= 2**63 - 1
        assert 1 <= int(fake(positive)) <= 2**31 - 1


def test_boolean_is_true_or_false_in_lowercase() -> None:
    flag = serialized(schema.bool)
    assert flag == "true"
    assert flag == "false"
    assert flag != "True"
    assert flag != "1"
    random.seed(1)
    assert {fake(flag) for _ in range(50)} == {"true", "false"}


def test_number_accepts_the_notations_the_request_check_accepts() -> None:
    ratio = serialized(schema.float.min(0.0).max(1.0))
    for text in ("0", "1", "0.5", ".5", "1e-1", "1.0"):
        assert text == ratio, text
    assert ratio != "2"
    assert ratio != "nan"
    random.seed(2)
    for _ in range(100):
        assert 0.0 <= float(fake(ratio)) <= 1.0


def test_typed_substitution_pins_the_value_and_accepts_any_notation() -> None:
    """``% 42`` закрепляет значение: ``"042"`` — то же число, а ``fake()`` пишет ``"42"``."""
    pinned = INT64 % 42
    assert isinstance(pinned, SerializedSchema)
    assert pinned == "42"
    assert pinned == "042"
    assert pinned != "43"
    assert fake(pinned) == "42"

    number = serialized(schema.float) % 1
    # Клиент на JS пишет 1.0 как "1": закрепление значением этого не ломает.
    assert number == "1"
    assert number == "1.0"
    assert serialized(schema.bool) % True == serialized(schema.bool(True))


def test_string_substitution_pins_the_exact_wire_text() -> None:
    pinned = INT64 % "042"
    assert pinned == schema.str("042")
    assert pinned == "042"
    assert pinned != "42"


@pytest.mark.parametrize(
    "value, message",
    [
        (True, "must be <class 'int'>"),
        ("x", "written as an integer in decimal notation"),
        (str(2**63), "less than or equal to"),
        (4.2, "must be <class 'int'>"),
    ],
)
def test_substitution_rejects_values_outside_the_contract(value: Any, message: str) -> None:
    """``True`` — подкласс ``int`` в Python, но в запрос он ушёл бы строкой ``"true"``."""
    with pytest.raises(Exception) as info:
        schema.dict({"id": INT64}) % {"id": value}
    assert message in str(info.value)


def test_delimited_array_is_split_and_checked_item_by_item() -> None:
    ids = serialized(schema.list(schema.int.min(1)).len(1, 3), delimiter=",")
    assert ids == "1,2,3"
    assert ids == "7"
    assert ids != "1,2,3,4"
    assert ids != "1,,3"
    # Пустая строка — это один пустой элемент, а не пустой массив: как при разборе запроса.
    assert ids != ""
    assert ids != "0"

    pinned = ids % [1, 2]
    assert pinned == "1,2"
    assert pinned != "1,3"
    assert fake(pinned) == "1,2"
    assert ids % "3,2" == schema.str("3,2")


def test_space_delimited_strings_are_generated_without_breaking_the_split() -> None:
    """В алфавите d42 есть пробел: генерация отбрасывает строки, которые разобрались бы иначе."""
    tags = serialized(schema.list(schema.str.len(1, 8)).len(1, 3), delimiter=" ")
    random.seed(3)
    for _ in range(100):
        value = fake(tags)
        assert value == tags
        assert 1 <= len(value.split(" ")) <= 3


def test_delimited_generation_gives_up_loudly_when_no_value_fits() -> None:
    impossible = serialized(schema.list(schema.str(" ")).len(1), delimiter=" ")
    with pytest.raises(ValidationFailedError, match="разбирается в другой массив"):
        fake(impossible)


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda: serialized(schema.dict), "нет строкового вида"),
        (lambda: serialized(schema.list(schema.int)), "нет строкового вида"),
        (lambda: serialized(schema.int | schema.str), "разных типов"),
        (lambda: serialized(schema.int, delimiter=","), "schema.list"),
        (lambda: serialized(schema.list(schema.int), delimiter=""), "непустой строкой"),
        (lambda: serialized("int"), "схемой d42"),  # type: ignore[arg-type]
        (lambda: serialized(schema.int | schema.none), "у null"),
        (lambda: serialized(schema.str, location="query"), "не ограничивает текст"),
    ],
)
def test_serialized_rejects_schemas_without_a_string_form(call: Any, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        call()


def test_representation_names_the_wrapper_and_the_typed_contract() -> None:
    assert repr(INT64) == (
        "serialized(schema.int.min(-9223372036854775808).max(9223372036854775807))"
    )
    ids = serialized(schema.list(schema.int).len(1, ...), delimiter="|")
    assert repr(ids) == "serialized(schema.list(schema.int).len(1, ...), delimiter='|')"


@pytest.mark.parametrize(
    "location, rejected, accepted",
    [
        ("path", ["", "a/b", "/"], ["a", " a b ", "a%2Fb"]),
        ("header", [" ab", "ab ", "\tab"], ["a b", ""]),
        ("cookie", [" ab", "ab ", "a;b"], ["a b", "a=b"]),
    ],
)
def test_location_rejects_text_the_transport_would_change(
    location: str, rejected: list[str], accepted: list[str]
) -> None:
    """Разобранный запрос таких строк не содержит: проверка не уже контракта."""
    value = serialized(schema.str, location=location)
    for text in rejected:
        assert text != value, text
        with pytest.raises(Exception, match="must be a request parameter"):
            schema.dict({"v": value}) % {"v": text}
    for text in accepted:
        assert text == value, text


def test_location_keeps_fake_values_intact_in_transit() -> None:
    random.seed(4)
    code = serialized(schema.str.regex("^[a-z]*$"), location="path")
    token = serialized(schema.str.regex("^.{4}$"), location="cookie")
    label = serialized(schema.str.len(2, ...), location="header")
    for _ in range(300):
        assert fake(code) != ""
        value = fake(token)
        assert ";" not in value
        assert value == value.strip()
        text = fake(label)
        assert text == text.strip()
        assert len(text) >= 2


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda: serialized(schema.float) % float("inf"), "a finite number"),
        (lambda: serialized(schema.list(schema.int), delimiter=",") % [], "non-empty list"),
    ],
)
def test_substitution_rejects_values_without_a_string_form(call: Any, message: str) -> None:
    with pytest.raises(Exception, match=message):
        call()


def test_make_required_works_on_a_parameter_dictionary() -> None:
    params = schema.dict({"id": INT64, optional("flag"): serialized(schema.bool), ...: ...})
    required = make_required(params)
    assert required == {"id": "1", "flag": "true"}
    assert required != {"id": "1"}


def test_overlay_replaces_the_generator_of_the_typed_value() -> None:
    params = schema.dict(
        {
            "id": INT64,
            "ids": serialized(schema.list(schema.int).len(1, ...), delimiter=","),
        }
    )
    overlaid = overlay_generators(
        params, {("id",): schema.int.min(1).max(9), ("ids", EACH): schema.int.min(5).max(5)}
    )
    fixture = build_fixture(overlaid)
    assert 1 <= int(fixture["id"]) <= 9
    assert set(fixture["ids"].split(",")) == {"5"}
    assert fixture == params

    with pytest.raises(ContractOverlayError, match="тип ручной схемы"):
        overlay_generators(params, {("id",): schema.str})
    with pytest.raises(ContractOverlayError, match="разделитель"):
        overlay_generators(params, {("ids",): serialized(schema.list(schema.int), delimiter="|")})
    bounded = schema.dict({"id": serialized(schema.int.min(1).max(9))})
    with pytest.raises(ContractOverlayError, match="нарушает"):
        build_fixture(overlay_generators(bounded, {("id",): schema.int.min(50).max(50)}))


def test_build_fixture_is_deterministic_for_parameter_schemas() -> None:
    params = schema.dict(
        {"action": schema.any(schema.str("A"), schema.str("B")), "id": INT64, ...: ...}
    )
    assert build_fixture(params, seed=7) == build_fixture(params, seed=7)
    assert build_fixture(params, seed=7)["action"] == "A"


def test_inexpressible_value_refuses_its_location_with_the_converter_reason() -> None:
    """``multipleOf`` в d42 не выражается ни в теле, ни в параметре: место отказывает."""
    from geas.errors import UnsupportedConstructError
    from geas.integrations.d42.converter import plan_parameters
    from geas.integrations.d42.renderer import render_parameters_module
    from geas.models import IntegerNode, Origin, ParameterContract

    origin = Origin(source="main", pointer="/paths/~1items/get/parameters/0")
    step = ParameterContract(
        name="step",
        location=ParameterLocation.QUERY,
        required=True,
        schema=IntegerNode(origin=origin, multiple_of=5),
        style="form",
        explode=True,
        origin=origin,
    )
    identifier = ParameterContract(
        name="id",
        location=ParameterLocation.PATH,
        required=True,
        schema=IntegerNode(origin=origin),
        style="simple",
        explode=False,
        origin=origin,
    )

    with pytest.raises(UnsupportedConstructError, match="multipleOf") as info:
        plan_parameters(ParameterLocation.QUERY, [step])
    assert "/param/query/step" in str(info.value)

    rendered = render_parameters_module(
        module_docstring="Параметры.",
        parameter_sets=[
            ("QuerySchema", ParameterLocation.QUERY, [step]),
            ("PathSchema", ParameterLocation.PATH, [identifier]),
        ],
    )
    assert rendered.exports == (("path", "PathSchema"),)
    assert [location for location, _ in rendered.refused] == ["query"]
    assert rendered.content is not None and "QuerySchema" not in rendered.content


def test_request_view_rejects_inconsistent_parameter_sets() -> None:
    """Места и плоские кортежи параметров — одно и то же: расхождение — ошибка сразу."""
    from geas.runtime.operation import ParameterSetView, ParameterView, RequestView

    def view(name: str, location: ParameterLocation) -> ParameterView:
        return ParameterView(
            name=name,
            location=location,
            required=False,
            style="form",
            explode=True,
            json_schema={"type": "string"},
        )

    limit = view("limit", ParameterLocation.QUERY)
    other = view("page", ParameterLocation.QUERY)
    query_set = ParameterSetView(location=ParameterLocation.QUERY, parameters=(limit,))

    assert RequestView(query=(limit,), parameter_sets=(query_set,)).parameter_set("query")
    with pytest.raises(ValueError, match="расходятся"):
        RequestView(query=(other,), parameter_sets=(query_set,))
    with pytest.raises(ValueError, match="расходятся"):
        RequestView(parameter_sets=(query_set,))
    with pytest.raises(ValueError, match="дважды"):
        RequestView(query=(limit,), parameter_sets=(query_set, query_set))
    with pytest.raises(ValueError, match="другого места"):
        RequestView(path=(limit,))


def test_request_view_built_by_hand_still_lists_parameter_sets() -> None:
    """Инструменты собирают views напрямую: места находятся и без координат d42."""
    from geas.runtime.operation import ParameterView, RequestView

    view = ParameterView(
        name="limit",
        location=ParameterLocation.QUERY,
        required=False,
        style="form",
        explode=True,
        json_schema={"type": "integer"},
    )
    request = RequestView(query=(view,))

    parameter_set = request.parameter_set("query")
    assert parameter_set.parameters == (view,)
    assert parameter_set.d42_reference is None
    assert "limit — optional; integer" in parameter_set.describe()
    assert "схема не сгенерирована" in parameter_set.describe()


# ---------------------------------------------------------- generated-модули
# ---------------------------------------------------------- generated-модули


def test_tandava_shape_becomes_a_one_line_alias(generated: Any) -> None:
    """int64 и enum в query: ровно та форма, ради которой делалась фича."""
    project, operations = generated
    handle = operations.main.get_role_link

    query = handle.d42_schema(Direction.REQUEST, part="query")

    assert query == schema.dict(
        {
            "action": schema.any(
                schema.str("ADMIN"), schema.str("READ"), schema.str("MANAGE_INTEGRATIONS")
            ),
            "folderId": INT64,
            ...: ...,
        }
    )
    source = (project.output_dir / "_d42" / "main__get_role_link_request_parameters.py").read_text(
        encoding="utf-8"
    )
    assert (
        '    "folderId": serialized(schema.int.min(-9223372036854775808).max(9223372036854775807)),'
        in source
    )
    assert "GeneratedMainGetRoleLinkQueryParamsSchema = schema.dict({" in source

    expected = query % {"folderId": 42, "action": "READ"}
    assert expected == {"folderId": "42", "action": "READ"}
    assert expected != {"folderId": "43", "action": "READ"}
    assert expected != {"folderId": "42", "action": "ADMIN"}


def test_each_location_gets_its_own_export(generated: Any) -> None:
    project, operations = generated
    document = project.contract_document("main__list_items")

    assert document["d42"]["parameters_module"] == "main__list_items_request_parameters"
    assert document["d42"]["parameters"] == {
        "cookie": "GeneratedMainListItemsCookiesSchema",
        "header": "GeneratedMainListItemsHeadersSchema",
        "path": "GeneratedMainListItemsPathParamsSchema",
        "query": "GeneratedMainListItemsQueryParamsSchema",
    }
    handle = operations.main.list_items
    for location in ParameterLocation:
        view = handle.request.parameter_set(location)
        assert handle.d42_schema(Direction.REQUEST, part=location.value) is handle.d42_schema(
            Direction.REQUEST, part=location
        )
        assert view.d42_reference == (
            f"{project.package}.generated._d42.main__list_items_request_parameters:"
            f"{view.d42_export}"
        )
        assert view.d42_source_path is not None and view.d42_source_path.is_file()


def test_optional_parameters_are_optional_keys_and_path_is_closed(generated: Any) -> None:
    _, operations = generated
    handle = operations.main.list_items
    query = handle.d42_schema(Direction.REQUEST, part="query")
    path = handle.d42_schema(Direction.REQUEST, part="path")
    headers = handle.d42_schema(Direction.REQUEST, part="header")

    assert query == {}
    # Словарь query открыт: контракт не запрещает необъявленные параметры.
    assert query == {"utm": "x"}
    assert path == {"workspaceId": "w"}
    assert path != {"workspaceId": "w", "extra": "x"}
    # Пустого сегмента маршрута не бывает: шаблон сопоставляется с [^/]+.
    assert path != {"workspaceId": ""}
    assert headers != {"X-Page-Size": "10", "X-Trace-Id": " 0123abcd"}
    assert headers == {"X-Page-Size": "10", "Host": "localhost"}
    assert headers != {"Host": "localhost"}


def test_arrays_follow_their_serialization(generated: Any) -> None:
    _, operations = generated
    query = operations.main.list_items.d42_schema(Direction.REQUEST, part="query")

    # Повторы ключа — список; ноль повторов — это отсутствие параметра, а не [].
    assert query == {"status": ["draft", "published"]}
    assert query != {"status": []}
    assert query != {"status": "draft"}
    # Одной строкой — через свой разделитель.
    assert query == {"ids": "1,2,3"}
    assert query != {"ids": "1,2,3,4,5,6"}
    assert query == {"tags": "a bb c"}
    assert query == {"codes": "1|2"}
    assert query != {"codes": "1|1"}
    # Различных значений у enum два — длиннее уникальный массив не бывает.
    assert query == {"kinds": ["a", "b"]}
    assert query != {"kinds": ["a", "b", "a"]}


def test_nullable_string_accepts_the_empty_string_that_decodes_to_null(generated: Any) -> None:
    _, operations = generated
    query = operations.main.list_items.d42_schema(Direction.REQUEST, part="query")
    assert query == {"q": ""}
    assert query == {"q": "abc"}
    assert query != {"q": "ab"}


def test_fake_values_pass_the_request_contract_check(generated: Any) -> None:
    """Критерий приёмки: всё, что выдаёт ``fake()``, проходит проверку запроса по контракту."""
    _, operations = generated
    for handle, attempts in (
        (operations.main.get_role_link, 200),
        (operations.main.list_items, 300),
        (operations.main.get_version, 200),
        (operations.main.create_note, 200),
        # Пустой сегмент path и пробел по краю cookie выпадают редко: нужна длинная серия.
        (operations.main.transport_edges, 3000),
    ):
        parts = {
            item.location: handle.d42_schema(Direction.REQUEST, part=item.location)
            for item in handle.request.parameter_sets
        }
        random.seed(handle.key)
        for index in range(attempts):
            values = {location: fake(part) for location, part in parts.items()}
            handle.validate_recorded_request(**_recorded(handle, values), index=index)


def test_transport_edges_are_encoded_in_the_generated_module(generated: Any) -> None:
    project, operations = generated
    source = (
        project.output_dir / "_d42" / "main__transport_edges_request_parameters.py"
    ).read_text(encoding="utf-8")
    assert '"code": serialized(schema.str.regex("^[a-z]*$"), location="path"),' in source
    assert (
        '"names": serialized(schema.list(schema.str).len(1, ...), delimiter=",", '
        'location="path"),' in source
    )
    path = operations.main.transport_edges.d42_schema(Direction.REQUEST, part="path")
    assert path == {"names": "a,b", "code": "x", "rest": "r"}
    assert path != {"names": "", "code": "x", "rest": "r"}
    assert path != {"names": "a", "code": "", "rest": "r"}
    # Записанный путь раскодирован: «/» внутри одного сегмента не бывает.
    assert path != {"names": "a", "code": "x", "rest": "a/b"}
    assert path != {"names": "a/b", "code": "x", "rest": "r"}


@pytest.mark.parametrize(
    "query_params, pinned",
    [
        ({"pairs": "a"}, {"pairs": "a"}),
        ({"ids": [1, 2]}, {"ids": "1,2"}),
        ({"ids": "3,4"}, {"ids": "3,4"}),
        ({"archived": True}, {"archived": "true"}),
        ({"q": 12345}, {"q": "12345"}),
        (
            {"owner": UUID("00000000-0000-0000-0000-000000000001")},
            {"owner": "00000000-0000-0000-0000-000000000001"},
        ),
        ({"since": dt.date(2024, 1, 2)}, {"since": "2024-01-02"}),
    ],
)
def test_mock_pins_query_params_as_matcher_strings(
    generated: Any, query_params: dict[str, Any], pinned: dict[str, str]
) -> None:
    """Значение становится строкой матчера, а она проверяется по контракту.

    Один повтор массива ``pairs`` (``minItems: 2``) закрепляется строкой: матчер JJ
    совпадает, если совпал хотя бы один повтор ключа.
    """
    _, operations = generated
    mock = operations.main.list_items.mock(
        response=None,
        path_params={"workspaceId": "w"},
        query_params=query_params,
    )
    assert mock._query_params == pinned


def test_number_parameters_have_no_integer_branch(generated: Any) -> None:
    """Строка запроса разбирается во float всегда: целая ветка только испортила бы unique."""
    project, operations = generated
    source = (project.output_dir / "_d42" / "main__list_items_request_parameters.py").read_text(
        encoding="utf-8"
    )
    assert "json_int" not in source
    query = operations.main.list_items.d42_schema(Direction.REQUEST, part="query")
    assert query == {"weights": ["1", "2.0"], "ratio": "1"}
    assert query != {"weights": ["1", "3"]}
    random.seed(10)
    for _ in range(500):
        values = fake(query)
        for name in ("weights", "ratios"):
            if name in values:
                decoded = [float(item) for item in values[name]]
                assert len(decoded) == len(set(decoded)), values[name]


@pytest.mark.parametrize(
    "kwargs, pinned",
    [
        ({"path_params": {"documentId": 5, "version": "7"}}, {"documentId": "5", "version": "7"}),
        ({"path_params": {"documentId": "05"}}, {"documentId": "05"}),
    ],
)
def test_mock_pins_path_params_as_matcher_strings(
    generated: Any, kwargs: dict[str, Any], pinned: dict[str, str]
) -> None:
    _, operations = generated
    mock = operations.main.get_version.mock(response=None, **kwargs)
    assert mock._path_params == pinned


@pytest.mark.parametrize(
    "path_params, error, message",
    [
        ({"version": 0}, RequestContractError, "path_params['version']"),
        ({"version": True}, RequestContractError, "не приводится к типу 'integer'"),
        ({"documentId": None}, ContractMockError, "null в запросе записывается пустой строкой"),
    ],
)
def test_mock_rejects_path_params_no_request_could_match(
    generated: Any, path_params: dict[str, Any], error: type[Exception], message: str
) -> None:
    _, operations = generated
    with pytest.raises(error, match=re.escape(message)):
        operations.main.get_version.mock(response=None, path_params=path_params)


def test_pinned_path_param_is_compared_by_its_wire_text(generated: Any) -> None:
    """``True`` и ``7`` сравниваются с запросом так, как их отправил бы клиент."""
    _, operations = generated
    handle = operations.main.get_version
    request = {
        "method": "GET",
        "path": "/documents/5/versions/7",
        "segments": {},
        "params": [],
        "headers": [],
        "body": None,
    }
    handle.validate_recorded_request(**request, index=0, pinned_path_params={"version": 7})
    with pytest.raises(RequestContractError, match="отличается от закреплённого"):
        handle.validate_recorded_request(**request, index=0, pinned_path_params={"version": 8})


@pytest.mark.parametrize(
    "headers, pinned",
    [
        ({"X-Page-Size": 10}, {"X-Page-Size": "10"}),
        ({"x-page-size": "5"}, {"x-page-size": "5"}),
        ({"Authorization": "Bearer token"}, {"Authorization": "Bearer token"}),
    ],
)
def test_mock_pins_declared_headers_by_contract_and_others_as_is(
    generated: Any, headers: dict[str, Any], pinned: dict[str, str]
) -> None:
    _, operations = generated
    mock = operations.main.list_items.mock(
        response=None, path_params={"workspaceId": "w"}, headers=headers
    )
    assert mock._headers == pinned


def test_mock_rejects_declared_header_outside_the_contract(generated: Any) -> None:
    _, operations = generated
    with pytest.raises(RequestContractError, match=re.escape("headers['X-Page-Size']")):
        operations.main.list_items.mock(
            response=None, path_params={"workspaceId": "w"}, headers={"X-Page-Size": 0}
        )


@pytest.mark.parametrize(
    "query_params, outcome",
    [
        ({"q": None}, {"q": ""}),
        ({"archived": None}, "null в запросе записывается пустой строкой"),
        ({"limit": None}, "null в запросе записывается пустой строкой"),
    ],
)
def test_mock_writes_null_as_empty_string_only_where_it_decodes_to_null(
    generated: Any, query_params: dict[str, Any], outcome: Any
) -> None:
    _, operations = generated
    create = lambda: operations.main.list_items.mock(  # noqa: E731
        response=None, path_params={"workspaceId": "w"}, query_params=query_params
    )
    if isinstance(outcome, dict):
        assert create()._query_params == outcome
        return
    with pytest.raises(ContractMockError, match=outcome):
        create()


def test_request_d42_schema_of_a_bodiless_operation_points_to_parts(generated: Any) -> None:
    _, operations = generated
    with pytest.raises(OperationLookupError) as info:
        operations.main.get_role_link.d42_schema(Direction.REQUEST)
    message = str(info.value)
    assert "нет тела запроса" in message
    assert "part=" in message
    assert "d42: false" not in message
    with pytest.raises(OperationLookupError, match="нет ответа с телом"):
        operations.main.get_version.d42_schema(Direction.RESPONSE)


@pytest.mark.parametrize(
    "query_params, message",
    [
        ({"ids": 1}, "is too short"),
        ({"ids": [1, 2, 3, 4, 5, 6]}, "is too long"),
        ({"ratio": float("inf")}, "нет записи числом"),
        ({"since": "вчера"}, "is not a 'date'"),
        ({"pairs": ["a", "b"]}, "сравнивает одно значение"),
    ],
)
def test_mock_rejects_query_params_no_request_could_match(
    generated: Any, query_params: dict[str, Any], message: str
) -> None:
    _, operations = generated
    with pytest.raises(Exception, match=message):
        operations.main.list_items.mock(
            response=None, path_params={"workspaceId": "w"}, query_params=query_params
        )


def test_body_and_parameters_have_independent_modules(generated: Any) -> None:
    project, operations = generated
    document = project.contract_document("main__create_note")
    assert document["d42"]["request_module"] == "main__create_note_request"
    assert document["d42"]["parameters_module"] == "main__create_note_request_parameters"
    handle = operations.main.create_note
    assert handle.d42_schema(Direction.REQUEST) == {"text": "x"}
    assert handle.d42_schema(Direction.REQUEST, part="query") == {"notify": "false"}


def test_export_name_of_a_parameter_schema_is_accepted_directly(generated: Any) -> None:
    _, operations = generated
    handle = operations.main.get_role_link
    by_part = handle.d42_schema(Direction.REQUEST, part="query")
    by_export = handle.d42_schema(
        Direction.REQUEST, export="GeneratedMainGetRoleLinkQueryParamsSchema"
    )
    assert by_export is by_part


def test_unsupported_serialization_refuses_only_its_location(generated: Any) -> None:
    """deepObject по waiver'у: query-схемы нет с причиной, path-схема есть."""
    project, operations = generated
    document = project.contract_document("main__search_deep")
    assert document["d42"]["parameters"] == {"path": "GeneratedMainSearchDeepPathParamsSchema"}
    assert "deepObject" in document["d42"]["parameters_reasons"]["query"]
    assert any("параметры query" in item for item in document["unsupported"])

    handle = operations.main.search_deep
    assert handle.d42_schema(Direction.REQUEST, part="path") == {"spaceId": "s"}
    with pytest.raises(OperationLookupError, match="deepObject") as info:
        handle.d42_schema(Direction.REQUEST, part="query")
    assert "query-параметров" in str(info.value)
    assert handle.request.parameter_set("query").d42_reason is not None


def test_headers_differing_only_in_case_are_refused(generated: Any) -> None:
    project, operations = generated
    document = project.contract_document("main__mixed_case_headers")
    assert "регистром" in document["d42"]["parameters_reasons"]["header"]
    assert operations.main.mixed_case_headers.d42_schema(Direction.REQUEST, part="query") == {
        "page": "1"
    }


def test_update_reports_every_refused_location(tmp_path: Path) -> None:
    project = parameters_project(tmp_path / "project")
    result = project.cli("update")

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "параметры" in line]
    assert len(lines) == 2, result.stdout
    assert lines[0].startswith("  main.mixedCaseHeaders (параметры header): d42-схема не")
    assert lines[1].startswith("  main.searchDeep (параметры query): d42-схема не")
    assert "deepObject" in lines[1]


# ------------------------------------------------------------------ ошибки


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"direction": Direction.RESPONSE, "part": "query"}, "только для запроса"),
        (
            {"direction": Direction.REQUEST, "part": "query", "export": "X"},
            "либо export, либо part",
        ),
        ({"direction": Direction.REQUEST, "part": "body"}, "неизвестное место"),
        ({"direction": Direction.REQUEST, "part": "path"}, "нет path-параметров"),
    ],
)
def test_part_lookup_errors_are_explicit(
    generated: Any, kwargs: dict[str, Any], message: str
) -> None:
    _, operations = generated
    with pytest.raises(OperationLookupError, match=message):
        operations.main.get_role_link.d42_schema(**kwargs)


def test_manifest_d42_false_explains_why_there_is_no_schema(tmp_path: Path) -> None:
    project = parameters_project(
        tmp_path / "project",
        operations={"main.getRoleLink": {"source": "main", "d42": False}},
    )
    project.update()
    document = project.contract_document("main__get_role_link")
    assert document["d42"]["parameters_module"] is None
    assert document["d42"]["parameters"] == {}
    with (
        project.importable() as module,
        pytest.raises(OperationLookupError, match="d42: false"),
    ):
        module.operations.main.get_role_link.d42_schema(Direction.REQUEST, part="query")


def test_artifacts_of_an_older_generator_ask_for_regeneration(tmp_path: Path) -> None:
    """Документ без раздела ``parameters`` — это артефакт Geas, который его не знал."""
    project = parameters_project(tmp_path / "project")
    project.update()
    path = project.output_dir / "contracts" / "main__get_role_link.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for key in ("parameters", "parameters_module"):
        del document["d42"][key]
    path.write_text(json.dumps(document), encoding="utf-8")

    with (
        project.importable() as module,
        pytest.raises(OperationLookupError, match="geas update"),
    ):
        module.operations.main.get_role_link.d42_schema(Direction.REQUEST, part="query")


# -------------------------------------------------------------- geas show


def test_show_part_prints_parameters_and_the_d42_reference(generated: Any) -> None:
    project, _ = generated
    result = project.cli("show", "main.getRoleLink", "--part", "query")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:2] == ["main.getRoleLink", "Request query-параметры"]
    assert any(line.startswith("├── action — required; string; enum[") for line in lines)
    assert "  folderId: style=form, explode=true" in lines
    assert (
        f"  {project.package}.generated._d42.main__get_role_link_request_parameters:"
        f"GeneratedMainGetRoleLinkQueryParamsSchema" in lines
    )


def test_show_part_json_and_refusal_reason(generated: Any) -> None:
    project, _ = generated
    result = project.cli("show", "main.searchDeep", "--part", "query", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["part"] == "query"
    assert payload["d42_reference"] is None
    assert "deepObject" in payload["d42_reason"]
    assert [item["name"] for item in payload["parameters"]] == ["filter"]

    text = project.cli("show", "main.searchDeep", "--part", "query").stdout
    assert "схема не сгенерирована — параметр 'filter'" in text


@pytest.mark.parametrize(
    "args, message",
    [
        (("--part", "query", "--direction", "response"), "только к запросу"),
        (("--part", "query", "--status", "200"), "--status не сочетается"),
        (("--part", "path"), "нет path-параметров"),
    ],
)
def test_show_part_usage_errors(generated: Any, args: tuple[str, ...], message: str) -> None:
    project, _ = generated
    result = project.cli("show", "main.getRoleLink", *args)
    assert result.returncode != 0
    assert message in result.stderr


# --------------------------------------------------------------- помощники


def _recorded(handle: Any, values: dict[ParameterLocation, dict[str, Any]]) -> dict[str, Any]:
    """Перехваченный запрос, собранный из значений d42-схем мест.

    Значения ставятся в запрос как есть: так клиент отправил бы их по проводу.
    """
    path = handle.path
    for name, value in values.get(ParameterLocation.PATH, {}).items():
        # aiohttp отдаёт путь уже раскодированным, а runtime раскодирует сегмент ещё
        # раз: «/» внутри значения разбил бы маршрут, поэтому его не экранируем.
        path = path.replace(f"{{{name}}}", quote(value, safe="/"))
    params = [
        (name, item)
        for name, value in values.get(ParameterLocation.QUERY, {}).items()
        for item in (value if isinstance(value, list) else [value])
    ]
    headers = list(values.get(ParameterLocation.HEADER, {}).items())
    cookies = values.get(ParameterLocation.COOKIE, {})
    if cookies:
        headers.append(("Cookie", "; ".join(f"{name}={value}" for name, value in cookies.items())))
    body = None
    if handle.request.bodies:
        headers.append(("Content-Type", "application/json"))
        body = fake(handle.d42_schema(Direction.REQUEST))
    return {
        "method": handle.method,
        "path": path,
        "segments": {},
        "params": params,
        "headers": headers,
        "body": body,
    }


def test_recorded_helper_rejects_a_value_outside_the_contract(generated: Any) -> None:
    """Самопроверка помощника: сломанное значение он не маскирует."""
    _, operations = generated
    handle = operations.main.get_role_link
    request = _recorded(handle, {ParameterLocation.QUERY: {"folderId": "x", "action": "READ"}})
    with pytest.raises(Exception, match="folderId"):
        handle.validate_recorded_request(**request, index=0)
    validate_or_fail(schema.str, "sanity")
