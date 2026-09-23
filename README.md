# geas

Тестовые контракты, JSON Schema, d42-схемы и operation-aware моки, сгенерированные из
checked-in OpenAPI.

**Geas** — в ирландской и шотландской традиции обязательное условие, нарушение которого
неизбежно имеет последствия. Здесь такое условие — OpenAPI-контракт: geas превращает его
в исполняемые схемы и показывает место, где fixture, мок или настоящий запрос перестал ему
соответствовать.

- Версия: **0.4.0**
- Python: **3.10+**
- Ядро зависит только от `jsonschema[format-nongpl]` и `PyYAML`. d42 и JJ — опциональные extras.

## Что делает geas

Мок в тесте отдаёт тело, написанное вручную. Когда бэкенд меняет схему, мок продолжает
отдавать старое тело, и тест остаётся зелёным, хотя проверяет уже устаревшую фикстуру.
geas связывает тесты со спецификацией:

- **генерирует из OpenAPI** (Swagger 2.0 или OpenAPI 3.0.x) контракт каждой выбранной
  операции: JSON Schema и d42-схемы для тел запроса и ответа, параметров и заголовков;
- **даёт типизированный доступ** к операциям: `operations.api.create_document`;
- **проверяет моки**: тело ответа — до регистрации мока, каждый перехваченный запрос — при
  выходе из блока;
- **генерирует фикстуры** по контракту и позволяет подменить генераторы отдельных полей;
- **ловит drift в CI**: `geas check` падает, если закоммиченные артефакты разошлись со
  спецификацией.

Неподдержанная конструкция спецификации ломает генерацию и никогда не превращается молча в
«любое значение». Осознанное исключение оформляется [waiver'ом](docs/waivers.md) с
владельцем и сроком.

## Установка

```bash
pip install geas            # контракты, JSON Schema, CLI, geas check
pip install 'geas[d42]'     # + d42-схемы, фикстуры, overlay'и
pip install 'geas[jj]'      # + operation-aware моки
pip install 'geas[all]'     # всё сразу
```

| Extra | Зависимость | Что без него не работает |
| --- | --- | --- |
| — | — | всё остальное работает: manifest, генерация, JSON Schema, CLI, `validate_*` |
| `[d42]` | `d42>=2,<3` | d42-схемы, `build_fixture`, `overlay_generators`; `update`/`check`, если d42 включён хотя бы у одной операции |
| `[jj]` | `jj>=2.9,<3` | `operation.mock()` |

Без нужного extra вы получите `MissingExtraError` с командой установки, например:

```
MissingExtraError: operation-aware моки требует опциональной зависимости. Установите: pip install 'geas[jj]'
```

`MissingExtraError` наследуется и от `ContractError`, и от `ImportError`.

В тестовом проекте фиксируйте точную версию geas. Версия генератора записывается в
артефакты, и после её смены нужно выполнить `geas update`.

## Быстрый старт

```bash
# 1. manifest и waivers
geas -m contracts/manifest.yaml init \
    --source ../api/openapi.yaml \
    --package app.contracts.generated \
    --directory ../app/contracts/generated

# 2. какие операции есть в спецификации
geas -m contracts/manifest.yaml list

# 3. добавить нужную операцию
geas -m contracts/manifest.yaml add api.createDocument --source main --operation-id createDocument

# 4. сгенерировать артефакты
geas -m contracts/manifest.yaml update

# 5. проверить, что артефакты совпадают со спецификацией (это же ставится в CI)
geas -m contracts/manifest.yaml check
```

Закоммитьте спецификацию, manifest, `waivers.yaml` и generated-каталог. Исключите
generated-каталог из автоформатирования, иначе `check` будет падать:

```toml
[tool.ruff]
extend-exclude = ["**/generated/**"]
```

Все команды и флаги описаны в [docs/cli.md](docs/cli.md). Если в проекте уже есть моки,
написанные вручную, воспользуйтесь [docs/migration.md](docs/migration.md). Полный рабочий
пример лежит в [examples/consumer](examples/consumer).

## Manifest

Manifest описывает, что генерировать и к какой операции спецификации привязан каждый
ключ. Все пути в нём отсчитываются от каталога самого manifest.

```yaml
version: 1

output:
  directory: app_contracts/generated     # generated-каталог
  package: app_contracts.generated       # имя, под которым его импортируют тесты

waivers: waivers.yaml                    # по умолчанию waivers.yaml рядом с manifest

policies:                                # необязательно; здесь значения по умолчанию
  waiver_max_days: 90
  waiver_warn_days: 14
  unknown_formats: reject

sources:
  main:
    path: api/openapi.yaml
    selection: explicit                  # explicit (по умолчанию) или all
    # root: api                          # корень для межфайловых $ref; по умолчанию каталог спецификации
    # base_path: /api/v1                 # префикс маршрутов

operations:
  api.createDocument:                    # стабильный ключ операции
    source: main
    operation_id: createDocument
    method: POST
    path: /api/v1/workspaces/{workspaceId}/documents
    request: {content_type: application/json}
    responses:
      - {status: 201, content_type: application/json}
    python_path: [api, create_document]
    # non_waivable: [...]                # поля, которые нельзя ослабить, см. docs/waivers.md
    # d42: false                         # не генерировать d42-схемы для операции
```

Записи операций обычно создаёт `geas add`, но их можно писать и вручную.

| Ключ операции | Значение |
| --- | --- |
| `source` | имя источника; обязательно |
| `operation_id` | `operationId` в спецификации |
| `method`, `path` | маршрут; `path` указывается полностью, с префиксом источника |
| `request.content_type` | закрепить одно тело запроса; остальные в контракт не попадут |
| `responses` | закрепить варианты ответа (`status` — число или `default`, `content_type` необязателен); без списка берутся все |
| `python_path` | путь в generated namespace, минимум два сегмента; по умолчанию каждый сегмент ключа переводится в snake_case (`api.createDocument` → `api.create_document`) |
| `non_waivable` | поля, которые нельзя ослабить waiver'ом, см. [docs/waivers.md](docs/waivers.md#non_waivable) |
| `d42` | `false` — не генерировать d42-схемы для операции |

Операция связывается со спецификацией по `operation_id`, по паре `method` + `path` или по
всем трём. Если один `operationId` встречается у нескольких ручек, укажите `method` и `path`.

Ключ операции — её стабильное имя в вашем проекте. Имя в тестах строится из ключа, а не
из `operationId`, поэтому переименование `operationId` на бэкенде ломает привязку, но не
меняет имя в тестах.

Префикс маршрута: в Swagger 2.0 `basePath` входит в маршрут автоматически, в OpenAPI 3.0
`servers` игнорируется. Чтобы задать или переопределить префикс, используйте `base_path`
у источника.

### Режимы выбора: `explicit` и `all`

**`explicit`** (по умолчанию) генерирует только операции из `operations`. На каждом
прогоне каждая запись сверяется со спецификацией: `operationId`, метод, маршрут, content
type запроса и закреплённые варианты ответа. Любое расхождение — ошибка привязки, и
генерация падает. Подходит для большой спецификации, из которой нужна малая часть.

Если в manifest уже есть операции, каждый `explicit`-источник должен использоваться хотя
бы одной из них. Иначе это ошибка manifest: скорее всего, опечатка в имени источника.

**`all`** генерирует все операции источника под ключами `<источник>.<operationId>`.
Каждой операции нужен уникальный `operationId`. Запись в `operations` необязательна: она
нужна, только чтобы задать `python_path`, сузить варианты ответа, объявить `non_waivable`
или отключить d42. Подходит для небольшой спецификации, которую ведёт та же команда.

## Использование в тестах

### Операции

```python
from app_contracts.generated import operations

op = operations.api.create_document

op.key  # 'api.createDocument'
op.method  # 'POST'
op.path  # '/api/v1/workspaces/{workspaceId}/documents'
op.request.path  # path-параметры: (ParameterView(name='workspaceId', ...),)
op.request.query  # query-параметры; есть также header и cookie
op.request.body()  # RequestBodyView: content_type, required, json_schema, d42_export
op.responses  # все варианты ответа
op.response(status=201)  # один вариант; если вариант один, аргументы можно опустить
op.unsupported  # варианты, которые нельзя выразить контрактом, с причинами
```

`response()` и `request.body()` выбирают вариант сами, если он единственный. Если
вариантов несколько, передайте `status`/`content_type`, иначе будет
`ResponseVariantError`.

`operations` — статический модуль: IDE и mypy видят все операции. Для инструментов есть
строковый доступ: `operations.by_key("api.createDocument")` и `operations.keys()`.

### Моки

Нужен extra `[jj]`.

```python
async with operations.api.create_document.mock(
    response=response_body,
    status=201,
    path_params={"workspaceId": workspace_id},
    wait_for_requests=1,
) as mock:
    await page.submit()

assert len(mock.history) == 1
request = mock.requests[0]
```

| Аргумент | Значение |
| --- | --- |
| `response` | тело ответа; проверяется по контракту до регистрации мока |
| `status` | HTTP-статус; он же выбирает вариант ответа |
| `content_type` | выбирает вариант ответа, если по статусу их несколько |
| `path_params` | значения сегментов маршрута; проверяются по схемам параметров |
| `query_params` | сузить matcher по query-параметрам |
| `headers` | сузить matcher по заголовкам запроса |
| `response_headers` | заголовки ответа; `Content-Type` подставляется из контракта, если не задан |
| `wait_for_requests` | сколько запросов дождаться перед выходом из блока |
| `timeout` | сколько ждать запросы, секунд; по умолчанию `5.0` |
| `history_callback` | функция, которая получит историю после снятия мока, например для вложения в Allure |

**До входа в блок** мок проверяет версию JJ (`>=2.9,<3`), `path_params`, выбор варианта
ответа, тело ответа (по JSON Schema, а при установленном `[d42]` — ещё и по d42-схеме),
статус (`100..599`) и `Content-Type` из `response_headers`. Неверный мок не доходит до
приложения:

```
ResponseContractError: тело ответа 201:application/json: 'id' is a required property
  ожидалось: ['id', 'title', 'visibility']
  фактически: {'nope': 1} [operation=api.createDocument, direction=response, pointer=/, schema_pointer=/required]
```

**При выходе из блока** мок дожидается `wait_for_requests` запросов, снимается (всегда,
даже если тест упал), сохраняет историю в `mock.history` и `mock.requests` и проверяет
**каждый** перехваченный запрос: метод, маршрут, параметры с их сериализацией и
обязательностью, тело и его `Content-Type`.

```
RequestContractError: тело запроса application/json: 'title' is a required property
  ожидалось: ['title']
  фактически: {} [operation=api.createDocument, direction=request, pointer=/, schema_pointer=/required]
```

Если тело блока бросило исключение, оно остаётся главным. Ошибки мока прикладываются к
нему заметками (`add_note`, Python 3.11+) и доступны в `mock.diagnostics`.

**`wait_for_requests` не проверяет количество вызовов.** Он только ждёт, пока приложение
отправит запросы, и падает, если их пришло меньше N:

```
ContractMockError: ожидалось минимум 1 запрос(ов), перехвачено 0 [operation=api.createDocument]
```

Три запроса вместо одного он пропустит. Точное количество проверяйте сами:
`assert len(mock.history) == 1`.

Ограничения:

- мок создаётся только из generated-операции; готовый `jj.Mocked` передать нельзя;
- в один мок нельзя войти дважды, для повторного использования создайте новый;
- значение `path_params` не может содержать `/`;
- для варианта ответа `default` передайте конкретный числовой `status`.

### Проверка без мока

```python
op.validate_response(body, status=201)  # ResponseContractError, если тело не подходит
op.validate_request_body(payload)  # RequestContractError, если тело не подходит
```

Без extras проверка идёт по JSON Schema. При установленном `[d42]` добавляется проверка
по d42-схеме.

### d42-схемы и фикстуры

Нужен extra `[d42]`.

```python
from geas import Direction
from geas.integrations.d42 import build_fixture

DocumentSchema = operations.api.create_document.d42_schema(Direction.RESPONSE)
body = build_fixture(DocumentSchema)
```

Если в направлении несколько d42-схем (например, у ответов 200 и 400 разные тела), без
`export` будет ошибка со списком схем. Передайте имя нужной:

```python
ErrorSchema = op.d42_schema(Direction.RESPONSE, export=op.response(status=400).d42_export)
```

`build_fixture` детерминирован: одна и та же схема с тем же `seed` (аргумент
необязателен) всегда даёт одно и то же значение. Необязательные поля в фикстуру не
попадают, поэтому добавление необязательного поля в спецификацию фикстуру не меняет.

Для некоторых операций d42-схемы не создаются. Это бывает с рекурсивными схемами и с
конструкциями, которые d42 не умеет выражать: несливаемым `allOf`, `multipleOf`,
`pattern` вместе с границами длины. Причину печатает `geas update`, и её же называет
ошибка `d42_schema()`. Моки и `validate_*` для таких операций работают по JSON Schema.

### Overlay'и: осмысленные значения в фикстурах

Сгенерированная схема даёт для строкового поля случайную строку. Чтобы фикстура выглядела
осмысленно, подмените генератор отдельного поля. Остальной контракт при этом не
меняется:

```python
from d42 import schema

from geas import Direction
from geas.integrations.d42 import EACH, build_fixture, overlay_generators

DocumentPageSchema = overlay_generators(
    operations.api.list_documents.d42_schema(Direction.RESPONSE),
    {
        ("items", EACH, "title"): schema.str("Отчёт за квартал"),
    },
)

build_fixture(DocumentPageSchema)
# {'items': [{'id': '5x PCZJr_…', 'title': 'Отчёт за квартал', 'visibility': 'private'}, …], 'total': …}
```

- Путь — кортеж имён полей; `EACH` означает «каждый элемент массива». Вложенные массивы
  поддерживаются: `("a", EACH, "b", EACH, "c")`.
- Подменить можно только лист. Структура, обязательность полей и nullable остаются
  сгенерированными.
- Путь и тип проверяются при вызове `overlay_generators`, то есть при импорте модуля с
  overlay'ями. Если поле переименовали в спецификации, упадёт импорт, а не тест через
  неделю. Для `enum` подменённые значения должны входить в список допустимых.
- Границы и `pattern` проверяются в `build_fixture`: значение сверяется с исходным
  контрактом.
- Результат — обычная d42-схема, с ним работают `fake()`, `%` и `make_required()`.
  Применяйте `%` и `make_required()` **до** `overlay_generators`: они создают новую схему,
  и проверка по исходному контракту на ней пропадёт.

Ошибки, которые вы можете увидеть:

```
ContractOverlayError: overlay /nope: ключа 'nope' нет в сгенерированной схеме. Доступны: 'items', 'total'
ContractOverlayError: overlay /items: подменить можно только генератор листа, а здесь ListSchema — структура всегда остаётся сгенерированной
ContractOverlayError: overlay /items/-/title: тип ручной схемы не совпадает с контрактом. Ожидалось: StrSchema; получено: IntSchema
ContractOverlayError: значение, выданное ручным генератором overlay'я, нарушает сгенерированный контракт: - Value <class 'str'> at _['items'][0]['title'] must have at least 1 element, but it has 0 elements
```

### Как посмотреть контракт

Чтобы увидеть, какие поля разрешает контракт, не открывая generated-файлы:

```python
print(operations.api.create_document.describe_response(status=201))
print(operations.api.create_document.describe_request_body())
```

```
api.createDocument
Response 201 application/json

$ref #/$defs/Document; object; дополнительные свойства разрешены
├── id — required; string; minLength 1
├── title — required; string; minLength 1; maxLength 120
└── visibility — required; string; enum["private", "workspace", "public"]

JSON Schema:
  /…/app_contracts/generated/contracts/api__create_document.json#/responses/0/schema

d42:
  app_contracts.generated._d42.api__create_document_response:GeneratedDocumentSchema

d42 source:
  /…/app_contracts/generated/_d42/api__create_document_response.py
```

То же из терминала: `geas show api.createDocument --status 201`, см.
[docs/cli.md](docs/cli.md#show). Координаты доступны и программно — у
`op.response(...)` и `op.request.body()` есть `contract_file`, `json_pointer`,
`contract_path`, `d42_module`, `d42_export`, `d42_reference` и `d42_source_path`. Если
d42-схемы у варианта нет, d42-координаты равны `None`. Не разбирайте текст `describe()` в
тестах: для программного доступа есть `json_schema` и `geas show --json`.

## Generated-артефакты

`geas update` пишет в `output.directory`:

| Файл | Что это |
| --- | --- |
| `__init__.py` | экспорт `operations`, `Operations` и `REGISTRY` |
| `operations.py` | типизированный namespace операций |
| `_registry.py` | индекс «ключ операции → файл контракта» |
| `contracts/<slug>.json` | контракт операции: JSON Schema тел, параметров и заголовков, закреплённые варианты, статус d42 |
| `_d42/<slug>_request.py`, `_d42/<slug>_response.py` | d42-схемы; создаются, только если d42 включён и в направлении есть тело |
| `_generated.json` | индекс набора: версия формата и генератора, отпечатки, список файлов, которыми владеет geas |

Артефакты детерминированы: повторный `update` без изменения входов даёт те же байты.
`update` удаляет только файлы, которые сам создал раньше. Руками артефакты не правят:
`check` уронит CI на любом расхождении.

## CI

```yaml
- name: Контракты соответствуют спецификации
  run: geas -m contracts/manifest.yaml check
```

`check` ничего не меняет и возвращает `1`, если артефакты разошлись со спецификацией или
генерация не проходит: waiver просрочен, привязка операции сломалась, в спецификации
появилась неподдержанная конструкция. Если d42 включён хотя бы у одной операции, в CI
нужен extra `[d42]`. Что делать при падении, описано в
[docs/cli.md](docs/cli.md#check-упал-в-ci).

`geas diff` показывает, что именно изменилось в контракте, и отделяет смысловые изменения
от косметики.

## Waivers и `non_waivable`

Если спецификация ошибается и исправить её сейчас нельзя, выпишите waiver: точечное
послабление с владельцем, задачей и сроком. Генерация сама печатает готовую заготовку:

```
ошибка: format 'my-custom-tag' неизвестен для type='string'. ... (contract path /body/tag)
Если это осознанное исключение, добавьте в waivers.yaml:
  - operation: api.getDocumentTag
    direction: response
    json_pointer: /body/tag
    rule: allow_unknown_format
    expected_source: 77111c9dbf349c0c707b300581710f165b8d3a011a3764b9fd94e518f3a429a2
    reason: <зачем>
    owner: <кто отвечает>
    issue: <ссылка>
    expires_at: <YYYY-MM-DD>
```

Просроченный, ненужный или устаревший waiver ломает генерацию. `non_waivable` в manifest
закрепляет поля, которые ослабить нельзя. Как выписывать waiver'ы, какие есть правила и
что делать с ошибками — в [docs/waivers.md](docs/waivers.md).

## Что поддерживается

Swagger 2.0 и OpenAPI 3.0.x: `$ref` (локальные и межфайловые внутри корня источника),
`allOf`, `oneOf`, `anyOf`, `discriminator`, `nullable`/`x-nullable`,
`readOnly`/`writeOnly`, `enum`, `format`, `pattern`, границы длины и чисел,
`additionalProperties`, параметры в path/query/header/cookie, `collectionFormat`.

Отклоняются с диагностикой: OpenAPI 3.1, `not`, `if`/`then`/`else`, `const`, булевы
схемы, `type` списком, `deepObject`/`label`/`matrix`, `in: formData`, диапазоны статусов
`2XX`, внешние HTTP-`$ref` и `$ref` за пределы корня источника.

Полная таблица — [docs/support-matrix.md](docs/support-matrix.md).

## Ограничения

- Мок подключается только через `async with …mock(…)`. Декоратора и persistent-моков нет.
- OpenAPI 3.1 не поддерживается.
- Параметры — только скаляры и массивы скаляров.
- Тело — только JSON: `application/json`, `…+json`, в Swagger 2.0 ещё `*/*`. Остальные
  варианты помечаются как непредставимые, но операция генерируется.
- geas не обращается к сети: внешние `$ref` запрещены, спецификация читается только с
  диска.

## Документация

- [docs/cli.md](docs/cli.md) — команды, флаги, коды возврата, типовые сценарии
- [docs/waivers.md](docs/waivers.md) — waiver'ы, `non_waivable`, политики
- [docs/migration.md](docs/migration.md) — подключение к проекту с существующими моками
- [docs/support-matrix.md](docs/support-matrix.md) — матрица поддержки OpenAPI 3.0 / Swagger 2.0
- [examples/consumer](examples/consumer) — рабочий пример проекта-потребителя
- [CHANGELOG.md](CHANGELOG.md) — история изменений

## Разработка

```bash
make install     # venv + editable-установка с dev-зависимостями
make lint        # ruff check + ruff format --check
make typecheck   # mypy strict
make test        # pytest
make check       # всё сразу
```

Устройство пакета:

```
geas/                  ядро: manifest, нормализация, JSON Schema, артефакты, CLI
geas/dialects/         адаптеры Swagger 2.0 и OpenAPI 3.0
geas/integrations/d42/ d42-схемы, overlay'и, фикстуры (extra [d42])
geas/integrations/jj/  моки (extra [jj])
```

Ядро не импортирует `d42` и `jj`: тесты проверяют это в подпроцессе, где оба модуля
заблокированы. Тесты не ходят в сеть — фикстура `no_outbound_network` разрешает только
loopback для локального сервера моков. Синтетические спецификации для тестов лежат в
`tests/fixtures/specs/`.

Архитектурные решения и их обоснования — [docs/adr/](docs/adr/).
