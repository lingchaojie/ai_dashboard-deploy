import http.client
import os
from pathlib import Path
import socket

path = Path(os.environ['GATEWAY_INSTALL_DIR']) / 'update-run/updater.sock'
connection = http.client.HTTPConnection('localhost', timeout=3)
connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.sock.settimeout(3)
connection.sock.connect(str(path))
connection.request('GET', '/status')
response = connection.getresponse()
if response.status != 200:
    raise SystemExit(1)
connection.close()
