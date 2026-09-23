# CLI `geas`

Справочник по командам. Если вы подключаете geas впервые, начните с
[типовых сценариев](#типовые-сценарии).

```
geas [-m MANIFEST] <команда> [аргументы]
geas --version
```

| Команда | Что делает | Что меняет |
| --- | --- | --- |
| [`init`](#init) | создаёт `manifest.yaml` и `waivers.yaml` | оба файла |
| [`list`](#list) | показывает операции спецификации | ничего |
| [`add`](#add) | добавляет операцию в manifest | manifest |
| [`update`](#update) | перегенерирует артефакты | generated-каталог |
| [`check`](#check) | проверяет, что артефакты совпадают со спецификацией | ничего |
| [`diff`](#diff) | показывает, что изменилось в контрактах | ничего |
| [`show`](#show) | показывает форму сгенерированного контракта | ничего |

## Общие правила

- `-m/--manifest` пишется **до** имени команды: `geas -m contracts/manifest.yaml check`.
  Без него используется `manifest.yaml` из текущего каталога.
- Пути внутри manifest отсчитываются от каталога самого manifest, поэтому
  команду можно запускать из любого каталога.
- Сеть не используется: спецификация и все `$ref` читаются только с диска.
- Ошибки и предупреждения печатаются в stderr, результат — в stdout.

### Коды возврата

| Код | Когда | Что делать |
| --- | --- | --- |
| `0` | успех | — |
| `1` | ошибка контракта или конфигурации: артефакты разошлись со спецификацией, сломалась привязка операции, waiver просрочен или больше не нужен, конструкция не поддержана, manifest не найден или не разбирается | прочитать сообщение в stderr и исправить спецификацию, manifest или waivers |
| `2` | ошибка вызова: неверный аргумент, неоднозначный выбор операции или варианта, файл уже существует | исправить аргументы команды |

## `init`

```
geas [-m MANIFEST] init --source SPEC --package PACKAGE [--directory DIR] [--force]
```

Создаёт минимальный manifest по пути из `-m` и пустой `waivers.yaml` рядом с
ним, затем печатает следующие шаги.

| Флаг | Обязателен | Значение |
| --- | --- | --- |
| `--source` | да | путь к файлу OpenAPI относительно manifest |
| `--package` | да | Python-пакет, под которым тесты будут импортировать generated-каталог |
| `--directory` | нет | generated-каталог относительно manifest; по умолчанию `generated` |
| `--force` | нет | перезаписать существующие файлы |

```bash
geas -m contracts/manifest.yaml init \
  --source ../api/openapi.yaml \
  --package myproject.contracts.generated \
  --directory ../myproject/contracts/generated
```

Источник в новом manifest называется `main` и работает в режиме
`selection: explicit`: в артефакты попадут только операции, добавленные через
`add`.

Если manifest или `waivers.yaml` уже существуют, команда ничего не пишет и
завершается с кодом `2`. С `--force` перезаписываются **оба** файла, в том числе
`waivers.yaml` со всеми waiver'ами.

## `list`

```
geas [-m MANIFEST] list [--source NAME] [--json]
```

Алиас — `inspect`. Показывает, что есть в спецификации. Запускайте его перед
`add`, чтобы узнать `operationId`, маршрут и варианты ответа.

```
источник main: api/openapi.yaml [openapi30]
  режим выбора: explicit, операций: 2
  * GET     /api/v1/workspaces/{workspaceId}/documents
      operationId=listDocuments  ключ=api.listDocuments
      responses: 200:application/json
    POST    /api/v1/workspaces/{workspaceId}/documents
      operationId=createDocument  ключ=-
      request: application/json
      responses: 201:application/json

* — операция выбрана manifest и попадает в generated-артефакты
```

- `*` — операция выбрана manifest и попадает в артефакты; `ключ=-` — не выбрана.
- `не поддержано: …` — вариант, который нельзя закрепить (например, тело не в
  JSON), и причина.

| Флаг | Значение |
| --- | --- |
| `--source NAME` | показать только один источник |
| `--json` | тот же отчёт в JSON |

## `add`

```
geas [-m MANIFEST] add KEY --source SOURCE
    [--operation-id ID] [--method METHOD] [--path PATH]
    [--request-content-type CT]
    [--response STATUS[:CONTENT_TYPE]]...
    [--python-path a.b]
    [--no-d42]
```

Добавляет операцию в секцию `operations` manifest под ключом `KEY`. Ключ —
стабильное имя операции в вашем проекте, например `ws2.addTicket`. Из него
строится имя в generated namespace: `operations.ws2.add_ticket`. Если на бэкенде
переименуют `operationId`, имя в тестах не изменится.

```bash
geas add ws2.addTicket --source main --operation-id addTicket
```

Операция ищется по `--operation-id` или по паре `--method` + `--path`. Путь
указывается полностью, в том виде, в каком его печатает `list`.

Команда сама дописывает в manifest полную привязку: `operation_id`, `method`,
`path`, request content type, вариант ответа и `python_path`. Если что-то из
этого неоднозначно, она завершается с кодом `2` и называет нужный флаг:

| Ситуация | Флаг |
| --- | --- |
| у операции нет `operationId` или он встречается у нескольких операций | `--method POST --path /api/v1/tickets` |
| у операции несколько JSON-тел запроса | `--request-content-type application/json` |
| несколько успешных (2xx) вариантов ответа | `--response 200:application/json`; флаг можно повторить, чтобы закрепить несколько вариантов |
| имя из ключа не подходит или конфликтует с другой операцией | `--python-path ws2.add_ticket` |
| d42-схемы для операции не нужны | `--no-d42` (в manifest запишется `d42: false`) |

Без `--response` закрепляется единственный успешный вариант. Если успешных
вариантов нет, закрепляется единственный имеющийся.

Что нужно знать:

- **Manifest не меняется, если операцию нельзя сгенерировать.** Перед записью
  команда собирает весь проект вместе с новой операцией во временном каталоге.
  Если конструкция не поддержана, вы увидите ошибку (часто с готовой заготовкой
  waiver'а), а manifest останется прежним. Команда упадёт и тогда, когда
  сломано что-то другое в проекте, например просрочен waiver.
- **Повторный вызов безопасен.** Тот же ключ с теми же параметрами — код `0`,
  изменений нет. Тот же ключ с другими параметрами — код `2` и обе версии записи.
  Такое изменение вносите в manifest вручную.
- **Manifest перезаписывается в каноническом виде:** ключи сортируются,
  YAML-комментарии пропадают. Если комментарии нужно сохранить, добавляйте
  операцию в manifest вручную.
- **Артефакты не обновляются.** После `add` запустите `geas update`.

## `update`

```
geas [-m MANIFEST] update
```

Перегенерирует артефакты в `output.directory`. Запускайте после любого
изменения спецификации, manifest, waivers и после обновления geas. Результат
коммитьте.

- Сначала в памяти собирается весь набор. Если где-то ошибка, на диск не
  пишется ничего.
- Файлы заменяются атомарно.
- Удаляются только файлы, которые geas создал сам (их список лежит в
  `_generated.json`). Посторонние файлы в каталоге остаются на месте.
- Повторный запуск без изменений на входе даёт те же байты.

Команда печатает число записанных файлов, удалённые устаревшие артефакты,
операции без d42-схем с причиной, варианты, не представимые контрактом, покрытие
waiver'ов со `scope: subtree` и предупреждения о waiver'ах с истекающим сроком.

Если хотя бы у одной операции включён d42 (по умолчанию он включён), нужен extra
`[d42]`: `pip install 'geas[d42]'`. Без него команда завершится ошибкой с этой же
подсказкой. Чтобы отключить d42 у отдельной операции, укажите `d42: false` в
manifest или используйте `geas add --no-d42`.

## `check`

```
geas [-m MANIFEST] check
```

Проверяет, что артефакты на диске совпадают с тем, что сгенерировал бы
`update`. Ничего не меняет. Эту команду ставят в CI:

```yaml
- name: Контракты соответствуют спецификации
  run: geas -m contracts/manifest.yaml check
```

Код `1` возвращается, если:

- какой-то файл отличается, отсутствует или лишний;
- артефакты записаны в другой версии формата;
- генерация не проходит: waiver просрочен или больше не нужен, привязка
  операции сломалась, в спецификации появилась неподдержанная конструкция.

```
generated-артефакты разошлись со спецификацией:
  отличается:  _d42/api__create_document_response.py
  отличается:  _generated.json
  отличается:  contracts/api__create_document.json

Запустите 'geas update' и закоммитьте результат
```

Как и `update`, команда печатает покрытие subtree-waiver'ов и предупреждения об
истечении. Предупреждения на код возврата не влияют. Extra `[d42]` нужен на тех
же условиях, что и для `update`.

> **Исключите generated-каталог из автоформатирования.** Любой проход
> `ruff format`/`black` по артефактам делает `check` красным:
>
> ```toml
> [tool.ruff]
> extend-exclude = ["**/generated/**"]
> ```

## `diff`

```
geas [-m MANIFEST] diff [--json]
```

Сравнивает контракты в generated-каталоге с тем, что даёт текущая
спецификация. Запускайте перед `update`, чтобы увидеть, что именно изменится.

```
api.createDocument:
  [контракт] ~ /responses/0/schema/$defs/Document/properties/title/maxLength: 120 → 200
```

- `[контракт]` — меняется, какие данные валидны, или публичное имя операции;
- `[оформление]` — меняется только несемантическая часть артефакта;
- `новая операция: KEY` и `операция исчезла: KEY` — изменился набор операций.

Код возврата `1`, если есть хотя бы одно изменение `[контракт]`, иначе `0`.

`--json` печатает то же самое в виде объекта с полями `added`, `removed`,
`changed` (у каждого элемента — `key`, `semantic` и `cosmetic` с указателями на
изменившиеся места) и `is_semantic`. Код возврата тот же.

## `show`

```
geas [-m MANIFEST] show OPERATION
    [--direction {request,response}]
    [--status STATUS] [--content-type CONTENT_TYPE]
    [--json] [--max-depth N]
```

Показывает тело запроса или ответа уже сгенерированного контракта: какие поля
разрешены и обязательны, их типы, `enum`, `pattern` и границы. В конце выводится
путь к JSON Schema и имя d42-схемы. Параметры и заголовки команда не показывает.

`show` читает только артефакты: ничего не перегенерирует, не открывает OpenAPI и
не требует extra `[d42]`. Если артефактов нет, команда завершается с кодом `1` и
предлагает запустить `geas update`.

| Аргумент | Значение |
| --- | --- |
| `OPERATION` | ключ операции из manifest, например `ws2.addTicket` |
| `--direction` | `response` (по умолчанию) или `request` |
| `--status` | статус ответа: число или `default`; только для `--direction response` |
| `--content-type` | нужен, если у выбранного варианта несколько content type |
| `--json` | машиночитаемый вывод |
| `--max-depth N` | глубина раскрытия вложенных полей, по умолчанию 6 |

Если вариант один, он выбирается автоматически. Если вариантов несколько и
селектор не передан, команда завершается с кодом `2` и перечисляет допустимые
значения. Неизвестный ключ операции, статус или content type тоже дают код `2`.

```bash
geas -m contracts/manifest.yaml show ws2.addTicket --status 200
geas -m contracts/manifest.yaml show ws2.addTicket --direction request
```

```
ws2.addTicket
Response 200 application/json

$ref #/$defs/ResponseDto; object; дополнительные свойства разрешены
├── description — optional; string
├── entityId — optional; integer; format int64
├── payload — optional; $ref #/$defs/ObjectNode; object; дополнительные свойства разрешены
└── rc — required; string; enum["OK", "ERROR"]

JSON Schema:
  /abs/path/schemas/generated/contracts/ws2__add_ticket.json#/responses/0/schema

d42:
  schemas.generated._d42.ws2__add_ticket_response:GeneratedResponseDtoSchema

d42 source:
  /abs/path/schemas/generated/_d42/ws2__add_ticket_response.py
```

Как читать вывод:

| Фрагмент | Что означает |
| --- | --- |
| `required` / `optional` | есть ли поле в `required` родительского объекта; `default` и `const` обязательным поле не делают |
| `string \| null` | поле допускает `null` (`type: [string, null]`, `nullable: true` или `x-nullable: true`) |
| `oneOf(string, integer)` | комбинатор; ветки со структурой раскрываются ниже как `oneOf[i]` |
| `$ref #/$defs/Node (рекурсия)` | ссылка уже раскрывалась на этом пути; обход остановлен |
| `$ref https://… (внешняя ссылка не раскрывается)` | внешние `$ref` не разрешаются |
| `дополнительные свойства разрешены` | `additionalProperties` отсутствует или равен `true` |
| `ещё: multipleOf, not` | ключевые слова, которые команда не расшифровывает; точная семантика — в JSON Schema по напечатанному пути |
| `… (вложенность глубже N…)` | достигнут предел `--max-depth` |

`--json` печатает объект с полями `operation`, `direction`, `status`,
`content_type`, `required` (для тела запроса), `contract_file`, `json_pointer`,
`contract_path`, `d42_module`, `d42_export`, `d42_reference`, `d42_source_path`
и `schema`. Этот формат подходит для скриптов и редактора. Разбирать текстовый
вывод в тестах не стоит: он может меняться.

## Типовые сценарии

### Подключить спецификацию

```bash
geas -m contracts/manifest.yaml init --source ../api/openapi.yaml \
    --package app.contracts.generated --directory ../app/contracts/generated
geas -m contracts/manifest.yaml list
geas -m contracts/manifest.yaml add api.createDocument --source main --operation-id createDocument
geas -m contracts/manifest.yaml update
geas -m contracts/manifest.yaml check
```

Закоммитьте manifest, `waivers.yaml` и generated-каталог, затем добавьте `check`
в CI. Пошаговая инструкция для проекта с уже написанными моками — в
[migration.md](migration.md).

### Спецификация обновилась

```bash
geas diff      # что изменилось и затрагивает ли это контракт
geas update    # принять изменения
geas check     # убедиться, что всё сходится
```

Закоммитьте спецификацию вместе с обновлёнными артефактами.

### `check` упал в CI

Прочитайте первую строку stderr:

| Сообщение | Что делать |
| --- | --- |
| `generated-артефакты разошлись со спецификацией` | запустить `geas update` локально и закоммитить результат |
| `operationId изменился`, `маршрут изменился`, `response-вариант … исчез` | обновить запись операции в manifest под новую спецификацию или откатить спецификацию |
| `waiver истёк`, `waiver'ы больше не нужны`, `waiver … устарел` | см. [waivers.md](waivers.md#что-ломает-генерацию-и-как-это-исправить) |
| `… не поддерживается` с contract path | починить спецификацию или выписать waiver по заготовке из сообщения, см. [waivers.md](waivers.md#как-выписать-waiver) |
| `… требует опциональной зависимости` | поставить extra, названный в сообщении, в CI-окружение |

### Обновить geas

После смены версии geas меняется поле `generator` в `_generated.json`, и `check`
начинает падать. Поднимите версию в зависимостях, запустите `geas update` и
закоммитьте обе правки вместе.
