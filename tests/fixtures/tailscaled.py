"""Local daemon substitute for startup tests; never contacts a tailnet."""
import os
import socket
import sys

flags = dict(arg[2:].split("=", 1) for arg in sys.argv[1:])
unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
unix.bind(flags["socket"])
if os.environ.get("HOMEY_TEST_DAEMON_FAIL"):
    print(os.environ["HOMEY_TEST_DAEMON_FAIL"], file=sys.stderr, flush=True)
    sys.exit(1)

listeners = []
for flag in ("socks5-server", "outbound-http-proxy-listen"):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    host, port = flags[flag].rsplit(":", 1)
    try:
        listener.bind((host, int(port)))
    except OSError:
        print(f"{flag}: bind: address already in use", file=sys.stderr, flush=True)
        sys.exit(1)
    listener.listen()
    listeners.append(listener)

unix.listen()
while True:
    connection, _ = unix.accept()
    with connection:
        request = connection.recv(4096)
        if b"GET /localapi/v0/status " in request:
            connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}')
