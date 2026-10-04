import socket
import struct
import time

host_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
print("host UTC", host_utc)


def ntp_query(server):
    data = b"\x1b" + 47 * b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    t0 = time.time()
    sock.sendto(data, (server, 123))
    payload, _ = sock.recvfrom(1024)
    t1 = time.time()
    sock.close()
    seconds = struct.unpack("!I", payload[40:44])[0] - 2208988800
    return seconds + (t1 - t0) / 2  # add half the round trip


for server in ("time.windows.com", "pool.ntp.org"):
    try:
        ref = ntp_query(server)
        ref_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ref))
        print("ntp", server, ref_str, "host-skew-seconds %.1f" % (time.time() - ref))
    except Exception as exc:
        print("ntp", server, "failed", repr(exc))
