# SSL Keylog Test

This folder contains a small test that performs a real TLS connection and writes the SSL key log to `../ssl_logs/sslkey.log` using Python's `SSLContext.keylog_filename`.

Files:
- `docker-compose.yml` — runs the Python TLS client in a container and mounts `../ssl_logs`.
- `ssl_test.py` — performs a TLS handshake to `www.example.com`.
- `run_test.sh` — helper script to run the test and print `sslkey.log`.

Run:

```bash
cd stage2/test
chmod +x run_test.sh
./run_test.sh
```

Check `../ssl_logs/sslkey.log` for key lines (e.g. `CLIENT_RANDOM` or similar).
