"""Keep the test suite offline, including accidental exchange/API calls."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError(
            "Network access is forbidden in tests; use a fake provider/exchange."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
