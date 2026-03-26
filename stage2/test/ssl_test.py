import socket
import ssl
import time

host = 'www.example.com'
ctx = ssl.create_default_context()
ctx.keylog_filename = '/var/log/ssl_secrets/sslkey.log'

print('Starting TLS connection to', host)
try:
    with socket.create_connection((host, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            ssock.sendall(b'GET / HTTP/1.1\r\nHost: ' + host.encode() + b'\r\nConnection: close\r\n\r\n')
            _ = ssock.recv(1024)
    print('TLS request finished')
except Exception as e:
    print('TLS request error:', e)

# give the keylogger a moment to flush
time.sleep(1)
