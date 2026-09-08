# Миграция существующего E2E-проекта

Документ про подключение библиотеки к проекту, где уже есть ручные схемы и
`mocked_*`-функции. Главное свойство миграции — **call sites не меняются**:
существующие `async with mocked_post_ticket(body) as mock:` продолжают работать,
пока вы переносите контракты по одной операции.

## Порядок

Миграция инкрементальная. Ни один шаг не требует переписать всё сразу.

### 1. Поставить библиотеку

```toml
# pyproject.toml тестового проекта
dependencies = [
    "openapi-contract-fixtures[d42,jj]",
]
```

Extras нужны, если проект использует d42-фикстуры и JJ-моки. Ядро (нормализация,
JSON Schema, CLI, `check` в CI) работает и без них.

### 2. Положить спецификацию в репозиторий

Библиотека читает **только** локальные файлы и никогда не ходит в сеть.
Спецификация должна лежать в репозитории рядом с тестами и обновляться как
обычный файл.

### 3. Завести manifest

```bash
openapi-contracts -m contracts/manifest.yaml init \
  --source ../api/openapi.yaml \
  --package myproject.contracts.generated \
  --directory ../myproject/contracts/generated
```

Начинайте с режима `explicit`: он превращает manifest в allowlist, и в
generated-артефакты попадают только те операции, которые вы явно закрепили.
Режим `all` разумен позже, когда контракт покрыт целиком.

### 4. Перенести первую операцию

```bash
openapi-contracts -m contracts/manifest.yaml list
openapi-contracts -m contracts/manifest.yaml add ws2.addTicket \
    --source main --operation-id addTicket
openapi-contracts -m contracts/manifest.yaml update
```

`add` транзакционен: если конструкция не поддержана, manifest останется
байт-в-байт прежним, и вы просто возьмёте следующую операцию.

### 5. Превратить `mocked_*` в тонкую обёртку

Было:

```python
# mocks/tickets.py
async def mocked_post_ticket(response_body: dict, wait_for_requests: int = 1):
    return jj.mocked(
        jj.match("POST", "/api/ws2/tickets"),
        jj.Response(status=200, json=response_body),
    )
```

Стало:

```python
# mocks/tickets.py
from myproject.contracts.generated import operations
from openapi_contracts.integrations.jj import ContractMock


def mocked_post_ticket(response_body: dict, wait_for_requests: int = 1) -> ContractMock:
    """Тонкая обёртка над generated-операцией.

    Существует только ради обратной совместимости call sites. В новых тестах
    используйте ``operations.ws2.add_ticket.mock(...)`` напрямую.
    """
    return operations.ws2.add_ticket.mock(
        response=response_body,
        wait_for_requests=wait_for_requests,
    )
```

Ни один вызывающий тест менять не нужно:

```python
async with mocked_post_ticket(response_body) as mock:
    await page.submit()
```

Разница в том, что теперь этот мок проверяет тело ответа **до** регистрации и
каждый перехваченный запрос **на выходе** из блока.

### 6. Писать новые тесты напрямую

Рекомендуемая форма — generated handle без обёртки:

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

Обёртки оставляйте только там, где они действительно что-то добавляют —
например собирают типовое тело ответа.

### 7. Перевести ручные схемы на overlay'и

Было — схема, переписанная руками целиком:

```python
QueueDetailsSchema = schema.dict({
    "id": schema.int,
    "name": ValidQueueNameSchema,
    "groups": schema.list(schema.dict({"title": ValidGroupTitleSchema})),
})
```

Проблема очевидна: когда бэкенд добавит обязательное поле, эта схема останется
прежней, тест продолжит проходить, а продакшен сломается.

Стало — generated-контракт с подменёнными **генераторами листьев**:

```python
from openapi_contracts.integrations.d42 import EACH, overlay_generators
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

Структура, обязательность полей, типы и границы остаются сгенерированными.
Переименовали поле в спецификации — overlay падает при импорте с точным путём,
а не молча генерирует данные не той формы.

### 8. Поставить `check` в CI

```yaml
- name: Контракты соответствуют спецификации
  run: openapi-contracts -m contracts/manifest.yaml check
```

Команда ничего не меняет в рабочем дереве и падает с кодом `1`, если
generated-артефакты разошлись со спецификацией.

**Исключите generated-каталог из автоформатирования.** Байты артефактов — часть
контракта; проход `black`/`ruff format` по ним сделает `check` красным:

```toml
[tool.ruff]
extend-exclude = ["**/contracts/generated/**"]
```

## Что делать с тем, что не переносится

Не всякую операцию удастся закрепить сразу. Варианты по убыванию
предпочтительности:

1. **Починить спецификацию.** Чаще всего ошибка указывает на реальный дефект:
   схема без `type`, `required` на несуществующее поле, `$ref` с лишним соседом.
2. **Сузить выбор.** Закрепите только те варианты ответа и content type, которые
   реально используются в тестах: `--response 200:application/json`.
3. **Выписать waiver** — точечный, со сроком и владельцем. См.
   [docs/waivers.md](waivers.md).
4. **Отключить d42 для операции** (`d42: false` в manifest), если проблема
   только в проекции d42, а JSON-Schema-контракт нужен. Библиотека делает это и
   сама, записывая причину в артефакт.

## Чего в v0.1 нет

* **Декораторов.** Только async context manager: контракт проверяется в двух
  точках жизненного цикла, и только у контекстного менеджера есть обе.
* **OpenAPI 3.1.** Отклоняется явной диагностикой, а не разбирается правилами 3.0.
* **Persistent-моков.** `start()` без обязательной проверки позволил бы молча
  пропустить валидацию.
* **Проверок бизнес-смысла и количества вызовов.** Это остаётся в тестах:
  библиотека даёт `mock.history`, ассерты пишете вы.

## Порядок подключения нескольких проектов

Подключайте по одному проекту и по одной операции. Признак, что шаг сделан
правильно: `openapi-contracts check` зелёный, тесты проходят без изменений в
call sites, а в diff видно только появление generated-артефактов и превращение
`mocked_*` в обёртку.
