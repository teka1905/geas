# Подключение geas к существующему E2E-проекту

Инструкция для проекта, в котором уже есть ручные d42-схемы и функции
`mocked_*`. Операции переносятся по одной, а вызовы вида
`async with mocked_post_ticket(body) as mock:` в существующих тестах менять не
нужно.

## 1. Установить библиотеку

```toml
# pyproject.toml тестового проекта
dependencies = [
    "geas[d42,jj]==0.4.0",
]
```

| Установка | Что даёт |
| --- | --- |
| `geas` | генерация контрактов и JSON Schema, CLI, `geas check` в CI, валидация вручную |
| `geas[d42]` | d42-схемы, фикстуры, overlay'и |
| `geas[jj]` | моки `operations.<…>.mock()` |

Фиксируйте точную версию. Версия генератора записывается в артефакты, и после
её смены `geas check` падает, пока артефакты не перегенерируют (см.
[обновление geas](cli.md#обновить-geas)).

## 2. Положить спецификацию в репозиторий

geas читает спецификацию только с диска и никогда не скачивает её. Положите файл
OpenAPI (Swagger 2.0 или OpenAPI 3.0.x) в репозиторий тестов и обновляйте его как
обычный файл.

Спецификацию можно разбить на несколько файлов. Межфайловые `$ref` разрешаются
внутри каталога спецификации; другой корень задаётся ключом `root` у источника
в manifest.

## 3. Создать manifest

```bash
geas -m contracts/manifest.yaml init \
  --source ../api/openapi.yaml \
  --package myproject.contracts.generated \
  --directory ../myproject/contracts/generated
```

- `--directory` — каталог для generated-кода внутри пакета, который импортируют
  тесты.
- `--package` — имя этого каталога при импорте.

Источник создаётся в режиме `selection: explicit`: в артефакты попадают только
операции, добавленные явно. Оставьте этот режим, пока покрыта только часть
спецификации. Режим `selection: all` генерирует все операции источника и
подходит небольшой спецификации, покрытой целиком.

## 4. Добавить первую операцию

```bash
geas -m contracts/manifest.yaml list
geas -m contracts/manifest.yaml add ws2.addTicket --source main --operation-id addTicket
geas -m contracts/manifest.yaml update
```

- `list` показывает `operationId`, маршруты и варианты ответа.
- `add` записывает операцию в manifest. Если операцию нельзя сгенерировать,
  manifest не меняется. Что делать в этом случае, описано в разделе
  [«Если операция не переносится»](#если-операция-не-переносится).
- `update` генерирует артефакты. Закоммитьте их вместе с manifest.

Подробно о флагах — в [cli.md](cli.md#add).

## 5. Превратить `mocked_*` в обёртку

Было:

```python
# mocks/tickets.py
def mocked_post_ticket(response_body: dict, wait_for_requests: int = 1):
    return jj.mocked(
        jj.match("POST", "/api/ws2/tickets"),
        jj.Response(status=200, json=response_body),
    )
```

Стало:

```python
# mocks/tickets.py
from geas.integrations.jj import ContractMock
from myproject.contracts.generated import operations


def mocked_post_ticket(response_body: dict, wait_for_requests: int = 1) -> ContractMock:
    """Обёртка для старых тестов. В новых используйте operations.ws2.add_ticket.mock()."""
    return operations.ws2.add_ticket.mock(
        response=response_body,
        wait_for_requests=wait_for_requests,
    )
```

Вызывающие тесты остаются без изменений:

```python
async with mocked_post_ticket(response_body) as mock:
    await page.submit()
```

Теперь мок проверяет тело ответа по контракту **до** регистрации, а каждый
перехваченный запрос — **при выходе** из блока. Если после перевода тест
падает с `ResponseContractError` или `RequestContractError`, значит, тест или
приложение расходятся со спецификацией. Сообщение показывает, где именно.

## 6. Новые тесты писать через generated-операцию

```python
from myproject.contracts.generated import operations


async def test_ticket_is_created(page):
    async with operations.ws2.add_ticket.mock(
        response=response_body,
        path_params={"queueId": queue_id},
        wait_for_requests=1,
    ) as mock:
        await page.submit()

    assert len(mock.history) == 1
```

`wait_for_requests=1` ждёт *как минимум* один запрос. Точное количество
проверяйте сами, через `mock.history`.

Обёртки оставляйте только там, где они что-то добавляют, например собирают
типовое тело ответа.

## 7. Заменить ручные d42-схемы overlay'ями

Было — схема, полностью написанная вручную:

```python
QueueDetailsSchema = schema.dict(
    {
        "id": schema.int,
        "name": ValidQueueNameSchema,
        "groups": schema.list(schema.dict({"title": ValidGroupTitleSchema})),
    }
)
```

Стало — сгенерированная схема, в которой подменены только генераторы нужных
листьев:

```python
from geas.integrations.d42 import EACH, overlay_generators
from myproject.contracts.generated._d42.v2__get_queue_response import (
    GeneratedTicketQueueDtoSchema,
)

QueueDetailsSchema = overlay_generators(
    GeneratedTicketQueueDtoSchema,
    {
        ("name",): ValidQueueNameSchema,
        ("groups", EACH, "title"): ValidGroupTitleSchema,
    },
)
```

Структура, обязательность полей, типы и границы берутся из спецификации. Если
поле переименуют, overlay упадёт уже при импорте модуля и покажет точный путь.

Имя модуля и переменной generated-схемы печатает `geas show`, в строке `d42:`:

```bash
geas -m contracts/manifest.yaml show v2.getQueue --status 200
```

## 8. Добавить `check` в CI

```yaml
- name: Контракты соответствуют спецификации
  run: geas -m contracts/manifest.yaml check
```

Исключите generated-каталог из автоформатирования, иначе форматтер изменит
артефакты и `check` упадёт:

```toml
[tool.ruff]
extend-exclude = ["**/contracts/generated/**"]
```

В CI-окружении должен стоять extra `[d42]`, если хотя бы у одной операции
включены d42-схемы.

## Если операция не переносится

Варианты от лучшего к худшему:

1. **Исправить спецификацию.** Чаще всего ошибка указывает на реальный дефект:
   схема без `type`, `required` на несуществующее поле, `$ref` с лишним соседним
   ключом.
2. **Закрепить только нужные варианты.** Если проблема в варианте ответа,
   который тестам не нужен, закрепите только нужные:
   `--response 200:application/json`.
3. **Выписать waiver.** Точечный, со сроком и владельцем — см.
   [waivers.md](waivers.md#как-выписать-waiver).

Конструкции, которые точны в JSON Schema, но не выражаются в d42 (рекурсия,
несливаемый `allOf`, `multipleOf`), операцию не блокируют. geas сам отключает
для неё d42-схемы и печатает причину в выводе `update`; моки и валидация
продолжают работать. Если d42-схемы операции не нужны вовсе, отключите их явно:
`d42: false` в manifest или `add --no-d42`.

## Как проверить, что операция перенесена

- `geas check` проходит.
- Существующие тесты проходят без изменений в местах вызова `mocked_*`.
- В diff коммита есть только спецификация, manifest, generated-артефакты и
  упрощённая обёртка `mocked_*`.

Переносите по одному проекту и по одной операции за раз.

## Ограничения, которые стоит учесть

- Мок подключается только через `async with …mock(…)`. Декоратора нет.
- Persistent-моков нет: каждый мок живёт в пределах своего блока `async with`.
- OpenAPI 3.1 не поддерживается: такой документ отклоняется с объяснением.
- Количество вызовов и бизнес-смысл запросов geas не проверяет. Для этого есть
  `mock.history` и ассерты в тесте.

Полный список поддержанных конструкций — [support-matrix.md](support-matrix.md).
