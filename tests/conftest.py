"""Keep ordinary regression tests offline; live checks are an explicit choice."""

import os
import socket

import pytest


def pytest_collection_modifyitems(items):
    if os.environ.get("UNIVERSAL_DOCS_LIVE_TESTS") == "1":
        return
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(
                pytest.mark.skip(
                    reason="set UNIVERSAL_DOCS_LIVE_TESTS=1 for public-network checks"
                )
            )


@pytest.fixture(autouse=True)
def block_unmarked_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return

    def denied(*args, **kwargs):
        raise AssertionError(
            "Regression tests must not open network sockets; use MockTransport or mark live"
        )

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
