import socket, signal, sys, time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1])))
s.listen(16)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
deadline = time.time() + 120
while time.time() < deadline:
    try:
        c, a = s.accept()
        print("ACCEPTED", a, flush=True)
        c.close()
    except OSError:
        break
