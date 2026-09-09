"""geas — тестовые контракты и operation-aware моки из OpenAPI.

Библиотека превращает checked-in OpenAPI в проверяемые артефакты:

``checked-in OpenAPI → нормализованный контракт → generated JSON Schema и d42
→ generator overlays → generated operation handles → operation-aware JJ-моки
→ проверка contract drift в CI``

Ядро (этот пакет) не зависит ни от d42, ни от JJ. Опциональные интеграции живут
в :mod:`geas.integrations.d42` и :mod:`geas.integrations.jj`
и подключаются через extras ``[d42]`` и ``[jj]``.
"""

from __future__ import annotations

from .errors import (
    ArtifactDriftError,
    ArtifactError,
    ContractError,
    ContractMockError,
    ContractOverlayError,
    ManifestBindingError,
    ManifestError,
    MissingExtraError,
    NamespaceCollisionError,
    OperationLookupError,
    RecursiveSchemaError,
    RefResolutionError,
    RequestContractError,
    ResponseContractError,
    ResponseVariantError,
    SpecLoadError,
    UnsupportedConstructError,
    UnsupportedSpecVersionError,
    ValidationFailedError,
    WaiverError,
)
from .models import Direction, ParameterLocation
from .runtime import (
    OperationHandle,
    OperationRegistry,
    ParameterView,
    RequestBodyView,
    RequestView,
    ResponseView,
)

#: Версия дистрибутива. Попадает в ``_generated.json`` как версия генератора.
__version__ = "0.2.0"

__all__ = [
    "ArtifactDriftError",
    "ArtifactError",
    "ContractError",
    "ContractMockError",
    "ContractOverlayError",
    "Direction",
    "ManifestBindingError",
    "ManifestError",
    "MissingExtraError",
    "NamespaceCollisionError",
    "OperationHandle",
    "OperationLookupError",
    "OperationRegistry",
    "ParameterLocation",
    "ParameterView",
    "RecursiveSchemaError",
    "RefResolutionError",
    "RequestBodyView",
    "RequestContractError",
    "RequestView",
    "ResponseContractError",
    "ResponseVariantError",
    "ResponseView",
    "SpecLoadError",
    "UnsupportedConstructError",
    "UnsupportedSpecVersionError",
    "ValidationFailedError",
    "WaiverError",
    "__version__",
]
