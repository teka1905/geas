"""Waiver внутри варианта ``oneOf`` с ``discriminator.mapping``.

На пути waiver'а ``$ref`` разворачивается по месту, чтобы послабление не протекло в
соседние операции. Вариант объединения при этом перестаёт быть ссылкой, но
``mapping`` обязан по-прежнему вести в него: и при сопоставлении на нормализации,
и в ``if``/``then`` JSON Schema, и в ветке d42.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from d42 import ValidationException, validate_or_fail

from geas import Direction
from geas.errors import ValidationFailedError
from geas.waivers import waiver_source_digest
from support import Project, make_project

#: ``Hint`` общий для двух операций: waiver на ``getHint`` не имеет права
#: ослабить ``getOtherHint``.
SPEC = """openapi: "3.0.3"
info:
  title: Discriminator waiver fixture
  version: "1.0.0"
paths:
  /hint:
    get:
      operationId: getHint
      responses:
        "200":
          description: Подсказка
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Hint"
  /other-hint:
    get:
      operationId: getOtherHint
      responses:
        "200":
          description: Подсказка
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Hint"
components:
  schemas:
    Hint:
      type: object
      properties:
        payload:
          $ref: "#/components/schemas/HintPayload"
    HintPayload:
      type: object
      required: [type]
      properties:
        title:
          type: string
        type:
          type: string
          enum: [KNOWLEDGE_REF, TICKET_REF]
      discriminator:
        propertyName: type
        mapping:
          KNOWLEDGE_REF: "#/components/schemas/KnowledgeRef"
          TICKET_REF: "#/components/schemas/TicketRef"
      oneOf:
        - $ref: "#/components/schemas/KnowledgeRef"
        - $ref: "#/components/schemas/TicketRef"
    KnowledgeRef:
      type: object
      required: [type]
      properties:
        type:
          type: string
          enum: [KNOWLEDGE_REF]
        knowledgeIds:
          type: array
          items:
            type: integer
            format: int64
    TicketRef:
      type: object
      required: [type]
      properties:
        type:
          type: string
          enum: [TICKET_REF]
        tickets:
          $ref: "#/components/schemas/ListSource"
    ListSource:
      type: object
      properties:
        values:
          type: array
          items:
            type: string
        variableId:
          type: integer
          format: int64
"""

POINTER = "/body/payload/#1/tickets/variableId"


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    built = make_project(tmp_path_factory.mktemp("discriminator_waiver"))
    built.write_spec("api/openapi.yaml", SPEC)
    built.write_manifest(sources={"main": {"path": "api/openapi.yaml", "selection": "all"}})
    built.write_waivers(
        [
            {
                "operation": "main.getHint",
                "direction": "response",
                "json_pointer": POINTER,
                "rule": "allow_null",
                "reason": "сервер отдаёт variableId: null, когда задан values",
                "owner": "team-hints",
                "issue": "ISSUE-1",
                "expires_at": dt.date.today() + dt.timedelta(days=30),
                "expected_source": waiver_source_digest(
                    {"type": "integer", "format": "int64"}, "allow_null"
                ),
            }
        ]
    )
    built.update()
    return built


@pytest.fixture(scope="module")
def operations(project: Project) -> Iterator[Any]:
    with project.importable() as generated:
        yield generated.operations


def ticket(variable_id: Any) -> dict[str, Any]:
    return {"payload": {"type": "TICKET_REF", "tickets": {"variableId": variable_id}}}


def test_waiver_applies_inside_the_mapped_variant(operations: Any) -> None:
    hint = operations.main.get_hint

    hint.validate_response(ticket(None), status=200)
    validate_or_fail(hint.d42_schema(Direction.RESPONSE), ticket(None))


def test_mapping_still_binds_the_value_to_its_variant(operations: Any) -> None:
    """``TICKET_REF`` по-прежнему обязан пройти ``TicketRef``: послаблен только ``null``."""
    hint = operations.main.get_hint

    hint.validate_response(ticket(7), status=200)
    with pytest.raises(ValidationFailedError):
        hint.validate_response(ticket("7"), status=200)
    with pytest.raises(ValidationFailedError):
        hint.validate_response({"payload": {"type": "TICKET_REF", "tickets": "x"}}, status=200)


def test_neighbour_operation_stays_strict(operations: Any) -> None:
    other = operations.main.get_other_hint

    other.validate_response(ticket(7), status=200)
    with pytest.raises(ValidationFailedError):
        other.validate_response(ticket(None), status=200)
    with pytest.raises(ValidationException):
        validate_or_fail(other.d42_schema(Direction.RESPONSE), ticket(None))


def mapped_then(payload: dict[str, Any]) -> dict[str, Any]:
    """``then`` по значению дискриминатора; ``allOf`` — собственные свойства и ``oneOf``."""
    _, union = payload["allOf"]
    return {rule["if"]["properties"]["type"]["const"]: rule["then"] for rule in union["allOf"]}


def test_inlined_variant_is_substituted_into_then(project: Project) -> None:
    """``then`` у развёрнутого варианта — его схема с послаблением, не ``$ref`` на исходное."""
    document = project.contract_document("main__get_hint")
    then = mapped_then(document["responses"][0]["schema"]["properties"]["payload"])

    assert then["KNOWLEDGE_REF"] == {"$ref": "#/$defs/KnowledgeRef"}
    variable_id = then["TICKET_REF"]["properties"]["tickets"]["properties"]["variableId"]
    assert variable_id["type"] == ["integer", "null"]


def test_neighbour_contract_keeps_references(project: Project) -> None:
    document = project.contract_document("main__get_other_hint")
    payload = document["responses"][0]["schema"]["$defs"]["HintPayload"]

    assert mapped_then(payload) == {
        "KNOWLEDGE_REF": {"$ref": "#/$defs/KnowledgeRef"},
        "TICKET_REF": {"$ref": "#/$defs/TicketRef"},
    }
