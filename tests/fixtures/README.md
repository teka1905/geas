# Фикстуры тестов

Синтетические спецификации, на которых гоняются тесты библиотеки. Всё придумано с
нуля в вымышленном домене **workspace / document / label / member**: никаких
реальных сервисов, маршрутов, схем и идентификаторов здесь нет и быть не должно.

Правила, которых держатся фикстуры:

* каждый файл разбирается `yaml.safe_load` (или `json.load` для `.json`);
* один файл — одна ясная идея; файлы-фичи держатся в пределах нескольких десятков строк;
* у всех спецификаций есть `info.title` и `info.version`, **кроме**
  `specs/features/minimal_info.yaml` — он специально проверяет, что отсутствие
  `info` библиотека переживает;
* файлы детерминированы: никаких дат, счётчиков и абсолютных путей внутри.

---

## `specs/basic/` — одна и та же API в двух диалектах

| Файл | Что это |
| --- | --- |
| `swagger2.json` | Swagger 2.0; префикс маршрута вынесен в `basePath: /api/v1` |
| `openapi30.yaml` | OpenAPI 3.0.3; тот же префикс вписан прямо в шаблоны путей |

Пара **семантически эквивалентна**: те же три операции (`listDocuments`,
`createDocument`, `deleteDocument`), те же маршруты, параметры, тела и ответы.
Канонические документы контрактов, собранные из этих двух файлов, совпадают
целиком, за единственным полем `dialect` (`swagger2` против `openapi30`) — его
сравнение должно игнорировать.

Что покрыто парой:

* `definitions` / `components.schemas` и `$ref` на именованные схемы;
* параметры в path, query и header; параметр уровня Path Item (слияние с
  параметрами операции);
* тело запроса (`in: body` против `requestBody`);
* статусы 200, 400 и 204 без тела; заголовок ответа `X-Request-Id`;
* `required`, вложенные объекты, массивы объектов, массивы скаляров;
* `enum`, `pattern`, `minLength`/`maxLength`, `minimum`/`maximum` для целых и
  для чисел, `format` (`date-time`, `uuid`, `int64`, `int32`, `double`, `email`,
  `uri` — последний в фикстурах фич), `boolean`;
* `additionalProperties: false` (`Member`, `ErrorResponse`) и типизированный
  `additionalProperties` (`DocumentBase.labels`);
* nullable: `x-nullable` в Swagger 2.0 против `nullable` в 3.0;
* `readOnly` (`DocumentBase.createdAt`) — вырезается из направления request в
  обоих диалектах;
* `writeOnly` (`DocumentCreate.draftSecret`) — только в файле 3.0, потому что в
  Swagger 2.0 такого ключа нет. Схема `DocumentCreate` используется **только** как
  тело запроса, поэтому writeOnly не меняет ни один контракт и эквивалентность
  пары сохраняется в обоих направлениях;
* `allOf`, который сливается доказуемо: `[$ref, inline-объект]` с
  непересекающимися свойствами (`Document`, `DocumentCreate`);
* массив в query двух видов: `collectionFormat: multi` == `style: form,
  explode: true` (`status`) и `collectionFormat: csv` == `style: form,
  explode: false` (`tags`).

---

## `specs/features/` — по одной фиче на файл (OpenAPI 3.0.3)

| Файл | Идея | Ожидаемое поведение |
| --- | --- | --- |
| `oneof_discriminator.yaml` | `oneOf` + `discriminator` с явным `mapping`, варианты — `$ref` | разбирается |
| `oneof_no_mapping.yaml` | `oneOf` + `discriminator` без `mapping` | разбирается |
| `anyof.yaml` | `anyOf`: вид композиции обязан сохраниться точно | разбирается |
| `allof_unmergeable.yaml` | `allOf`, части которого конфликтуют по свойству `size` | разбирается, но композиция не сливается |
| `allof_with_siblings.yaml` | `allOf` рядом с собственными `type`/`properties`/`required` | разбирается, локальная часть не теряется |
| `oneof_with_siblings.yaml` | `oneOf` рядом с собственными `type`/`properties`/`required` | разбирается |
| `ref_siblings.yaml` | соседи `$ref`: `readOnly` и `nullable` — законные, `minLength` — нет | `getLegalCard` разбирается, `getIllegalCard` отклоняется |
| `cookie_params.yaml` | параметр в cookie | разбирается |
| `deep_object.yaml` | `style: deepObject` в query | обе операции отклоняются |
| `unknown_format.yaml` | `format: my-custom-tag` на строке | отклоняется при `unknown_formats: reject`, проходит при `annotate` |
| `recursive.yaml` | самоссылка (`Node`) и взаимный цикл из трёх схем (`Alpha → Beta → Gamma → Alpha`) | разбирается, контракт помечается рекурсивным |
| `multi_content_types.yaml` | два content type у запроса, `application/json` + `application/pdf` у ответа | разбирается, PDF-вариант помечается как непредставимый |
| `charset_media_type.yaml` | `application/json;charset=UTF-8` и вариант с пробелом и другим регистром | оба нормализуются в `application/json` |
| `default_response.yaml` | единственный ответ операции — `default` | разбирается |
| `keyword_named_properties.yaml` | свойства названы `type`, `items`, `required`, `properties`, `description`, `enum`, `allOf` | разбирается — регрессия на position-awareness |
| `unsupported_keywords.yaml` | шесть операций: `not`, `const`, `patternProperties`, `if`/`then`, булева схема `true`, `type: [string, "null"]` | каждая отклоняется своей диагностикой |
| `no_type.yaml` | свойство без `type` и без единого ограничения | отклоняется |
| `minimal_info.yaml` | спецификация вообще без секции `info` | разбирается |
| `openapi31.yaml` | `openapi: 3.1.0` | отклоняется как неподдержанная версия |
| `swagger1.json` | `swagger: "1.2"` (структура `apis`/`operations`/`nickname`) | отклоняется как неподдержанная версия |

Файлы с несколькими операциями (`unsupported_keywords.yaml`, `deep_object.yaml`,
`ref_siblings.yaml`) проверяются **по одной операции за раз**: при сборке всех
сразу видна только первая ошибка.

---

## `specs/multifile/` — межфайловые `$ref`

| Файл | Что проверяет |
| --- | --- |
| `root.yaml` | ссылки на `./schemas/common.yaml#/components/schemas/Label` и на вложенный `./schemas/deep/more.yaml#/components/schemas/Detail` |
| `schemas/common.yaml` | фрагмент схем; сам ссылается на `./deep/more.yaml` — путь разрешается относительно **этого** файла, а не корневого |
| `schemas/deep/more.yaml` | самый глубокий фрагмент |
| `escape/root.yaml` | `$ref: ../../../outside.yaml#/x` — уход за корень источника, обязан быть отклонён |
| `remote/root.yaml` | `$ref: https://example.invalid/x.yaml#/y` — сеть, обязана быть отклонена |

`schemas/common.yaml` и `schemas/deep/more.yaml` — фрагменты, а не спецификации:
в них нет ни `openapi`, ни `paths`, только `components.schemas`.

Корень источника для `escape/root.yaml` — каталог самого файла
(`specs/multifile/escape/`, значение по умолчанию). Цель ссылки —
[`outside.yaml`](outside.yaml) в корне каталога фикстур: файл существует
специально, иначе ошибка была бы «файл не найден», а проверить надо именно
отказ по границе корня.

---

## `specs/naming/` — имена и коллизии

| Файл | Что проверяет |
| --- | --- |
| `collisions.yaml` | `getItem` и `get_item` дают один `snake_case`; `import` — ключевое слово Python; `2fa` начинается с цифры; `by_key` занят публичным API namespace |
| `duplicate_operation_id.yaml` | один `operationId` на двух операциях |
| `missing_operation_id.yaml` | операция без `operationId` |

Коллизия `getItem` / `get_item` видна только когда выбраны **обе** операции;
остальные три случая ловятся на каждой операции по отдельности.

---

## `outside.yaml`

Не спецификация, а цель для `specs/multifile/escape/root.yaml`. Лежит вне корня
того источника — см. раздел про `multifile/` выше.
