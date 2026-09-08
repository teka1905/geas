"""Делает пример запускаемым «как есть».

Единственная задача файла — положить каталог примера в ``sys.path``, чтобы
``import app_contracts`` работал без установки примера как пакета:

.. code-block:: sh

    python -m jj -H 127.0.0.1 -p 8080 &
    JJ_REMOTE_MOCK_URL=http://127.0.0.1:8080 pytest examples/consumer/tests

В настоящем проекте-потребителе этого файла не будет: там ``app_contracts`` —
обычный пакет самого проекта, и он уже импортируется.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EXAMPLE_ROOT = str(Path(__file__).parent)
if _EXAMPLE_ROOT not in sys.path:
    sys.path.insert(0, _EXAMPLE_ROOT)
