from dotenv import load_dotenv
load_dotenv()
from typing import Dict, Iterable, List, Optional
import pandas as pd, subprocess, csv, sys, time, ast
from pathlib import Path
from dynamov2.database.db_helper import db_helper
from dynamov2.logger.logger import CustomLogger
from src.flowmeter_setup import setup_flowmeter
from src.extract_hostname import extract_server_hostnames_df
from src.process_traffic_profile import process_traffic_profile
from dotenv import load_dotenv

class ProcessTraffic:
    def __init__(self, pcap_name: str):
        self.input_pcap = pcap_name
        self.pcap_size = Path(pcap_name).stat().st_size / (1024 * 1024)
        self.iana_mapping = self.build_port_service_map('src/iana_port_mapping.csv')
        self.generate_cicflowmeter_output()
        self.traffic_profile = self.process_csv(self.generate_csv(self.input_pcap), self.input_pcap)
        # self.time_series = self.generate_time_series()
        # self.time_series_payload = self._build_time_series_payload()

    def build_port_service_map(
        self,
        csv_path: str,
        service_col: str = "Service Name",
        port_col: str = "Port Number",
        drop_services: Optional[Iterable[str]] = None,
        conflict: str = "first",
    ) -> dict[int, str]:
        """
        Build {port: service_name} from an IANA-style CSV.

        Args:
            csv_path: path to CSV (e.g., '/mnt/data/iana_port_mapping.csv')
            service_col: column with service names (default 'Service Name')
            port_col: column with port numbers/ranges (default 'Port Number')
            drop_services: iterable of service names to skip (e.g., {'tcp'}) (case-insensitive)
            conflict: how to handle duplicate port assignments:
                - 'first'      -> keep the first service seen for a port (default)
                - 'overwrite'  -> always replace with the latest seen

        Returns:
            Dict[int, str]
        """
        def _parse_port_tokens(port_text: str) -> List[int]:
            """
            Parse a Port Number cell which may contain:
            - single ints: '22'
            - comma lists: '22, 2222'
            - ranges: '6000-6063'
            - mixtures: '80, 8080-8081'
            Returns a list of integer ports.
            """
            if not port_text:
                return []
            ports: List[int] = []
            for token in str(port_text).split(','):
                token = token.strip()
                if not token:
                    continue
                if '-' in token:
                    try:
                        start_s, end_s = token.split('-', 1)
                        start, end = int(start_s.strip()), int(end_s.strip())
                        if start > end:
                            start, end = end, start  # tolerate reversed ranges
                        ports.extend(range(start, end + 1))
                    except ValueError:
                        # malformed range -> skip
                        continue
                else:
                    # single number
                    try:
                        ports.append(int(token))
                    except ValueError:
                        # non-numeric token -> skip
                        continue
            return ports

        drop_set = {s.lower() for s in (drop_services or [])}
        out: Dict[int, str] = {}

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # basic header sanity
            if service_col not in reader.fieldnames or port_col not in reader.fieldnames:
                raise ValueError(
                    f"CSV must contain columns '{service_col}' and '{port_col}'. "
                    f"Found: {reader.fieldnames}"
                )

            for row in reader:
                service_raw = (row.get(service_col) or "").strip()
                port_text = (row.get(port_col) or "").strip()
                if not service_raw or not port_text:
                    continue

                # optionally drop certain service names
                if service_raw.lower() in drop_set:
                    continue

                ports = _parse_port_tokens(port_text)
                if not ports:
                    continue

                for p in ports:
                    if not (0 <= p <= 65535):
                        continue
                    if p in out:
                        if conflict == "overwrite":
                            out[p] = service_raw
                        elif conflict == "first":
                            # keep existing
                            pass
                        else:
                            raise ValueError("conflict must be 'first' or 'overwrite'")
                    else:
                        out[p] = service_raw
        return out

    def _is_cache_valid(self, output_path: Path, source_path: Path) -> bool:
        try:
            if not output_path.exists():
                return False
            if output_path.stat().st_size <= 0:
                return False
            return output_path.stat().st_mtime >= source_path.stat().st_mtime
        except FileNotFoundError:
            return False

    def generate_cicflowmeter_output(self):
        '''
        Generate cicflowmeter output. 
        The current working directory will be mounted to /app so the container will have access to /pcap
        '''
        setup_flowmeter()
        print("Flowmeter setup")
        pcap_path = Path(self.input_pcap)  # e.g. pcap/123_repo_host_model_runid.pcap

        # The container writes to /app/cicflowmeter_output, which is mounted to this NAS path.
        # Keep this in sync with stage4/src/flowmeter_setup.py.
        nas_out_dir = Path("/mnt/NAS/dynamov2/cicflowmeter_output")
        expected_host_csv = nas_out_dir / pcap_path.with_suffix(".csv").name

        if self._is_cache_valid(expected_host_csv, pcap_path):
            print(f"CICFlowMeter output exists; skipping: {expected_host_csv}")
            return

        out_dir = Path("cicflowmeter_output")
        csv_path = out_dir / pcap_path.with_suffix(".csv").name  # -> cicflowmeter_output/<name>.csv (inside container)

        try:
            start = time.perf_counter()
            subprocess.run(
                ["docker", "exec", "flowmeter_container", "cicflowmeter", "-f", str(self.input_pcap), "-c", str(csv_path)],
                check=True,
            )
            elapsed = time.perf_counter() - start
            print(f"flowmeter readings completed in {elapsed:.1f}s")
        except Exception as e:
            #This should not be reached, exit the code so that it can be investigated
            print(f"Error: {e}")
            sys.exit()

    def filter_pcap(self, input_pcap: str):
        '''
        Filter pcap to remove meaningless traffic to endre the amount of traffic in the pcap does not have specific protocols
        '''
        def build_filter(drop_ethertypes, drop_ipv4_protos, drop_icmpv6=True, drop_mdns=True):
            parts = []
            if drop_ethertypes:
                parts.append("not (" + " or ".join(f"eth.type==0x{e:04x}" for e in sorted(drop_ethertypes)) + ")")
            if drop_ipv4_protos:
                parts.append("not (ip and (" + " or ".join(f"ip.proto=={p}" for p in sorted(drop_ipv4_protos)) + "))")
            if drop_icmpv6:
                parts.append("not icmpv6")
            if drop_mdns:
                parts.append("not (udp.port == 5353)")
            return " and ".join(parts) if parts else ""

        pcap_file = Path(input_pcap)
        self.pcap_size = pcap_file.stat().st_size / (1024 * 1024)
        output_pcap = pcap_file.with_stem(pcap_file.stem + "_filtered")

        # EtherType constants
        ETHERTYPE_ARP    = 0x0806
        ETHERTYPE_LLDP   = 0x88CC
        ETHERTYPE_MPLS_U = 0x8847
        ETHERTYPE_MPLS_M = 0x8848
        ETHERTYPE_PPPoED = 0x8863
        ETHERTYPE_PPPoES = 0x8864

        # IPv4 protocol numbers
        IPPROTO_ICMP = 1
        IPPROTO_IGMP = 2

        # IPv6 next header numbers
        IP6_TCP   = 6
        IP6_UDP   = 17
        IP6_ICMP6 = 58

        # ---- Configure drops ----
        DROP_L2_ETHERTYPES = {
            ETHERTYPE_ARP,       # ARP
            ETHERTYPE_LLDP,      # LLDP
            ETHERTYPE_MPLS_U,    # MPLS unicast
            ETHERTYPE_MPLS_M,    # MPLS multicast
            ETHERTYPE_PPPoED,    # PPPoE Discovery
            ETHERTYPE_PPPoES,    # PPPoE Session
        }
        DROP_IPV4_PROTOS = {IPPROTO_ICMP, IPPROTO_IGMP}  # ICMPv4, IGMP

        DROP_ICMPV6 = True

        display_filter = build_filter(DROP_L2_ETHERTYPES, DROP_IPV4_PROTOS, DROP_ICMPV6)
        cmd = [
            "tshark", "-n", "-r", input_pcap,
            "-Y", display_filter,
            "-w", output_pcap, "-F", "pcap",
            "-o", "tcp.desegment_tcp_streams:false", 
            "-o", "http.desegment_body:false",
        ]
        subprocess.run(cmd, check=True)
        return output_pcap
    
    def generate_csv(self, pcap_file: str):
        '''
        tshark processing for application level information.
        '''
        cmd = [
        "tshark",
        "-n",                    # no DNS lookups (faster)
        "-r", pcap_file,           
        "-T", "fields",          # export as fields
        "-E", "header=y",        # include CSV header
        "-E", "separator=,",     # comma separated
        "-E", "quote=d",
        "-e", "frame.number",
        "-e", "frame.time_epoch",
        "-e", "frame.len",
        "-e", "frame.protocols",
        "-e", "ip.proto",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "tcp.srcport",
        "-e", "tcp.dstport",
        "-e", "tcp.stream",
        "-e", "udp.srcport",
        "-e", "udp.dstport",
        "-e", "udp.stream",
        "-e", "dns.qry.name",     # domain name queried
        "-e", "dns.a",            # resolved IPv4 address (A record)
        "-e", "dns.aaaa",         # resolved IPv6 address (AAAA record)
        ]
        

        pcap_file = Path(pcap_file)
        generated_csv = pcap_file.with_suffix(".csv")

        if self._is_cache_valid(generated_csv, pcap_file):
            print(f"tshark CSV exists; skipping: {generated_csv}")
            return generated_csv

        start = time.perf_counter()
        with open(generated_csv, "w") as f:
            # tshark prints "reading from file ..." to stderr; suppress to reduce confusion.
            subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL, check=True)
        elapsed = time.perf_counter() - start
        print(f"tshark CSV generated in {elapsed:.1f}s: {generated_csv}")
        return generated_csv

    def process_csv(self, input_csv: str, pcap_path: str) -> Dict:
        dataframe = pd.read_csv(str(input_csv))

        #Setting up dataframe
        bin_size = 1
        dataframe["iat"] = dataframe["frame.time_epoch"].diff()
        dataframe["iat"] = dataframe["iat"].fillna(0)
        dataframe['frame.protocols'] = dataframe["frame.protocols"].apply(lambda x: x.split(":")[-1] if pd.notnull(x) else x)
        dataframe["bin"] = (dataframe["frame.time_epoch"] // bin_size).astype(int)
        dataframe['bytes_per_bin'] = dataframe.groupby('bin')['frame.len'].transform('sum') / bin_size
                
        ###############
        '''
        For testing purpose
        '''
        self.start_time = dataframe['frame.time_epoch'].min()
        print("Setting start_time to earliest packet in pcap: ", self.start_time)
        ###############

        pcap_profile = {}
        #Number of hosts
        host_num = dataframe['ip.src'].nunique()

        #Number of flows
        tcp_flow = dataframe['tcp.stream'].nunique() if 'tcp.stream' in dataframe else 0
        udp_flow = dataframe['udp.stream'].nunique() if 'udp.stream' in dataframe else 0
        flow_count = tcp_flow + udp_flow

        #Stats should all be per application from here on.

        #Traffic amount based on protocol.

        tcp_streams = dataframe[~dataframe['tcp.stream'].isna()]
        udp_streams = dataframe[~dataframe['udp.stream'].isna()]
        tcp_traffic = (tcp_streams
            .groupby('tcp.stream', as_index=False) #To calculate per flow
            .agg(application_port=('tcp.dstport','min'),
                total_size=('frame.len','sum'),
                protocols_seen=('frame.protocols', lambda s: sorted([p for p in s.explode().dropna().unique() if p != 'tcp'])),
                traffic_start=('frame.time_epoch', 'min'),
                traffic_end=('frame.time_epoch', 'max'),
                n_packets=('frame.len','size'),
                tcp_stream=('tcp.stream', 'first'),
                src_ips=('ip.src', lambda s: sorted(pd.unique(s))))
            .groupby('application_port') #To attribute flows to applications
            .agg(total_size=('total_size','sum'),
                    tshark_parsing=('protocols_seen', lambda s: sorted(s.explode().dropna().unique())),
                    traffic_start=('traffic_start', 'min'),
                    traffic_end=('traffic_end', 'max'),
                    n_packets=('n_packets','sum'),
                    hosts=('src_ips', lambda s: s.explode().nunique()),
                    flow_count=('tcp_stream','size'))
        )

        tcp_traffic.index = tcp_traffic.index.astype(int)
        tcp_traffic = tcp_traffic.to_dict(orient='index')

        udp_traffic = (udp_streams
            .groupby('udp.stream', as_index=False) #To calculate per flow
            .agg(application_port=('udp.dstport','min'),
                total_size=('frame.len','sum'),
                protocols_seen=('frame.protocols', lambda s: sorted([p for p in s.explode().dropna().unique() if p != 'udp'])),
                traffic_start=('frame.time_epoch', 'min'),
                traffic_end=('frame.time_epoch', 'max'),
                time_seen=('frame.time_epoch', 'min'),
                n_packets=('frame.len','size'),
                udp_stream=('udp.stream', 'first'),
                src_ips=('ip.src', lambda s: sorted(pd.unique(s))))
            .groupby('application_port') #To attribute flows to applications
            .agg(total_size=('total_size','sum'),
                    tshark_parsing=('protocols_seen', lambda s: sorted(s.explode().dropna().unique())),
                    traffic_start=('traffic_start', 'min'),
                    traffic_end=('traffic_end', 'max'),
                    time_seen=('time_seen', 'min'),
                    n_packets=('n_packets','sum'),
                    hosts=('src_ips', lambda s: s.explode().nunique()),
                    flow_count=('udp_stream','size'))
                    )

        udp_traffic.index = udp_traffic.index.astype(int)
        udp_traffic = udp_traffic.to_dict(orient='index')


        #Include IANA mappings
        for d in [tcp_traffic, udp_traffic]:
            for key in list(d.keys()):  # snapshot of keys
                d[key]['iana_mapping'] = self.iana_mapping.get(key, "unknown")
                d[key]['traffic_start'] = float(d[key]['traffic_start']-self.start_time)
                d[key]['traffic_end'] = float(d[key]['traffic_end'] - self.start_time)
                d[key]['traffic_duration'] = float(d[key]['traffic_end'] - d[key]['traffic_start'])

        hostname_dict = extract_server_hostnames_df(pcap_path).to_dict(orient="records")

        pcap_profile = {
            'host_num': host_num,
            'flow_count': flow_count,
            'pcap_size': self.pcap_size,
            'tcp_traffic': tcp_traffic,
            'udp_traffic': udp_traffic,
            'hostname_info': hostname_dict
        }

        return pcap_profile

if __name__ == '__main__':
    # db_helper = RemoteDBHelper()
    logger = CustomLogger("PCAP Processing", logfile_name="pcap_processing")
    row = db_helper.get_unchecked_agent_traffic_parameters()
    load_dotenv()
    # row = db_helper.get_github_repository(repository_id=86704)
    while True:
        if row:
            logger.info(f"Retrieved row: {row}")
            # row_id, row_name, row_processing_host, row_model, run_id = ast.literal_eval(row)
            repo_owner,repo_name = row.name.split("/")
            pcap_path = f"pcap/{row.id}_{repo_owner}_{repo_name}_{row.processing_host}_{row.model}_{row.run_id}.pcap"#.replace("-","_")
            pc = ProcessTraffic(pcap_name=pcap_path)
            payload = {
                'application_flow': pc.traffic_profile,
            }
            logger.info("Processed PCAP to get traffic_profile")
            distinct_applications = db_helper.get_distinct_applications()
            response = process_traffic_profile(pc.traffic_profile, distinct_applications)
            if isinstance(response, dict):
                payload = payload | response
                logger.info("Processed LLM response to traffic_profile")
            success, message = db_helper.update_agent_traffic_parameters(row.id, row.run_id, model=row.model, **payload)
            if success:
                logger.info(message)
            else:
                logger.warning(message)
        else:
            time.sleep(10)
        row = db_helper.get_unchecked_agent_traffic_parameters()
    print("No more rows to process")
