# Пример проекта-потребителя

Полный, запускаемый пример того, как выглядит проект, который берёт тестовые
контракты из checked-in OpenAPI. Домен вымышленный: рабочие пространства
(`workspace`) и документы (`document`).

Пример намеренно маленький: две операции, три схемы. Показывается **процесс**, а
не полнота поддержки OpenAPI.

## Что здесь лежит

| Путь | Кто владелец | Что это |
| --- | --- | --- |
| [`api/openapi.yaml`](api/openapi.yaml) | человек | checked-in спецификация — единственный источник истины |
| [`manifest.yaml`](manifest.yaml) | человек | что генерируем, к чему привязываемся, какие политики |
| [`waivers.yaml`](waivers.yaml) | человек | временные послабления контракта (здесь их нет) |
| [`app_contracts/__init__.py`](app_contracts/__init__.py) | человек | ручной код проекта |
| `app_contracts/generated/**` | **генератор** | артефакты; правки руками затираются |
| [`tests/test_documents.py`](tests/test_documents.py) | человек | тесты, ради которых всё затевалось |

Каталог `app_contracts/generated/` **закоммичен целиком**. Это принципиально:
generated-код читается в code review, а `openapi-contracts check` в CI падает,
если он разошёлся со спецификацией. Ничего не генерируется «на лету» во время
прогона тестов.

## Рабочий цикл

```sh
cd examples/consumer

# что вообще есть в источнике и что из этого выбрано manifest
openapi-contracts -m manifest.yaml list

# добавить в allowlist операцию, которая появилась в спецификации
# (транзакционно: manifest меняется, только если операция полностью собралась)
openapi-contracts -m manifest.yaml add api.archiveDocument \
    --source main --operation-id archiveDocument --response 204

# перегенерировать артефакты и закоммитить их
openapi-contracts -m manifest.yaml update

# это ставится в CI: молча выходит с 0, если артефакты актуальны, и с 1, если нет
openapi-contracts -m manifest.yaml check

# что именно изменилось в контракте (косметика отделена от семантики)
openapi-contracts -m manifest.yaml diff
```

## Как запустить тесты примера

Тестам нужен локальный мок-сервер JJ — тот же, что и в обычном проекте:

```sh
# из корня репозитория
python -m jj -H 127.0.0.1 -p 8080 &

JJ_REMOTE_MOCK_URL=http://127.0.0.1:8080 \
    pytest examples/consumer/tests
```

Каталог примера сам попадает в `sys.path` через
[`conftest.py`](conftest.py) — в настоящем проекте этого файла не будет, там
`app_contracts` и так свой пакет.

Зависимости примера: `openapi-contract-fixtures[d42,jj]` и любой HTTP-клиент
(здесь — `aiohttp`, он и так приезжает вместе с `jj`).

То же самое, но автоматически, делает
[`tests/test_example_consumer.py`](../../tests/test_example_consumer.py) в
корневом наборе тестов: он поднимает мок-сервер, прогоняет тесты примера и
отдельно проверяет, что закоммиченные артефакты актуальны.

## Что показано в тестах

### 1. Точка входа — generated namespace

```python
from app_contracts.generated import operations

handle = operations.api.list_documents
```

Имена берутся из **ключей manifest**, а не из `operationId`. Если бэкенд
переименует `operationId`, упадёт генерация (`ManifestBindingError` с указанием,
что именно изменилось), а публичное имя в тестах останется прежним.

### 2. Канонический вид мока

```python
async with operations.api.list_documents.mock(
    response=response,
    path_params={"workspaceId": WORKSPACE_ID},
    wait_for_requests=1,
) as mock:
    page = await fetch_documents(limit=3)
```

Что происходит вокруг этого блока без единой строки кода в тесте:

* тело ответа проверяется по сгенерированной JSON Schema **и** по d42 — до того,
  как мок будет зарегистрирован (невалидная фикстура не доезжает до сервера);
* `path_params` сверяются с контрактом: параметра нет в маршруте — ошибка сразу;
* на выходе из блока каждый перехваченный запрос проверяется целиком — метод,
  маршрут, path/query/header/cookie-параметры и их сериализация, content type и
  тело.

`wait_for_requests` — это **синхронизация жизненного цикла**, а не ассерт: он
ждёт «минимум N запросов». Точное количество проверяет тест (см. пункт 5).

### 3. Тонкая обёртка `mocked_*`

```python
def mocked_list_documents(*, response, workspace_id=WORKSPACE_ID, wait_for_requests=1):
    return operations.api.list_documents.mock(
        response=response,
        path_params={"workspaceId": workspace_id},
        wait_for_requests=wait_for_requests,
    )
```

Одна строка, за которой прячется весь переезд: проекты, где уже написаны сотни
`async with mocked_list_documents(...)`, получают проверки по контракту, не
переписывая сценарии.

### 4. `overlay_generators` + `EACH`

```python
readable = overlay_generators(
    generated,
    {("items", EACH, "title"): schema.str("Годовой отчёт")},
)
```

Сгенерированная схема описывает контракт, но не описывает *осмысленные* данные:
`schema.str.len(1, 120)` даст «9-hK_2 0zQ», и на скриншоте теста это выглядит как
мусор. Overlay подменяет генератор **одного листа**, а `EACH` означает «каждый
элемент массива».

Структура при этом всегда остаётся сгенерированной: какие ключи есть, какие из
них обязательны, сколько элементов в списке — решает контракт, не overlay. Путь
проверяется в момент сборки overlay'я: переименовали поле в спецификации —
падает импорт модуля с overlay'ями, а не тест через неделю.

### 5. Ассерты по `mock.history`

```python
assert len(mock.history) == 1
request = mock.history[0]["request"]
assert dict(request.params)["limit"] == "3"
```

Библиотека **не** проверяет количество вызовов за вас. Она гарантирует, что
каждый перехваченный запрос соответствует контракту; сколько их должно быть и
что именно в них лежит по смыслу — знает только тест.

## Чего в примере намеренно нет

* **Waiver'ов.** У любого waiver'а есть срок жизни, и закоммиченный пример с
  waiver'ом однажды перестал бы собираться. Формат waiver'а описан комментарием
  в [`waivers.yaml`](waivers.yaml).
* **Генерации во время прогона тестов.** Артефакты — часть репозитория.
* **Импортов из `openapi_contracts.integrations.*` глубже публичных точек
  входа.** Тест примера использует только `openapi_contracts` и
  `openapi_contracts.integrations.d42`.
