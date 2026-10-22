import io
import unittest
from unittest.mock import patch

from qqbridge.brain import BrainClient, BrainError


class StreamTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_read_once_and_response_is_closed(self):
        response = io.BytesIO(b'{"type":"session","coordination_id":"one"}\n{"type":"action","action":{}}\n{"type":"done","actions":[]}\n')
        with patch('qqbridge.brain.urlopen', return_value=response) as request:
            events = [item async for item in BrainClient().qq_reply_stream({'message_ids': ['one']})]
        self.assertEqual([item['type'] for item in events], ['session', 'action', 'done'])
        request.assert_called_once()
        self.assertTrue(response.closed)

    async def test_in_band_error_raises_without_retry(self):
        response = io.BytesIO('{"type":"error","message":"上游未完成"}\n'.encode())
        with patch('qqbridge.brain.urlopen', return_value=response) as request:
            with self.assertRaisesRegex(BrainError, '上游未完成'):
                async for _ in BrainClient().qq_reply_stream({}):
                    pass
        request.assert_called_once()
        self.assertTrue(response.closed)
