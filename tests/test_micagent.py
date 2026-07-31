from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.micagent import _KeyForwarder  # noqa: E402


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


class KeyForwarderTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebind_after_reconnect_sends_one_toggle_to_new_socket(self) -> None:
        muted = {"v": False}
        forwarder = _KeyForwarder(
            asyncio.get_running_loop(),
            muted,
        )
        first = FakeWebSocket()
        second = FakeWebSocket()

        forwarder.bind(first)
        self.assertTrue(await forwarder.send_toggle())
        forwarder.unbind(first)
        forwarder.bind(second)
        self.assertTrue(await forwarder.send_toggle())

        self.assertEqual(
            [{"type": "mute", "muted": True}],
            first.messages,
        )
        self.assertEqual(
            [{"type": "mute", "muted": True}],
            second.messages,
        )

    async def test_disconnected_toggle_is_explicitly_ignored(self) -> None:
        output: list[str] = []
        forwarder = _KeyForwarder(
            asyncio.get_running_loop(),
            {"v": False},
            output=output.append,
        )

        self.assertFalse(await forwarder.send_toggle())

        self.assertIn("当前未连接", output[0])

    async def test_start_is_process_idempotent_across_rebinds(self) -> None:
        fake_thread = MagicMock()
        with patch(
            "audio_gateway.micagent.threading.Thread",
            return_value=fake_thread,
        ) as thread_factory:
            forwarder = _KeyForwarder(
                asyncio.get_running_loop(),
                {"v": False},
            )
            forwarder.start()
            forwarder.bind(FakeWebSocket())
            forwarder.start()

        thread_factory.assert_called_once()
        fake_thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
