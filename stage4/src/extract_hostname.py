import subprocess
import pandas as pd
from io import StringIO
import re
from pathlib import Path

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$|^[0-9a-fA-F:]+$")

def extract_server_hostnames_df(pcap_path: str) -> pd.DataFrame:
    """
    Server-centric mapping: return unique (ip, hostname) pairs for servers.
    Sources:
      - DNS A answers: ip=dns.a, hostname=dns.qry.name
      - HTTP Host:     ip=ip.dst, hostname=http.host
      - TLS SNI:       ip=ip.dst, hostname=tls.handshake.extensions_server_name
      - NBNS:          ip=ip.dst, hostname=nbns.name
    """
    pcap_p = Path(pcap_path)
    cache_path = pcap_p.with_suffix(".hostnames.csv")
    try:
        if cache_path.exists() and cache_path.stat().st_size > 0 and cache_path.stat().st_mtime >= pcap_p.stat().st_mtime:
            return pd.read_csv(cache_path)
    except FileNotFoundError:
        # pcap disappeared or cache not accessible; fall through to fresh extraction
        pass

    tshark_cmd = [
        "tshark", "-r", pcap_path,
        "-Y", "dns or http.host or nbns or bootp or tls.handshake.extensions_server_name",
        "-T", "fields",
        "-E", "header=y", "-E", "separator=,", "-E", "occurrence=f",
        "-e", "frame.time", "-e", "ip.src", "-e", "ip.dst",
        "-e", "dns.qry.name", "-e", "dns.a",
        "-e", "http.host", "-e", "nbns.name",
        "-e", "tls.handshake.extensions_server_name",
    ]

    # Write to disk to avoid keeping potentially-large stdout in memory.
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        subprocess.run(tshark_cmd, stdout=f, stderr=subprocess.DEVNULL, text=True, check=True)
    tmp_path.replace(cache_path)
    df = pd.read_csv(cache_path)

    # Ensure columns exist
    for c in ["ip.src","ip.dst","dns.qry.name","dns.a","http.host","nbns.name","tls.handshake.extensions_server_name"]:
        if c not in df.columns:
            df[c] = pd.NA

    rows = []

    # 1) DNS A answers → (dns.a, dns.qry.name)
    dns_mask = df["dns.qry.name"].notna() & df["dns.a"].notna()
    if dns_mask.any():
        part = df.loc[dns_mask, ["dns.a", "dns.qry.name"]].rename(
            columns={"dns.a": "ip", "dns.qry.name": "hostname"}
        )
        part["source"] = "DNS_A"
        rows.append(part)

    # 2) HTTP Host → (ip.dst, http.host)
    http_mask = df["http.host"].notna()
    if http_mask.any():
        part = df.loc[http_mask, ["ip.dst", "http.host"]].rename(
            columns={"ip.dst": "ip", "http.host": "hostname"}
        )
        part["source"] = "HTTP_HOST"
        rows.append(part)

    # 3) TLS SNI → (ip.dst, sni)
    sni_col = "tls.handshake.extensions_server_name"
    sni_mask = df[sni_col].notna()
    if sni_mask.any():
        part = df.loc[sni_mask, ["ip.dst", sni_col]].rename(
            columns={"ip.dst": "ip", sni_col: "hostname"}
        )
        part["source"] = "TLS_SNI"
        rows.append(part)

    # 4) NBNS → (ip.dst, nbns.name)
    nbns_mask = df["nbns.name"].notna()
    if nbns_mask.any():
        part = df.loc[nbns_mask, ["ip.dst", "nbns.name"]].rename(
            columns={"ip.dst": "ip", "nbns.name": "hostname"}
        )
        part["source"] = "NBNS"
        rows.append(part)

    if not rows:
        return pd.DataFrame(columns=["ip", "hostname"])

    out_df = pd.concat(rows, ignore_index=True)

    # Clean & dedupe
    out_df["ip"] = out_df["ip"].astype(str).str.strip()
    out_df["hostname"] = out_df["hostname"].astype(str).str.strip().str.lower()
    out_df = out_df[out_df["ip"].apply(lambda x: bool(IP_RE.match(x)))]
    out_df = out_df[out_df["hostname"].ne("")]

    # Prefer DNS_A over others when duplicates exist
    out_df["priority"] = out_df["source"].map({"DNS_A":0, "TLS_SNI":1, "HTTP_HOST":2, "NBNS":3}).fillna(9)
    out_df = (out_df.sort_values(["ip","hostname","priority"])
                    .drop_duplicates(subset=["ip","hostname"], keep="first")
                    .drop(columns=["priority"])
                    .reset_index(drop=True))

    return out_df

# Example:
# df = extract_server_hostnames_df("pcap/kafka-reactive-app.pcap")
# print(df.head())