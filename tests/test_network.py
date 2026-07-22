"""Outbound URL policy contracts for provider requests."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.network import OutboundURLPolicyError, validate_outbound_url


class NetworkPolicyTests(unittest.TestCase):
    def test_requires_https_and_exact_provider_allowlist(self) -> None:
        for url in (
            "http://api.openai.com/v1/chat/completions",
            "https://evil.example/v1/chat/completions",
            "https://api.openai.com@evil.example/v1/chat/completions",
            "https://api.openai.com/v1/chat/completions#secret",
        ):
            with self.subTest(url=url), self.assertRaises(OutboundURLPolicyError):
                validate_outbound_url(url)

    def test_rejects_direct_private_address(self) -> None:
        with self.assertRaises(OutboundURLPolicyError):
            validate_outbound_url("https://127.0.0.1/v1/chat/completions")

    def test_rejects_private_dns_resolution(self) -> None:
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with mock.patch("flayr_core.network.socket.getaddrinfo", return_value=answer):
            with self.assertRaisesRegex(OutboundURLPolicyError, "非公网"):
                validate_outbound_url("https://api.openai.com/v1/chat/completions")

    def test_accepts_allowlisted_public_provider(self) -> None:
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with mock.patch("flayr_core.network.socket.getaddrinfo", return_value=answer):
            validated = validate_outbound_url("https://api.openai.com/v1/chat/completions")
        self.assertEqual(validated.hostname, "api.openai.com")
        self.assertEqual(validated.resolved_addresses, ("8.8.8.8",))


if __name__ == "__main__":
    unittest.main()
