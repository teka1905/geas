# openapi-contract-fixtures

Тестовые контракты, JSON Schema, d42-схемы и operation-aware моки, сгенерированные из
checked-in OpenAPI.

- Версия: **0.1.0**
- Python: **3.10+**
- Ядро зависит только от `jsonschema[format]` и `PyYAML`. d42 и JJ — опциональные extras.

---

## 1. Зачем это нужно: contract drift

Тест поднимает мок и кладёт в него руками написанное тело ответа. Бэкенд меняет схему:
переименовывает поле, делает его nullable, убирает из `required`, добавляет вариант в
`enum`. Мок продолжает отдавать старое тело, тест продолжает быть зелёным — и остаётся
зелёным ровно до того момента, когда фича доезжает до продакшена. Это и есть **contract
drift**: тест проверяет не сервис, а собственную устаревшую фикстуру.

Библиотека закрывает это одним воспроизводимым пайплайном:

```
checked-in OpenAPI
  → нормализованный контракт
  → generated JSON Schema и d42
  → generator overlays
  → generated operation handles
  → operation-aware JJ-моки
  → проверка drift в CI
```

Что даёт каждое звено:

| Звено | Что происходит |
| --- | --- |
| **checked-in OpenAPI** | спецификация лежит в репозитории; сеть не используется никогда |
| **нормализованный контракт** | Swagger 2.0 и OpenAPI 3.0.x сводятся к одному IR; неподдержанная конструкция — ошибка с точным адресом, а не «любое значение» |
| **generated JSON Schema** | точная семантика `oneOf`, `discriminator`, `format` и границ; работает без extras |
| **generated d42** | параллельное представление того же контракта для генерации фикстур |
| **generator overlays** | подмена генератора отдельного листа (осмысленные названия вместо `9-hK_2 0zQ`) без правки generated-файлов |
| **generated operation handles** | статический typed namespace: `operations.ws2.add_ticket` |
| **operation-aware JJ-моки** | тело мока валидируется **до** регистрации, каждый перехваченный запрос — **после** выхода из блока |
| **проверка drift в CI** | `openapi-contracts check` роняет сборку, если закоммиченные артефакты разошлись со спецификацией |

Главный принцип — **fail closed**. Ни одна неподдержанная конструкция не превращается
молча в «принимает что угодно». Единственное послабление — явный, срочный и закреплённый
[waiver](docs/waivers.md).

---

## 2. Быстрый старт

### Установка

```bash
pip install openapi-contract-fixtures            # ядро: JSON Schema + CLI + drift-check
pip install 'openapi-contract-fixtures[d42]'     # + генерация d42-схем и фикстур
pip install 'openapi-contract-fixtures[jj]'      # + operation-aware моки
pip install 'openapi-contract-fixtures[all]'     # всё сразу
```

### `init` — каркас manifest и waivers

```bash
openapi-contracts -m manifest.yaml init \
    --source api/openapi.yaml \
    --directory demo/generated \
    --package demo.generated
```

```
создан manifest.yaml
создан waivers.yaml

Дальше:
  openapi-contracts -m manifest.yaml list
  openapi-contracts -m manifest.yaml add <ключ> --source main --operation-id <id>
  openapi-contracts -m manifest.yaml update
  openapi-contracts -m manifest.yaml check   # это и ставится в CI
```

Получившийся `manifest.yaml`:

```yaml
version: 1
output:
  directory: demo/generated
  package: demo.generated
waivers: waivers.yaml
policies:
  waiver_max_days: 90
  unknown_formats: reject
sources:
  main:
    path: api/openapi.yaml
    selection: explicit
operations: {}
```

### `list` — что вообще есть в источнике

```bash
openapi-contracts -m manifest.yaml list
```

```
источник main: api/openapi.yaml [openapi30]
  режим выбора: explicit, операций: 3
    GET     /api/v1/workspaces/{workspaceId}/documents
      operationId=listDocuments  ключ=-
      responses: 200:application/json, 400:application/json
  * POST    /api/v1/workspaces/{workspaceId}/documents
      operationId=createDocument  ключ=api.createDocument
      request: application/json
      responses: 200:application/json, 400:application/json

* — операция выбрана manifest и попадает в generated-артефакты
```

### `add` — положить операцию в allowlist

```bash
openapi-contracts -m manifest.yaml add ws2.addTicket \
    --source main --operation-id addTicket
```

```
операция ws2.addTicket добавлена в /path/to/manifest.yaml
Теперь запустите 'openapi-contracts update', чтобы обновить артефакты
```

`add` **транзакционен**: операция целиком нормализуется и рендерится во временный каталог
до того, как manifest будет тронут. Если конструкция не поддержана, manifest остаётся
байт-в-байт прежним, а в stderr печатается ошибка с готовым рецептом waiver'а.

### `update` — сгенерировать артефакты

```bash
openapi-contracts -m manifest.yaml update
```

```
обновлено файлов: 8 в /path/to/demo/generated
```

### `check` — проверить, что закоммиченное совпадает со спецификацией

```bash
openapi-contracts -m manifest.yaml check
```

```
артефакты актуальны: 8 файл(ов)
```

Расхождение печатается в stderr и даёт код возврата `1`:

```
generated-артефакты разошлись со спецификацией:
  отличается:  contracts/api__delete_document.json

Запустите 'openapi-contracts update' и закоммитьте результат
```

Полный справочник по командам и флагам — [docs/cli.md](docs/cli.md).

---

## 3. Архитектура

```
openapi_contracts/                  ядро: manifest, IR, нормализация, JSON Schema, артефакты, CLI
openapi_contracts/dialects/         адаптеры диалектов: swagger2, openapi30
openapi_contracts/integrations/d42/ опционально: IR → d42 2.x, рендер, overlays, фикстуры
openapi_contracts/integrations/jj/  опционально: OperationHandle.mock() → JJ
```

**Ядро независимо.** Оно импортирует только `jsonschema` и `PyYAML`. Ни один модуль ядра
не импортирует `d42` или `jj` на уровне модуля. Отложенный импорт есть ровно в трёх точках:
`OperationHandle.mock()`, `OperationHandle.d42_schema()` и рендер d42-модулей внутри
`render_artifacts`. Если extra не установлен, поднимается `MissingExtraError` с точной
командой установки.

**Гарантия.** Ядро и generated-реестр импортируются на голом окружении без d42 и без JJ.
Это проверяется тестами, которые импортируют пакет в подпроцессе с заблокированными
`d42` и `jj` в `sys.meta_path`.

**Адаптеры диалектов.** Диалект отвечает ровно за одно: привести свой документ к
диалектно-нейтральной `RawOperation`. Ниже по стеку — нормализация, JSON Schema, d42,
runtime, CLI — про версию спецификации уже не знают. Различия (`definitions` против
`components.schemas`, `in: body` против `requestBody`, `collectionFormat` против
`style`/`explode`, `x-nullable` против `nullable`, `consumes`/`produces` против `content`)
исчезают на границе `dialects/`.

Подробное обоснование решений — [ADR 0001](docs/adr/0001-architecture.md).

---

## 4. Режимы выбора операций: `explicit` и `all`

Режим задаётся у источника — `sources.<name>.selection`.

### `explicit` (по умолчанию)

Генерируются **только** операции, перечисленные в `operations`. Запись одновременно
работает как allowlist и как закрепление привязки: `operation_id`, `method`, `path`,
`request.content_type` и выбранные `responses` сверяются со спецификацией на каждом
прогоне. Любое расхождение — `ManifestBindingError`, генерация падает.

```yaml
sources:
  main: {path: api/openapi.yaml, selection: explicit, base_path: /api/v1}
operations:
  ws2.addTicket:
    source: main
    operation_id: addTicket
    method: POST
    path: /api/v1/queues/{queueId}/tickets
    request: {content_type: application/json}
    responses:
      - {status: 200, content_type: application/json}
    python_path: [ws2, add_ticket]
```

Операция связывается со спецификацией либо по `operation_id`, либо по паре
`method` + `path`; без того и без другого запись отклоняется. Если один `operationId`
встречается в источнике несколько раз, требуется уточнить `method` и `path`.

Ключ операции задаётся вручную и является стабильным именем: generated Python path
строится из **ключа**, а не из `operationId`. Переименовали `operationId` на бэкенде —
падает binding, но публичное Python-имя само не меняется.

Если в manifest уже есть операции, но какой-то `explicit`-источник не покрыт ни одной из
них, это ошибка manifest (почти наверняка опечатка в имени источника). Пустой
`explicit`-источник допустим только пока `operations` пуст — то есть сразу после `init`.

### `all`

Генерируются все операции источника. Ключ строится автоматически как
`<имя источника>.<operationId>`, поэтому:

- операция **без** `operationId` — ошибка (ключ невозможно построить детерминированно);
- дублирующийся `operationId` внутри источника — ошибка.

Запись в `operations` для такого источника необязательна и нужна только чтобы уточнить
`python_path`, сузить набор вариантов ответа, задать `non_waivable` или выключить d42.

```yaml
sources:
  main: {path: api/openapi.yaml, selection: all}
operations: {}
```

```
источник main: api/openapi.yaml [openapi30]
  режим выбора: all, операций: 2
  * GET     /trees
      operationId=getTree  ключ=main.getTree
```

**Что выбирать.** `explicit` — для большой чужой спецификации, где нужны три операции из
двухсот и важно, чтобы изменение привязки ломало сборку. `all` — для маленькой
спецификации, которую ведёт та же команда.

---

## 5. CLI

```
openapi-contracts [-h] [--version] [-m MANIFEST] {init,list,inspect,add,update,check,diff}
```

| Команда | Что делает |
| --- | --- |
| `init` | создать минимальный `manifest.yaml` и `waivers.yaml`, ничего не затирая |
| `list` (алиас `inspect`) | показать источники, операции, варианты и причины неподдержки |
| `add` | транзакционно добавить операцию в explicit-allowlist |
| `update` | детерминированно перегенерировать артефакты |
| `check` | проверить рабочее дерево на drift, ничего не меняя |
| `diff` | семантический diff контрактов относительно закоммиченных артефактов |

Коды возврата:

| Код | Значение |
| --- | --- |
| `0` | успех, расхождений нет |
| `1` | ошибка контракта: drift, binding, waiver, неподдержанная конструкция |
| `2` | ошибка использования CLI |

Полный справочник по флагам — [docs/cli.md](docs/cli.md).

---

## 6. Generated-артефакты

`update` пишет в `output.directory` следующий набор:

| Файл | Зачем он |
| --- | --- |
| `__init__.py` | реэкспорт `operations`, `Operations` и `REGISTRY` |
| `operations.py` | статический typed namespace операций; атрибуты объявлены как `property`, поэтому тип виден IDE и mypy, а документ контракта читается лениво |
| `_registry.py` | индекс «ключ операции → слаг файла» и сам `OperationRegistry` |
| `contracts/<slug>.json` | нормализованный контракт операции; **внутри лежат JSON Schema** тела запроса, тел ответов и всех параметров |
| `_d42/<slug>_request.py`, `_d42/<slug>_response.py` | generated d42-схемы направления; создаются, только если d42 включён и в направлении есть тело |
| `_d42/__init__.py` | пакет d42-модулей; появляется только вместе с ними |
| `_generated.json` | описание набора: `artifact_format`, `generator`, `output`, `operations` (`slug` + семантический `fingerprint`), общий `fingerprint`, список `owned`-файлов и `digests` |

Свойства набора:

- **детерминированность** — стабильная сортировка, UTF-8, `\n`, ни timestamp, ни абсолютных
  путей, ни случайных значений. Повторный `update` без изменения входов даёт байт-в-байт
  тот же результат;
- **атомарность записи** — сначала рендерится весь набор (все ошибки случаются здесь),
  потом каждый файл пишется через временный файл рядом и `os.replace`;
- **owned-файлы** — удаляются только те файлы, которые генератор сам записал в прошлый раз
  (список `owned` в `_generated.json`), с проверкой на выход за каталог и на symlink.

Файлы помечены шапкой «сгенерировано автоматически»; править их руками бессмысленно —
`check` уронит CI на расхождении.

### Как этим пользоваться

```python
from demo.generated import operations

op = operations.ws2.add_ticket
op.key  # 'ws2.addTicket'
op.method  # 'POST'
op.path  # '/api/v1/queues/{queueId}/tickets'
op.request.path  # (ParameterView(name='queueId', ...),)
op.request.query  # параметры query
op.request.body()  # RequestBodyView: content_type, required, json_schema, d42_export
op.responses  # (ResponseView(status=200, content_type='application/json', ...),)
op.response(status=200)  # выбор варианта; при единственном варианте аргументы можно опустить
op.unsupported  # варианты, не представимые контрактом, с причинами
```

Валидация вручную, без мока:

```python
op.validate_response(body, status=200)
op.validate_request_body(payload)
```

Generated d42-схема варианта — через `d42_schema()`; имя переменной берётся из самого
контракта (`d42_export`), угадывать его не нужно:

```python
from openapi_contracts import Direction

variant = op.response(status=200)
GeneratedTicketSchema = op.d42_schema(Direction.RESPONSE, export=variant.d42_export)
```

> `export` обязателен: вызов `op.d42_schema(Direction.RESPONSE)` без него всегда поднимает
> `OperationLookupError`, несмотря на то что у параметра есть значение по умолчанию.

Строковый доступ `operations.by_key("ws2.addTicket")` и `operations.keys()` оставлены как
escape hatch для инструментов; в тестах используйте generated namespace.

---

## 7. Канонический API моков

```python
async with operations.ws2.add_ticket.mock(
    response=response_body,
    path_params={"queueId": queue_id},
    wait_for_requests=1,
) as mock:
    await page.submit()

assert len(mock.history) == 1  # cardinality-ассерт пишет тест
```

Аргументы `mock()`:

| Аргумент | Смысл |
| --- | --- |
| `response` | тело ответа; проверяется по контракту **до** регистрации мока |
| `status` | HTTP-статус; он же сужает выбор варианта ответа |
| `content_type` | сужает выбор варианта ответа |
| `path_params` | закрепляемые сегменты маршрута; проверяются по схеме параметра |
| `query_params` | сузить matcher по query-параметрам |
| `headers` | сузить matcher по заголовкам **запроса** |
| `response_headers` | заголовки ответа; `Content-Type` подставляется из контракта, если не задан |
| `wait_for_requests` | сколько запросов дождаться перед выходом из блока |
| `timeout` | таймаут ожидания, секунды (по умолчанию `5.0`) |

### Жизненный цикл

**До входа в блок** (внутри `mock()`, ещё до регистрации мока в JJ):

1. проверяется версия установленного JJ (проверенный диапазон — `>=2.9,<3`);
2. проверяется, что все `path_params` описаны маршрутом и каждое значение валидно по схеме
   своего параметра;
3. выбирается вариант ответа по `status`/`content_type`; неоднозначный выбор — ошибка,
   «молча взять первый» библиотека не умеет;
4. тело ответа валидируется по **JSON Schema** контракта;
5. тело ответа независимо валидируется по **generated d42** — если extra `[d42]` установлен
   и для операции d42 сгенерирован. Два пути не дублируют друг друга: d42-проекция `oneOf`
   шире исходной семантики, и точность держит именно JSON Schema;
6. проверяются диапазон статуса (`100..599`) и совпадение `Content-Type` ответа с контрактом.

Невалидное тело ответа падает здесь — до приложения оно не доезжает:

```
ResponseContractError: тело ответа 200:application/json: 'id' is a required property
  ожидалось: ['id', 'slug', 'title']
  фактически: {'nope': 1} [operation=ws2.addTicket, direction=response, ...]
```

**На входе в блок**: строятся matcher (метод, маршрут с подставленными `path_params`,
`query_params`, `headers`) и ответ; мок регистрируется как `disposable` с
`prefetch_history`.

**На выходе из блока**:

7. если тело сценария не бросило исключение и задан `wait_for_requests` — дожидаемся
   запросов с `timeout`;
8. мок снимается (cleanup/deregister) — всегда, даже если тело сценария упало;
9. забирается история; она остаётся доступной через `mock.history` и `mock.requests`;
10. если задан `wait_for_requests`, а перехвачено меньше — ошибка;
11. **каждый** перехваченный запрос валидируется по контракту: метод, маршрут,
    path/query/header/cookie-параметры с их сериализацией и обязательностью, тело вместе с
    `Content-Type`.

```
RequestContractError: request #0: тело запроса обязательно, но запрос пришёл без тела
  ожидалось: тело ['application/json']
  фактически: пусто [operation=ws2.addTicket, direction=request, pointer=/body]
```

Если тело сценария бросило исключение, оно остаётся **первичным**: cleanup всё равно
выполняется, а вторичные диагностики прикладываются к нему заметками (`add_note`, Python
3.11+) и всегда доступны через `mock.diagnostics`.

### `wait_for_requests` — это не ассерт на количество

`wait_for_requests=N` решает ровно одну задачу: **синхронизацию жизненного цикла**. Он
даёт асинхронному приложению время дослать запросы до того, как мок будет снят и история
собрана. Проверяет он только нижнюю границу: «перехвачено не меньше N».

```
ContractMockError: ожидалось минимум 1 запрос(ов), перехвачено 0 [operation=ws2.addTicket]
```

Он **не заменяет** ассерт на точное количество вызовов: три запроса вместо одного
`wait_for_requests=1` пропустит молча. Cardinality проверяет тест:

```python
async with operations.ws2.add_ticket.mock(response=body, wait_for_requests=1) as mock:
    await page.submit()

assert len(mock.history) == 1
request = mock.requests[0]
assert request.method == "POST"
```

### Ограничения мока

- Готовый чужой `jj.Mocked` библиотека не принимает: восстановить контракт интроспекцией
  matcher'а и response'а невозможно, поэтому мок всегда строится из `OperationHandle`.
- В один `ContractMock` нельзя войти дважды — создайте новый.
- Persistent-моки не поддерживаются (см. «Ограничения v0.1»).
- Значение `path_params`, содержащее `/`, отклоняется: оно разбило бы маршрут на лишние
  сегменты.
- Если выбран вариант ответа `default`, конкретный HTTP-статус нужно передать аргументом
  `status`.

---

## 8. Overlays: осмысленные данные без потери контракта

Сгенерированная схема описывает контракт, но не описывает *осмысленные* данные:
`schema.str` в поле «название» даст `"9-hK_2 0zQ"`, и по такому скриншоту тест не
почитаешь. `overlay_generators` подменяет генератор **отдельного листа**, ничего не ломая
в остальном контракте.

```python
from d42 import schema

from openapi_contracts import Direction
from openapi_contracts.integrations.d42 import EACH, build_fixture, overlay_generators

op = operations.ws2.get_queue
variant = op.response(status=200)
generated = op.d42_schema(Direction.RESPONSE, export=variant.d42_export)

QueueDetailsSchema = overlay_generators(
    generated,
    {
        ("name",): schema.str("Отчёт за квартал"),
        ("groups", EACH, "title"): schema.str("Аналитика"),
    },
)

fixture = build_fixture(QueueDetailsSchema)
# {'name': 'Отчёт за квартал', 'groups': [{'title': 'Аналитика'}, {'title': 'Аналитика'}, ...]}
```

Инварианты:

1. **Подменяется только генератор листа.** Структура (словари, списки) и обязательность
   ключей всегда остаются сгенерированными; ни один объект d42 не мутируется на месте.
2. **Путь проверяется при сборке overlay'я, а не при генерации.** Поле переименовали в
   спецификации — падает импорт модуля с overlay'ями, а не тест через неделю.
3. **`EACH` — публичный маркер «каждый элемент массива»**; вложенные массивы
   поддерживаются: `("a", EACH, "b", EACH, "c")`.
4. **Через nullable спуск прозрачен**: если по пути стоит `X | schema.none`, overlay
   применяется к ветке `X`, а `schema.none` остаётся на месте — поле как было nullable, так
   и осталось.
5. **Совместимость проверяется настолько рано, насколько разрешима.** Тип ручной схемы
   обязан совпасть с типом листа; если лист — union литералов (`enum`), ручной генератор
   обязан быть его подмножеством.

Что проверить заранее нельзя — `pattern` и границы (`minLength`, `minimum`, ...): это
разрешимо только для конкретного значения. Поэтому `build_fixture` валидирует результат по
**исходному, до-overlay'ному** контракту и падает `ContractOverlayError`, если ручной
генератор вышел за его пределы. Реальные сообщения:

```
overlay /nope: ключа 'nope' нет в сгенерированной схеме. Доступны: 'groups', 'name'
overlay /groups: подменить можно только генератор листа, а здесь ListSchema —
  структура всегда остаётся сгенерированной
overlay /name: тип ручной схемы не совпадает с контрактом.
  Ожидалось: StrSchema; получено: IntSchema
значение, выданное ручным генератором overlay'я, нарушает сгенерированный контракт:
  Value <class 'str'> at _['name'] must have at least 1 element, but it has 0 elements
```

Результат overlay'я — обычная d42-схема: `fake()`, `%` (substitute) и `make_required()`
работают на ней как на любой другой. Оговорка: `%` и `make_required()` строят **новый**
объект и про запомненный исходный контракт не знают, поэтому порядок должен быть
обратным — сначала `make_required` / `%`, потом `overlay_generators`.

Пути overlay'ев записаны в той же грамматике, что и contract path у waiver'ов:
`("groups", EACH, "title")` — это `/groups/-/title`.

---

## 9. Waivers и `non_waivable`

**Waiver** — единственный способ пропустить конструкцию, которую нормализация иначе
отклонила бы. Он намеренно неудобен: обязательны владелец, причина, тикет, срок и
`expected_source` — отпечаток исходного фрагмента спецификации.

Сообщение об ошибке печатает готовый рецепт, включая точное значение `expected_source`:

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

Генерация падает, если waiver просрочен, выписан дальше `policies.waiver_max_days`,
неполон, объявлен дважды, конфликтует с другим правилом на той же точке, ссылается на
несуществующую операцию, **не понадобился** или **устарел** (исходный фрагмент изменился —
`expected_source` больше не совпадает).

**`non_waivable`** — обратная сторона: свойства контракта, которые нельзя ослабить никаким
waiver'ом. Объявляются в manifest у операции:

```yaml
operations:
  ws2.addTicket:
    source: main
    operation_id: addTicket
    non_waivable:
      - {direction: response, json_pointer: /body/rc, rules: [required, non_null, non_empty_enum]}
```

Если waiver пересекается с `non_waivable`, генерация падает; если само свойство перестало
выполняться (поле стало необязательным, стало nullable, потеряло enum) — падает тоже.

Полный формат, правила жизненного цикла и разобранный пример —
[docs/waivers.md](docs/waivers.md).

---

## 10. Интеграция с CI

Одна команда:

```yaml
- name: contract drift
  run: openapi-contracts -m contracts/manifest.yaml check
```

`check` ничего не меняет в рабочем дереве: он рендерит набор в памяти и сравнивает с тем,
что лежит на диске. Коды возврата стабильны и годятся для гейта: `0` — чисто, `1` — ошибка
контракта (drift, binding, waiver, неподдержанная конструкция), `2` — ошибка использования
CLI.

`check` ловит не только «забыли перегенерировать», но и всё, что ломает генерацию:
просроченный waiver, изменившийся `operationId`, исчезнувший вариант ответа, новую
неподдержанную конструкцию в спецификации.

Что печатается при расхождении:

```
generated-артефакты разошлись со спецификацией:
  отсутствует: contracts/ws2__add_ticket.json
  отличается:  operations.py
  лишний:      contracts/ws2__old_operation.json

Запустите 'openapi-contracts update' и закоммитьте результат
```

Полезное дополнение — `diff`: он показывает, **что именно** изменилось в контракте, и
отделяет семантику от оформления.

```
ws2.addTicket:
  [контракт] ~ /responses/0/schema/$defs/Ticket/properties/title/maxLength: 120 → 200
```

`diff` возвращает `1`, если есть семантические изменения, и `0`, если изменения только
косметические (слаг, диалект, имена d42-модулей). `--json` даёт машиночитаемый вывод с тем
же кодом возврата.

Если вы генерируете d42-артефакты, в CI-окружении должен стоять extra `[d42]` — иначе
`check` и `update` упадут (см. следующий раздел).

---

## 11. Опциональные зависимости

| Extra | Что включает | Что без него не работает |
| --- | --- | --- |
| — | ядро: manifest, нормализация, JSON Schema, артефакты, CLI, runtime-валидация | — |
| `[d42]` | `d42>=2,<3` | генерация и чтение d42-схем, `build_fixture`, `overlay_generators` |
| `[jj]` | `jj>=2.9,<3` | `OperationHandle.mock()` |
| `[all]` | оба | |
| `[dev]` | оба + pytest, ruff, mypy, build | разработка самой библиотеки |

Отсутствующий extra — это не `ModuleNotFoundError` из недр библиотеки, а
`MissingExtraError` с точной командой установки. Три реальных сообщения:

```
MissingExtraError: operation-aware моки требует опциональной зависимости.
Установите: pip install 'openapi-contract-fixtures[jj]'

MissingExtraError: generated d42-схемы требует опциональной зависимости.
Установите: pip install 'openapi-contract-fixtures[d42]'

ошибка: генерация d42-схем для операций ws2.addTicket требует опциональной зависимости.
Установите: pip install 'openapi-contract-fixtures[d42]'
```

`MissingExtraError` наследуется и от `ContractError`, и от `ImportError`, поэтому ловится
любым из двух.

Проект, которому нужен только drift-check в CI, ставит библиотеку **без extras**: ядро и
generated-реестр импортируются на голом окружении.

---

## 12. Матрица поддержки OpenAPI 3.0 / Swagger 2.0

Каждая конструкция имеет ровно один из трёх статусов: **поддержана**, **отклоняется с
диагностикой** или **не читается вовсе**. Полная таблица — включая колонку про то, что
выражается в d42-проекции, а что остаётся только на JSON-Schema-пути, —
[docs/support-matrix.md](docs/support-matrix.md).

Коротко: поддержаны `$ref` (локальные и межфайловые внутри корня источника), `allOf`,
`oneOf`, `anyOf`, `discriminator`, `nullable` / `x-nullable`, `readOnly` / `writeOnly`,
`enum`, `pattern`, границы длины и чисел, `uniqueItems`, булев и типизированный
`additionalProperties`, параметры в path/query/header/cookie с проверенной матрицей
`style`/`explode`, `collectionFormat` в Swagger 2.0.

Отклоняются с диагностикой: `not`, `if`/`then`/`else`, `const`, `patternProperties`,
`propertyNames`, `contains`, `prefixItems`, `dependentSchemas`, `dependentRequired`,
`unevaluatedProperties`, `unevaluatedItems`, `$defs`, булевы схемы, `type` списком,
`style: deepObject` / `label` / `matrix`, `content` вместо `schema` у параметра,
`allowReserved`, `in: formData`, диапазоны статусов вида `2XX`, внешние HTTP-`$ref`,
`$ref` за пределы корня источника и неизвестный `format` (при `policies.unknown_formats:
reject`).

**`oneOf` в d42-проекции расширяется** до `schema.any` (то есть до `anyOf`), потому что у
d42 нет эксклюзивного объединения. Точная семантика `oneOf` и `discriminator` целиком
держится на JSON Schema, и именно поэтому JSON-Schema-валидация выполняется всегда и не
отключается.

---

## 13. OpenAPI 3.1 в v0.1 сознательно не поддерживается

OpenAPI 3.1 **не является надмножеством** 3.0: в нём удалён `nullable`,
`exclusiveMinimum`/`exclusiveMaximum` стали числовыми, `type` может быть массивом,
появились булевы схемы и `$defs`, а Schema Object — это полноценная JSON Schema 2020-12.
Разбирать 3.1 правилами 3.0 значит молча потерять `null` в типах и неверно прочитать
границы.

Поэтому документ с `openapi: 3.1.x` не обрабатывается «как получится», а отклоняется явной
диагностикой:

```
ошибка: OpenAPI 3.1.0 не поддерживается в версии 0.1.
OpenAPI 3.1 не является надмножеством 3.0: в нём удалён 'nullable',
'exclusiveMinimum'/'exclusiveMaximum' стали числовыми, 'type' может быть массивом,
появились булевы схемы и '$defs'. Разбирать 3.1 правилами 3.0 значит молча потерять
контракт, поэтому библиотека отказывается это делать.
Адаптер 3.1 добавляется отдельно (dialects/openapi31.py) без изменений в ядре. [source=main]
```

Код возврата — `1`. Адаптер 3.1 — это новый модуль в `dialects/` и одна запись в реестре,
без изменений в ядре, генераторе, runtime и CLI.

---

## 14. Тесты не ходят в сеть

Ни библиотека, ни её тесты не делают исходящих сетевых запросов.

- Внешние `$ref` (`http://`, `https://`, любой `scheme` или `netloc`) запрещены всегда и
  отклоняются `RefResolutionError`.
- Межфайловые `$ref` разрешаются только внутри явно заданного `sources.<name>.root`;
  выход за корень через `..` или symlink отсекается после `resolve()`.
- Валидатор JSON Schema получает пустой `referencing.Registry`, чей `retrieve` всегда
  бросает исключение: попытка внешнего разрешения `$ref` превращается в ошибку контракта,
  а не в HTTP-запрос.
- В тестах автоиспользуемая фикстура `no_outbound_network` патчит `socket.socket.connect`
  и `socket.create_connection` и разрешает только `AF_UNIX` и loopback (`127.0.0.0/8`,
  `::1`) — их использует локальный HTTP-сервер моков. Попытка выйти наружу — это не
  «медленный тест», а дефект: где-то не сработал мок.

Синтетические спецификации для тестов лежат в `tests/fixtures/specs/` в вымышленном домене;
реальных сервисов, маршрутов и идентификаторов там нет.

---

## 15. Миграция существующих `mocked_*`-обёрток

Существующие обёртки остаются тонкими функциями поверх `OperationHandle.mock()` и
мигрируют **без изменения call sites**:

```python
def mocked_post_ticket(body, **kwargs):
    """Тонкая обёртка: сохраняет старую сигнатуру, внутри — generated handle."""
    return operations.ws2.add_ticket.mock(response=body, **kwargs)
```

```python
async with mocked_post_ticket(body) as mock:  # ни один вызов не переписан
    ...
```

Рекомендуемая целевая форма — прямой generated handle:

```python
async with operations.ws2.add_ticket.mock(response=body, wait_for_requests=1) as mock:
    ...
```

Пошаговый план (подключение спецификации, обёртки, перевод рукописных схем в
`overlay_generators` над сгенерированными, подключение `check` в CI) —
[docs/migration.md](docs/migration.md).

---

## Ограничения v0.1

- **Нет decorator API.** Контракт подключается явным `async with ...mock(...)`. Декоратор
  над сценарием отложен сознательно: он вынужден догадываться, куда отдать `ContractMock`,
  и создаёт второй путь валидации, который придётся держать в синхроне с основным.
- **Нет OpenAPI 3.1.** Документ отклоняется явной диагностикой (раздел 13).
- **Нет persistent-моков.** `start()` без обязательного «закрыть и провалидировать»
  позволил бы молча пропустить валидацию запросов. Мок регистрируется как `disposable`
  явно, а не по переменной окружения — чтобы поведение не зависело от настроек машины.
- **Рекурсивный контракт получает JSON Schema, но не получает d42.** d42 строит схему «по
  значению», и рекурсия развернулась бы бесконечно. `update` печатает об этом строкой
  `контракт рекурсивен, d42-схемы не генерируются (JSON Schema и валидация работают)`,
  а `OperationHandle.d42_schema()` для такой операции поднимает `OperationLookupError`.
- **`update` и `check` требуют extra `[d42]`, если для операций включены d42-артефакты.**
  Выключить их можно точечно ключом `d42: false` у операции в manifest либо флагом
  `--no-d42` у `openapi-contracts add`.
- **Параметры — только скаляры и массивы скаляров.** Объекты в параметрах и вложенные
  массивы не сериализуются однозначно и отклоняются.
- **Тело — только JSON-совместимые media type** (`application/json` и `*/+json`; в Swagger
  2.0 дополнительно `*/*`). Остальные варианты помечаются как непредставимые, не попадают
  в контракт, но саму операцию не роняют.

---

## Разработка

```bash
make install     # venv + editable-установка с dev-зависимостями
make lint        # ruff check + ruff format --check
make typecheck   # mypy strict
make test        # pytest (сеть не требуется и запрещена)
make check       # всё сразу
```

## Документация

- [docs/cli.md](docs/cli.md) — команды, флаги, коды возврата
- [docs/waivers.md](docs/waivers.md) — формат waiver'ов, жизненный цикл, разобранный пример
- [docs/support-matrix.md](docs/support-matrix.md) — матрица поддержки OpenAPI 3.0 / Swagger 2.0
- [docs/migration.md](docs/migration.md) — миграция существующего E2E-проекта
- [docs/adr/0001-architecture.md](docs/adr/0001-architecture.md) — архитектурные решения
- [CHANGELOG.md](CHANGELOG.md) — история изменений
