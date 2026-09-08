"""JJ-интеграция (extra ``[jj]``).

Публичный вход один — :class:`ContractMock`, который создаётся методом
``OperationHandle.mock()``. Готовый чужой ``jj.Mocked`` библиотека не принимает:
контракт восстановить интроспекцией matcher'а и response'а невозможно, поэтому
мок всегда строится из :class:`~geas.OperationHandle`.
"""

from __future__ import annotations

from .backend import SUPPORTED_JJ, check_version
from .contract_mock import ContractMock

__all__ = ["SUPPORTED_JJ", "ContractMock", "check_version"]
