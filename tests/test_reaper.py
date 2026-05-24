"""Smoke tests for the REAPER OSC client.

We don't have REAPER running in CI, so we spin up our own
ThreadingOSCUDPServer to receive the messages the client sends and
verify the addresses + payloads. That's enough to catch the obvious
"wrong OSC path" regressions without needing a DAW.
"""

import threading
import time

import pytest
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from src.reaper import ReaperClient


def _start_collector(port: int):
    """Boot a tiny OSC server that records every message into a list."""
    received: list[tuple[str, tuple]] = []
    disp = Dispatcher()
    disp.set_default_handler(lambda addr, *args: received.append((addr, args)))
    server = ThreadingOSCUDPServer(("127.0.0.1", port), disp)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return received, server


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client_and_received():
    port = _free_port()
    received, server = _start_collector(port)
    client = ReaperClient(send_port=port)
    try:
        yield client, received
    finally:
        server.shutdown()
        server.server_close()


def _await(received, addr, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for a, args in received:
            if a == addr:
                return args
        time.sleep(0.01)
    raise AssertionError(f"no message for {addr} in {timeout}s; got {received}")


def test_transport_play(client_and_received):
    client, received = client_and_received
    client.transport_play()
    args = _await(received, "/play")
    assert args == (1,)


def test_transport_stop(client_and_received):
    client, received = client_and_received
    client.transport_stop()
    args = _await(received, "/stop")
    assert args == (1,)


def test_track_mute_on(client_and_received):
    client, received = client_and_received
    client.track_mute(track=3, on=True)
    args = _await(received, "/track/3/mute")
    assert args == (1,)


def test_track_mute_toggle(client_and_received):
    client, received = client_and_received
    client.track_mute(track=3)  # toggle
    args = _await(received, "/track/3/mute")
    assert args == ("",)


def test_fx_bypass_on(client_and_received):
    """on=True means FX is ACTIVE → REAPER value 0 (bypassed=False)."""
    client, received = client_and_received
    client.fx_bypass(track=2, fx=1, on=True)
    args = _await(received, "/track/2/fxbypass/1")
    assert args == (0,)


def test_fx_param_value(client_and_received):
    client, received = client_and_received
    client.fx_param(track=4, fx=0, param=2, value=0.75)
    args = _await(received, "/track/4/fx/0/fxparam/2/value")
    assert args == pytest.approx((0.75,))


def test_goto_marker(client_and_received):
    client, received = client_and_received
    client.goto_marker(5)
    args = _await(received, "/marker/5")
    assert args == (1,)


def test_set_tempo(client_and_received):
    client, received = client_and_received
    client.set_tempo(140.0)
    args = _await(received, "/tempo/raw")
    assert args == pytest.approx((140.0,))


def test_generic_action(client_and_received):
    client, received = client_and_received
    client.action(40157)  # insert marker at cursor
    args = _await(received, "/action/40157")
    assert args == (1,)
