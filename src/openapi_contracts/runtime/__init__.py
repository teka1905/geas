"""Runtime-часть: реестр операций, публичные handles и валидация.

Импортируется без d42 и без JJ — этого достаточно, чтобы работать с
generated-реестром и проверять данные по JSON Schema.
"""

from __future__ import annotations

from .operation import (
    OperationHandle,
    ParameterView,
    RequestBodyView,
    RequestView,
    ResponseView,
)
from .registry import CONTRACT_DOCUMENT_VERSION, OperationRegistry, handle_from_document
from .validation import decode_parameter, validate_instance

__all__ = [
    "CONTRACT_DOCUMENT_VERSION",
    "OperationHandle",
    "OperationRegistry",
    "ParameterView",
    "RequestBodyView",
    "RequestView",
    "ResponseView",
    "decode_parameter",
    "handle_from_document",
    "validate_instance",
]
