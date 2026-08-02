import socket, sys, time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(8)
t0 = time.time()
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    print("CONNECT_OK", sys.argv[1], sys.argv[2], "in %.3fs" % (time.time() - t0), flush=True)
except Exception as e:
    print("CONNECT_FAIL", type(e).__name__, e, "in %.3fs" % (time.time() - t0), flush=True)
finally:
    s.close()
