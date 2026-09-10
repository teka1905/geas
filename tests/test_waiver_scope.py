"""Тесты области действия waiver'а (``scope``) и предупреждения об истечении.

Мотивация — реальный случай: генератор Swagger 2 пометил всю request-модель
операции как ``readOnly``, и один дефект корневой модели потребовал 75 записей в
``waivers.yaml`` — по одной на каждый лист. Такой файл нельзя ни прочитать, ни
отредактировать: починка источника роняет генерацию пачкой из 75 ошибок, а срок
истекает у всех разом и без предупреждения.

``scope: subtree`` сворачивает такую пачку в одну запись. Здесь проверяется, что
свёртка **эквивалентна** развёрнутому набору, что она не расползается шире, чем
заявлено, и что за широту приходится платить более чувствительным отпечатком.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from geas.contracts import build_contracts
from geas.errors import ManifestError, UnsupportedConstructError, WaiverError
from geas.manifest import Policies
from geas.models import Direction
from geas.waivers import Waiver, WaiverRule, WaiverScope, WaiverSet, load_waivers
from support import Project, make_project

#: Отпечаток-заглушка. Настоящий печатает сама библиотека — см. :func:`resolve_digests`.
PLACEHOLDER = "0" * 64

#: Спецификация с дефектом «вся модель запроса помечена readOnly».
#: ``Ticket`` намеренно общий для двух операций: послабление на одной из них не
#: имеет права ослабить вторую.
READ_ONLY_MODEL = """openapi: "3.0.3"
info:
  title: Subtree fixture
  version: "1.0.0"
paths:
  /tickets:
    post:
      operationId: addTicket
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/Ticket"
      responses:
        "200":
          description: Создан
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
  /mirror:
    post:
      operationId: mirrorTicket
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/Ticket"
      responses:
        "200":
          description: Создан
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
components:
  schemas:
    Ticket:
      type: object
      required: [queue]
      properties:
        queue:
          type: string
        subject:
          type: string
          readOnly: true
        author:
          readOnly: true
          $ref: "#/components/schemas/Party"
        tags:
          type: array
          readOnly: true
          items:
            $ref: "#/components/schemas/Tag"
    Party:
      type: object
      properties:
        login:
          type: string
          readOnly: true
        email:
          type: string
          readOnly: true
    Tag:
      type: object
      properties:
        name:
          type: string
          readOnly: true
        weight:
          type: integer
"""

#: Все точки, где ``readOnly`` выкидывает поле из запроса ``addTicket``.
READ_ONLY_POINTS = (
    "/body/author",
    "/body/author/email",
    "/body/author/login",
    "/body/subject",
    "/body/tags",
    "/body/tags/-/name",
)

#: Спецификация, где обязательность приходит с нескольких уровней сразу.
REQUIRED_MODEL = """openapi: "3.0.3"
info:
  title: Required fixture
  version: "1.0.0"
paths:
  /orders:
    post:
      operationId: addOrder
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/Order"
      responses:
        "200":
          description: Создан
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
components:
  schemas:
    Order:
      type: object
      required: [id, customer]
      properties:
        id:
          type: string
        customer:
          $ref: "#/components/schemas/Customer"
    Customer:
      type: object
      required: [name]
      properties:
        name:
          type: string
"""

#: Рекурсивная модель: развернуть её по месту невозможно.
RECURSIVE_MODEL = """openapi: "3.0.3"
info:
  title: Recursive fixture
  version: "1.0.0"
paths:
  /nodes:
    post:
      operationId: addNode
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/Node"
      responses:
        "200":
          description: Создан
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
components:
  schemas:
    Node:
      type: object
      properties:
        child:
          $ref: "#/components/schemas/Node"
        id:
          type: string
          readOnly: true
"""


# ------------------------------------------------------------------ помощники


def today() -> dt.date:
    return dt.date.today()


def in_days(days: int) -> dt.date:
    return today() + dt.timedelta(days=days)


def ticket_project(
    root: Path,
    *,
    source: str = READ_ONLY_MODEL,
    operations: dict[str, Any] | None = None,
    policies: dict[str, Any] | None = None,
) -> Project:
    """Проект на спецификации с двумя операциями поверх общей модели."""
    project = make_project(root)
    project.write_spec("api/openapi.yaml", source)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations=operations
        or {
            "api.addTicket": {"source": "main", "operation_id": "addTicket"},
            "api.mirrorTicket": {"source": "main", "operation_id": "mirrorTicket"},
        },
        policies=policies,
    )
    return project


def waiver(**overrides: Any) -> dict[str, Any]:
    """Полностью заполненный waiver с отпечатком-заглушкой."""
    data: dict[str, Any] = {
        "operation": "api.addTicket",
        "direction": "request",
        "json_pointer": "/body",
        "rule": "ignore_read_only",
        "reason": "генератор Swagger 2 пометил модель запроса как readOnly",
        "owner": "team-tickets",
        "issue": "ISSUE-1",
        "expires_at": in_days(30),
        "expected_source": PLACEHOLDER,
    }
    data.update(overrides)
    return data


_STALE = re.compile(
    r"waiver (?P<operation>\S+) / (?P<direction>\S+) / (?P<pointer>\S+) / (?P<rule>[a-z_]+)"
)
_ACTUAL = re.compile(r"фактический:\s+(?P<digest>[0-9a-f]{64})")


def resolve_digests(project: Project, waivers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Заменить заглушки на фактические отпечатки — ровно как это делает человек.

    Отпечаток поддерева не считается глазами: он берётся по всему поддереву с
    развёрнутыми ``$ref``. Библиотека печатает фактическое значение в ошибке,
    оттуда его и копируют в ``waivers.yaml``. Заодно это проверка того, что в
    сообщении есть всё нужное для копипасты.
    """
    current = [dict(item) for item in waivers]
    for _ in range(len(current) + 1):
        if not any(item["expected_source"] == PLACEHOLDER for item in current):
            return current
        project.write_waivers(current)
        with pytest.raises(WaiverError) as info:
            project.build()
        message = str(info.value)
        stale = _STALE.search(message)
        actual = _ACTUAL.search(message)
        assert stale is not None and actual is not None, message
        for item in current:
            if (
                item["operation"] == stale["operation"]
                and item["direction"] == stale["direction"]
                and item["json_pointer"] == stale["pointer"]
                and item["rule"] == stale["rule"]
            ):
                item["expected_source"] = actual["digest"]
                break
        else:  # pragma: no cover — сообщение обязано указывать на реальный waiver
            raise AssertionError(f"ошибка не указала ни на один waiver:\n{message}")
    raise AssertionError("отпечатки не сошлись за отведённое число проходов")


def build_with(project: Project, waivers: WaiverSet) -> Any:
    """Собрать контракты тем же набором, у которого потом спрашивают покрытие."""
    return build_contracts(project.load(), waivers)


def request_properties(project: Project, key: str) -> dict[str, Any]:
    """Свойства тела запроса собранной операции.

    Корень может остаться ссылкой на общее определение — именно так выглядит
    операция, до которой послабление не дотянулось.
    """
    document = project.build().by_key(key).document
    schema = document["request"]["bodies"][0]["schema"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = schema["$defs"][name]
    return dict(schema["properties"])


# ----------------------------------------------- эквивалентность свёртки


def test_subtree_waiver_is_equivalent_to_the_pile_it_replaces(tmp_path: Path) -> None:
    """Главный критерий: свёрнутый набор даёт ровно тот же контракт.

    Если байты разошлись — поддерево накрывает не то, что накрывали точечные
    записи, и свёртка не эквивалентна.
    """
    spread = ticket_project(tmp_path / "spread")
    spread.write_waivers(
        resolve_digests(
            spread,
            [
                waiver(json_pointer=pointer, issue=f"ISSUE-{index}")
                for index, pointer in enumerate(READ_ONLY_POINTS)
            ],
        )
    )
    expanded = spread.build().by_key("api.addTicket").document

    collapsed = ticket_project(tmp_path / "collapsed")
    collapsed.write_waivers(
        resolve_digests(collapsed, [waiver(json_pointer="/body", scope="subtree")])
    )
    folded = collapsed.build().by_key("api.addTicket").document

    assert folded == expanded
    # И сам контракт действительно не потерял ни одного поля модели.
    assert set(folded["request"]["bodies"][0]["schema"]["properties"]) == {
        "author",
        "queue",
        "subject",
        "tags",
    }


def test_subtree_waiver_reports_how_many_points_it_covered(tmp_path: Path) -> None:
    """Ширину поддерева не видно по записи — её видно по числу накрытых точек."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(resolve_digests(project, [waiver(json_pointer="/body", scope="subtree")]))
    waivers = project.waivers()

    build_with(project, waivers)

    coverage = waivers.subtree_coverage()
    assert len(coverage) == 1
    covered_waiver, covered = coverage[0]
    assert covered_waiver.scope is WaiverScope.SUBTREE
    assert covered == len(READ_ONLY_POINTS)


def test_subtree_waiver_does_not_leak_into_a_neighbour_operation(tmp_path: Path) -> None:
    """Изоляция — то же свойство, что и у точечного waiver'а, только шире область."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(resolve_digests(project, [waiver(json_pointer="/body", scope="subtree")]))

    assert "subject" in request_properties(project, "api.addTicket")
    # Вторая операция ссылается на ту же схему ``Ticket`` и обязана остаться строгой.
    assert set(request_properties(project, "api.mirrorTicket")) == {"queue"}


# ---------------------------------------------------------------- приоритет


def test_exact_waiver_wins_over_a_covering_subtree(tmp_path: Path) -> None:
    """Точное совпадение сильнее покрывающего поддерева.

    Проверяется через отпечаток: у точечного waiver'а он свой, и если бы
    выигрывало поддерево, сверка пошла бы по отпечатку поддерева и не сошлась.
    """
    project = ticket_project(tmp_path / "project")
    waivers = resolve_digests(
        project,
        [
            waiver(json_pointer="/body", scope="subtree"),
            waiver(json_pointer="/body/subject", issue="ISSUE-2"),
        ],
    )
    project.write_waivers(waivers)

    assert "subject" in request_properties(project, "api.addTicket")
    # Разные отпечатки — значит сверялись действительно разные фрагменты.
    digests = {item["json_pointer"]: item["expected_source"] for item in waivers}
    assert digests["/body"] != digests["/body/subject"]


def test_nearest_subtree_wins_over_a_farther_one(tmp_path: Path) -> None:
    """Из двух вложенных поддеревьев применяется ближайшее к точке."""
    project = ticket_project(tmp_path / "project")
    waivers = resolve_digests(
        project,
        [
            waiver(json_pointer="/body", scope="subtree"),
            waiver(json_pointer="/body/author", scope="subtree", issue="ISSUE-2"),
        ],
    )
    project.write_waivers(waivers)
    loaded = project.waivers()

    build_with(project, loaded)

    coverage = {w.path: covered for w, covered in loaded.subtree_coverage()}
    # ``/body/author`` забирает себе сам узел и оба его свойства,
    # ``/body`` — всё остальное.
    assert coverage[("body", "author")] == 3
    assert coverage[("body",)] == len(READ_ONLY_POINTS) - 3


def test_exact_and_subtree_on_one_point_are_rejected(tmp_path: Path) -> None:
    """Две области на одной точке с одним правилом — двусмысленность, а не выбор."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        [
            waiver(json_pointer="/body/subject", expected_source="a" * 64),
            waiver(json_pointer="/body/subject", scope="subtree", expected_source="b" * 64),
        ]
    )

    with pytest.raises(WaiverError, match="объявлен дважды"):
        project.waivers()


def test_tie_between_equally_narrow_waivers_is_an_error() -> None:
    """Страховка на будущее: полная ничья обязана падать, а не выбирать наугад.

    Через ``waivers.yaml`` такую пару не собрать — одинаковая ширина при
    одинаковом правиле и варианте даёт совпадающий ``identity``, и набор
    отвергается как дубликат ещё при загрузке. Проверка держит инвариант на
    случай, если появятся новые измерения сужения.
    """
    common: dict[str, Any] = {
        "operation": "api.addTicket",
        "direction": Direction.REQUEST,
        "path": ("body", "subject"),
        "rule": WaiverRule.IGNORE_READ_ONLY,
        "reason": "r",
        "owner": "o",
        "issue": "ISSUE-1",
        "expires_at": in_days(1),
        "expected_source": "a" * 64,
    }
    tied = [Waiver(**common), Waiver(**common)]

    with pytest.raises(WaiverError, match="равнозначных"):
        WaiverSet._pick(tied, path=("body", "subject"))


# ------------------------------------------------------- допустимые правила


@pytest.mark.parametrize(
    "rule, extra",
    [
        ("replace_schema", {"replacement": {"type": "string"}}),
        ("add_property", {"replacement": {"type": "string"}}),
        ("extend_enum", {"values": ["draft"]}),
        ("allow_any", {}),
        ("allow_null", {}),
        ("ignore_discriminator", {}),
        ("allow_unknown_format", {}),
        ("allow_unsupported_serialization", {}),
    ],
)
def test_subtree_is_rejected_for_rules_that_replace_content(
    tmp_path: Path, rule: str, extra: dict[str, Any]
) -> None:
    """Поддеревом послабляются только правила, осмысленные в каждой точке.

    «Заменить всё поддерево на эту схему» или «дополнить каждый enum внутри
    этими литералами» — не послабление, а неопределённость.
    """
    project = ticket_project(tmp_path / "project")
    project.write_waivers([waiver(rule=rule, scope="subtree", expected_source="a" * 64, **extra)])

    with pytest.raises(WaiverError) as info:
        project.waivers()

    message = str(info.value)
    assert "не допускает 'scope: subtree'" in message
    assert rule in message


@pytest.mark.parametrize("rule", ["ignore_read_only", "ignore_write_only", "relax_required"])
def test_subtree_is_accepted_for_slot_rules(tmp_path: Path, rule: str) -> None:
    """Разрешены ровно те правила, что решают судьбу слота, а не его содержимого."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers([waiver(rule=rule, scope="subtree", expected_source="a" * 64)])

    loaded = project.waivers()

    assert loaded.waivers[0].scope is WaiverScope.SUBTREE


def test_unknown_scope_value_is_rejected(tmp_path: Path) -> None:
    project = ticket_project(tmp_path / "project")
    project.write_waivers([waiver(scope="everything", expected_source="a" * 64)])

    with pytest.raises(WaiverError, match="scope"):
        project.waivers()


def test_relax_required_subtree_covers_every_level(tmp_path: Path) -> None:
    """``relax_required`` с поддеревом снимает обязательность на всех уровнях."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/openapi.yaml", REQUIRED_MODEL)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations={"api.addOrder": {"source": "main", "operation_id": "addOrder"}},
    )
    project.write_waivers(
        resolve_digests(
            project,
            [
                waiver(
                    operation="api.addOrder",
                    rule="relax_required",
                    json_pointer="/body",
                    scope="subtree",
                )
            ],
        )
    )

    document = project.build().by_key("api.addOrder").document
    body = document["request"]["bodies"][0]["schema"]
    assert body.get("required", []) == []
    assert body["properties"]["customer"].get("required", []) == []


# --------------------------------------------------- чувствительность отпечатка


def test_change_deep_inside_the_subtree_breaks_the_waiver(tmp_path: Path) -> None:
    """Ради этого отпечаток и считается по всему поддереву.

    Точечные записи закреплены каждая за своим узлом, поэтому правка соседнего
    узла проскакивает молча. Одна запись на поддерево обязана ловить любое
    изменение внутри — иначе она прикрывала бы уже не тот контракт, под который
    её выписали.
    """
    project = ticket_project(tmp_path / "project")
    project.write_waivers(resolve_digests(project, [waiver(json_pointer="/body", scope="subtree")]))
    assert "subject" in request_properties(project, "api.addTicket")

    # Меняется лист глубоко внутри поддерева, сам waiver не трогали.
    project.write_spec(
        "api/openapi.yaml",
        READ_ONLY_MODEL.replace(
            """        name:
          type: string
          readOnly: true""",
            """        name:
          type: string
          maxLength: 64
          readOnly: true""",
        ),
    )

    with pytest.raises(WaiverError) as info:
        project.build()

    message = str(info.value)
    assert "поддерево изменилось" in message
    # Диагностика показывает и покрывающий waiver, и конкретную точку контракта.
    assert "waiver покрывает поддерево: /body" in message
    assert "точка контракта:" in message


def test_annotations_inside_the_subtree_do_not_break_the_waiver(tmp_path: Path) -> None:
    """``description`` смысла контракта не меняет и отпечаток менять не должен."""
    project = ticket_project(tmp_path / "project")
    waivers = resolve_digests(project, [waiver(json_pointer="/body", scope="subtree")])
    project.write_waivers(waivers)

    project.write_spec(
        "api/openapi.yaml",
        READ_ONLY_MODEL.replace(
            """        name:
          type: string
          readOnly: true""",
            """        name:
          type: string
          description: Человекочитаемое имя тега
          example: важное
          readOnly: true""",
        ),
    )

    assert "subject" in request_properties(project, "api.addTicket")


def test_ref_indirection_inside_the_subtree_does_not_break_the_waiver(tmp_path: Path) -> None:
    """Вынос фрагмента в ``$ref`` и обратно — рефакторинг, а не изменение контракта.

    Отпечаток считается по развёрнутому поддереву, поэтому не зависит ни от
    имени схемы, ни от того, вынесена она в ``$ref`` или написана по месту.
    """
    project = ticket_project(tmp_path / "project")
    waivers = resolve_digests(project, [waiver(json_pointer="/body", scope="subtree")])
    project.write_waivers(waivers)

    # ``Tag`` встраивается по месту и переименовывается в неиспользуемое определение.
    project.write_spec(
        "api/openapi.yaml",
        READ_ONLY_MODEL.replace(
            """          items:
            $ref: "#/components/schemas/Tag\"""",
            """          items:
            type: object
            properties:
              name:
                type: string
                readOnly: true
              weight:
                type: integer""",
        ).replace(
            """    Tag:
      type: object
      properties:
        name:
          type: string
          readOnly: true
        weight:
          type: integer
""",
            "",
        ),
    )

    assert "subject" in request_properties(project, "api.addTicket")


def test_recursion_inside_the_claimed_subtree_fails_clearly(tmp_path: Path) -> None:
    """Fail closed: рекурсию внутри заявленного поддерева нельзя развернуть по месту."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/openapi.yaml", RECURSIVE_MODEL)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations={"api.addNode": {"source": "main", "operation_id": "addNode"}},
    )
    project.write_waivers([waiver(operation="api.addNode", json_pointer="/body", scope="subtree")])

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    message = str(info.value)
    assert "рекурсивный $ref" in message
    assert "scope: subtree" in message


# ------------------------------------------------------- ненужный и запрещённый


def test_unused_subtree_waiver_breaks_the_build(tmp_path: Path) -> None:
    """Ни разу не сработавшее поддерево — такой же мусор, как ненужная точка."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        [
            waiver(
                json_pointer="/body",
                rule="ignore_write_only",
                scope="subtree",
                expected_source="a" * 64,
            )
        ]
    )

    with pytest.raises(WaiverError, match="больше не нужны"):
        project.build()


def test_subtree_waiver_cannot_cover_a_non_waivable_point(tmp_path: Path) -> None:
    """Защищённая точка внутри поддерева — это пересечение, а не «мимо»."""
    project = ticket_project(
        tmp_path / "project",
        operations={
            "api.addTicket": {
                "source": "main",
                "operation_id": "addTicket",
                "non_waivable": [
                    {"direction": "request", "json_pointer": "/body/queue", "rules": ["required"]}
                ],
            },
            "api.mirrorTicket": {"source": "main", "operation_id": "mirrorTicket"},
        },
    )
    project.write_waivers([waiver(json_pointer="/body", scope="subtree", expected_source="a" * 64)])

    with pytest.raises(WaiverError) as info:
        project.waivers().validate(project.load(), today=today())

    message = str(info.value)
    assert "пересекается с non_waivable" in message
    # Обе координаты: и что покрывает waiver, и что именно он задел.
    assert "waiver покрывает поддерево: /body" in message
    assert "защищённая точка внутри:   /body/queue" in message


# ------------------------------------------------------ обратная совместимость


def test_waiver_without_scope_stays_exact(tmp_path: Path) -> None:
    """Файлы, написанные до 0.3.0, работают ровно как раньше."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        resolve_digests(
            project,
            [
                waiver(json_pointer=pointer, issue=f"ISSUE-{index}")
                for index, pointer in enumerate(READ_ONLY_POINTS)
            ],
        )
    )
    loaded = project.waivers()

    assert all(item.scope is WaiverScope.EXACT for item in loaded.waivers)
    assert loaded.subtree_coverage() == ()
    # Соседняя точка внутри той же модели точечным waiver'ом не накрыта.
    assert loaded.waivers[0].covers(("body", "subject", "extra")) is False


def test_exact_scope_is_not_written_back(tmp_path: Path) -> None:
    """Канонический вид файла у проектов без subtree не меняется."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        [
            waiver(json_pointer="/body/subject", expected_source="a" * 64),
            waiver(json_pointer="/body/tags", scope="subtree", expected_source="b" * 64),
        ]
    )

    document = project.waivers().to_dict()

    by_pointer = {item["json_pointer"]: item for item in document["waivers"]}
    assert "scope" not in by_pointer["/body/subject"]
    assert by_pointer["/body/tags"]["scope"] == "subtree"
    # Круговорот через файл ничего не теряет.
    project.waivers_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    assert load_waivers(project.waivers_path).to_dict() == document


def test_explicit_exact_scope_is_accepted(tmp_path: Path) -> None:
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        [waiver(json_pointer="/body/subject", scope="exact", expected_source="a" * 64)]
    )

    assert project.waivers().waivers[0].scope is WaiverScope.EXACT


# ------------------------------------------------- предупреждение об истечении


def test_expiring_waivers_are_reported_without_failing(tmp_path: Path) -> None:
    """Срок не должен наступать внезапно посреди чужого релиза."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        resolve_digests(
            project, [waiver(json_pointer="/body", scope="subtree", expires_at=in_days(3))]
        )
    )
    project.update()

    result = project.cli("check")

    assert result.returncode == 0, result.stderr
    assert "предупреждение: waiver скоро истекает" in result.stderr
    assert "осталось дней: 3" in result.stderr
    # Покрытие печатается рядом — ревьюеру видно, насколько запись широка.
    assert "покрыто точек контракта — 6" in result.stdout


def test_waiver_outside_the_warning_window_is_silent(tmp_path: Path) -> None:
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        resolve_digests(
            project, [waiver(json_pointer="/body", scope="subtree", expires_at=in_days(30))]
        )
    )
    project.update()

    result = project.cli("check")

    assert result.returncode == 0, result.stderr
    assert "скоро истекает" not in result.stderr


def test_warning_window_is_configurable(tmp_path: Path) -> None:
    project = ticket_project(tmp_path / "project", policies={"waiver_warn_days": 45})
    project.write_waivers(
        resolve_digests(
            project, [waiver(json_pointer="/body", scope="subtree", expires_at=in_days(30))]
        )
    )
    project.update()

    result = project.cli("check")

    assert result.returncode == 0, result.stderr
    assert "скоро истекает" in result.stderr


def test_expiring_today_is_still_a_warning_not_an_error(tmp_path: Path) -> None:
    """Waiver действует до конца дня ``expires_at`` — ронять сборку не за что."""
    project = ticket_project(tmp_path / "project")
    project.write_waivers(
        resolve_digests(
            project, [waiver(json_pointer="/body", scope="subtree", expires_at=today())]
        )
    )
    project.update()

    result = project.cli("check")

    assert result.returncode == 0, result.stderr
    assert "истекает сегодня" in result.stderr


def test_default_warning_window() -> None:
    assert Policies().waiver_warn_days == 14


@pytest.mark.parametrize("value", [-1, True, 3.0, "14"])
def test_invalid_warn_days_is_rejected(value: Any) -> None:
    with pytest.raises(ManifestError, match="waiver_warn_days"):
        Policies.from_dict({"waiver_warn_days": value})


def test_warning_window_wider_than_the_lifetime_is_rejected() -> None:
    """Окно шире срока горело бы всегда и перестало бы быть сигналом."""
    with pytest.raises(ManifestError, match="не несёт сигнала"):
        Policies.from_dict({"waiver_max_days": 30, "waiver_warn_days": 31})
