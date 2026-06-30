from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from ous_monitor.scrapers import netshoes


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)


class FakeClient:
    """Devolve uma sequência pré-definida de respostas, uma por GET."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        return self._responses.pop(0)


class NetshoesRetryTest(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        client = FakeClient([
            FakeResponse(429, {"Retry-After": "2"}),
            FakeResponse(429),
            FakeResponse(200),
        ])
        with patch("ous_monitor.scrapers.netshoes.time.sleep") as sleep:
            resp = netshoes._get_with_retry(client, {"page": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.calls, 3)
        # 1ª espera respeita Retry-After=2; 2ª usa backoff exponencial.
        self.assertEqual(sleep.call_args_list[0].args[0], 2.0)
        self.assertGreater(sleep.call_args_list[1].args[0], 0)

    def test_raises_after_exhausting_retries(self):
        client = FakeClient([FakeResponse(429) for _ in range(netshoes.MAX_RETRIES + 1)])
        with patch("ous_monitor.scrapers.netshoes.time.sleep"):
            with self.assertRaises(httpx.HTTPStatusError):
                netshoes._get_with_retry(client, {"page": 1})
        self.assertEqual(client.calls, netshoes.MAX_RETRIES + 1)

    def test_retry_after_parsing(self):
        self.assertEqual(
            netshoes._retry_after_seconds(FakeResponse(429, {"Retry-After": "5"})), 5.0)
        self.assertIsNone(
            netshoes._retry_after_seconds(FakeResponse(429, {"Retry-After": "Wed, 21 Oct"})))
        self.assertIsNone(netshoes._retry_after_seconds(FakeResponse(429)))


if __name__ == "__main__":
    unittest.main()
