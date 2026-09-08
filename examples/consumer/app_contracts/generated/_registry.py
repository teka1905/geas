"""Индекс операций и сам реестр.

Документы контрактов читаются лениво, поэтому импорт этого модуля дёшев и не
требует ни d42, ни JJ.
"""

# Файл сгенерирован автоматически командой 'geas update'.
# Не редактируйте его руками: изменения будут затёрты, а 'geas check'
# уронит CI на расхождении.
from __future__ import annotations

from pathlib import Path

from geas.runtime import OperationRegistry

#: Стабильный ключ операции → имя файла контракта.
INDEX: dict[str, str] = {
    'api.createDocument': 'api__create_document',
    'api.listDocuments': 'api__list_documents',
}

#: Каталог с нормализованными контрактами.
CONTRACTS_DIR = Path(__file__).parent / "contracts"

REGISTRY = OperationRegistry(
    index=INDEX,
    contracts_dir=CONTRACTS_DIR,
    d42_package=f"{__package__}._d42",
)

__all__ = ["CONTRACTS_DIR", "INDEX", "REGISTRY"]
