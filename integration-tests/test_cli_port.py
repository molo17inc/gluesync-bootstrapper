#!/usr/bin/env python3
"""automator_app.cli: an explicit --port already taken by another process is a startup error (exit 1), not a
silent exit 0 after uvicorn's bind fails in its thread."""

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automator_app import cli  # noqa: E402


class ExplicitPortInUseTests(unittest.TestCase):
    def test_taken_port_exits_1_without_starting_the_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            argv = ["--host", "127.0.0.1", "--port", str(port)]
            with mock.patch.object(cli, "_start_uvicorn_thread") as start, \
                    mock.patch.object(cli, "_show_error_dialog"):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(argv)
        self.assertEqual(ctx.exception.code, 1)
        start.assert_not_called()

    def test_free_explicit_port_is_used_as_is(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        argv = ["--host", "127.0.0.1", "--port", str(port)]
        thread = mock.Mock()
        with mock.patch.object(cli, "_start_uvicorn_thread", return_value=thread) as start, \
                mock.patch.object(cli, "GluesyncTrayApp") as tray:
            cli.main(argv)
        self.assertEqual(start.call_args.args[0].port, port)
        tray.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
