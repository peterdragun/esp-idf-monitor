# SPDX-FileCopyrightText: 2015-2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
"""IDE WebSocket client used to advertise debug events to an external WebSocket server.

Two backends are available:

* On Python >= 3.8 the implementation forwards to ``esp_pylib.ws`` so the
  monitor shares one connection (and one ``ESPRESSIF_IDE_WS`` channel) with
  any other Espressif tool that uses esp-pylib's logging / IDE integration.
* On Python 3.7 (or when esp-pylib is not installed) we fall back to the
  legacy implementation built on the ``websocket-client`` package. The fallback
  is preserved unchanged so 3.7 users keep working until support is dropped.

Both backends expose the same public API:

* ``WebSocketClient(url)`` — connect synchronously; raises ``RuntimeError`` on
  failure (the pylib backend specifically raises ``esp_pylib.errors.FatalError``,
  which is itself a ``RuntimeError`` subclass — so ``except RuntimeError``
  catches both regardless of which backend is active).
* ``.send(payload_dict)`` — fire a ``{'event': '<name>', ...}`` payload.
* ``.wait(expect_iterable)`` — block until a matching JSON message is received.
  ``expect_iterable`` is a list of ``(key, value)`` tuples; **all** must match.
* ``.close()`` — close the underlying connection.

The expected payload shapes used by IDF Monitor are:

* ``{'event': 'gdb_stub', 'port': '/dev/ttyUSB1', 'prog': 'build/elf_file'}``
* ``{'event': 'coredump', 'file': '/tmp/xy', 'prog': 'build/elf_file'}``

and the IDE replies with ``{'event': 'debug_finished'}``.
"""

import json
import sys
import time
from typing import Any
from typing import Iterable
from typing import Tuple
from typing import Union

from .output_helpers import error_print
from .output_helpers import note_print

# (py37-drop): Once Python >= 3.8 is the minimum supported version,
# drop the legacy backend and the websocket-client import — esp-pylib's
# `FatalError` becomes the only failure type and the import is unconditional.
try:
    from esp_pylib import ws as _pylib_ws
    from esp_pylib.errors import FatalError
except ImportError:  # esp-pylib not installed (e.g. Python 3.7 ide extra path)
    _pylib_ws = None  # type: ignore[assignment]

try:
    import websocket  # legacy `websocket-client` package
except ImportError:
    websocket = None  # type: ignore[assignment]


def _pylib_backend_available() -> bool:
    """True when esp-pylib is importable and exposes the expected ws API."""
    return _pylib_ws is not None and hasattr(_pylib_ws, 'set_ws_url')


class _PylibBackend:
    """Adapter that maps the legacy WebSocketClient API onto ``esp_pylib.ws``."""

    def __init__(self, url: str) -> None:
        # Tell esp-pylib about the URL up-front so log messages emitted from any
        # esp-pylib-using component (logger.warn / logger.err, excepthook, etc.)
        # share the same connection.
        _pylib_ws.set_ws_url(url)
        _pylib_ws.ensure_connected()

    def send(self, payload_dict: dict) -> None:
        _pylib_ws.send_event(**payload_dict)
        # Match the legacy log line — the integration test greps for it.
        note_print(f'WebSocket sent: {payload_dict}')

    def wait(self, expect_iterable: Iterable[Tuple[str, Any]]) -> None:
        # esp-pylib only matches on the `event` key, while the legacy API
        # accepts arbitrary (key, value) pairs. Reject multi-key expectations
        # explicitly so a future protocol change shows up as a loud failure
        # rather than silently being narrowed to event-only matching.
        expect = dict(expect_iterable)
        event = expect.pop('event', None)
        if event is None or expect:
            raise FatalError(f'Unsupported WebSocket wait expectation: {expect_iterable}')
        msg = _pylib_ws.wait_for_event(event)
        note_print(f'WebSocket received: {msg}')

    def close(self) -> None:
        _pylib_ws.close()


# (py37-drop): delete `_LegacyBackend` entirely; it exists only because
# `websockets >= 12` (used by esp-pylib) requires Python >= 3.8.
class _LegacyBackend:
    """Original websocket-client based implementation, kept for Python 3.7."""

    RETRIES: int = 3
    CONNECTION_RETRY_DELAY: int = 1

    def __init__(self, url: str) -> None:
        self.url: str = url
        self.ws = None
        self._connect()

    def _connect(self) -> None:
        """
        Connect to WebSocket server at url
        """

        self.close()
        for _ in range(self.RETRIES):
            try:
                self.ws = websocket.create_connection(self.url)
                break  # success
            except AttributeError:
                # `websocket` is None — package was never imported.
                raise RuntimeError('Please install the websocket_client package for IDE integration!')
            except Exception as e:  # noqa
                error_print(f'WebSocket connection error: {e}')
            time.sleep(self.CONNECTION_RETRY_DELAY)
        else:
            raise RuntimeError('Cannot connect to WebSocket server')

    def close(self) -> None:
        try:
            self.ws.close()
        except AttributeError:
            # Not yet connected
            pass
        except Exception as e:  # noqa
            error_print(f'WebSocket close error: {e}')

    def send(self, payload_dict: dict) -> None:
        """
        Serialize payload_dict in JSON format and send it to the server
        """
        for _ in range(self.RETRIES):
            try:
                self.ws.send(json.dumps(payload_dict))
                note_print(f'WebSocket sent: {payload_dict}')
                break
            except Exception as e:  # noqa
                error_print(f'WebSocket send error: {e}')
                self._connect()
        else:
            raise RuntimeError('Cannot send to WebSocket server')

    def wait(self, expect_iterable: Iterable[Tuple[str, Any]]) -> None:
        """
        Wait until a dictionary in JSON format is received from the server with all (key, value) tuples from
        expect_iterable.
        """
        for _ in range(self.RETRIES):
            try:
                r = self.ws.recv()
            except Exception as e:
                error_print(f'WebSocket receive error: {e}')
                self._connect()
                continue
            obj = json.loads(r)
            if all([k in obj and obj[k] == v for k, v in expect_iterable]):
                note_print(f'WebSocket received: {obj}')
                break
            error_print(f'WebSocket expected: {dict(expect_iterable)}, received: {obj}')
        else:
            raise RuntimeError('Cannot receive from WebSocket server')


class WebSocketClient:
    """Public WebSocket client. Internally dispatches to esp-pylib (preferred)
    or the legacy websocket-client backend (Python 3.7 fallback)."""

    # Re-exported so existing references like ``WebSocketClient.RETRIES`` keep working.
    RETRIES: int = _LegacyBackend.RETRIES
    CONNECTION_RETRY_DELAY: int = _LegacyBackend.CONNECTION_RETRY_DELAY

    def __init__(self, url: str) -> None:
        self.url: str = url
        self._impl: Union[_PylibBackend, _LegacyBackend]
        if sys.version_info >= (3, 8) and _pylib_backend_available():
            self._impl = _PylibBackend(url)
        else:
            self._impl = _LegacyBackend(url)

    def send(self, payload_dict: dict) -> None:
        self._impl.send(payload_dict)

    def wait(self, expect_iterable: Iterable[Tuple[str, Any]]) -> None:
        self._impl.wait(expect_iterable)

    def close(self) -> None:
        self._impl.close()
