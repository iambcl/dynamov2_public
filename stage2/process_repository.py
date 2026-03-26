from typing import List
import os, subprocess, re, time, json, sys, shutil, requests, sys, socket, tempfile
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from dynamov2.logger.logger import CustomLogger
from dynamov2.database.db_helper import db_helper
from dynamov2.git_utils.utils import clone_github_repo    
try:
    import yaml
except Exception:
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parent

class Repository:
    def __init__(self, repo_url: str, repo_path: str, pcap_time: int, docker_compose_file_paths: List[str], pcap_location = None):
        if docker_compose_file_paths is None:
            self.error_message = "No docker compose files present."
            return 
        self.project_root = PROJECT_ROOT
        self.pcap_size_check_limit = 10**6 #Check 1 minute pcap before proceeding with full capture.
        self.start_time = None
        self.pcap_location = pcap_location
        '''
        Download github repo, determine subnets and generate pcap
        '''
        try:
            clone_github_repo(repo_url, docker_compose_file_paths)
            self.get_repo_details(repo_path)
            self.repo_dir = os.getenv("REPO_DIRECTORY")
            self.parse_docker_compose_files(docker_compose_file_paths) #Generate the docker-compose command based on the input file paths
            self.subnets = None
            self.error_message = self.run(pcap_time)
        except Exception as e:
            self.error_message = f"error: {e}"
        finally:
            self.cleanup_repository()

    def parse_docker_compose_files(self, docker_compose_file_paths: List[str]):
        # derive a stable compose project name from the checked-out repo directory
        # sanitize to avoid invalid characters and force lowercase
        project_name = None
        try:
            project_name = Path(self.repo_dir).name
        except Exception:
            project_name = None
        if not project_name:
            project_name = (self.repo_name or "repo").split("/")[-1]
        # replace non-alphanumeric chars with underscore and lowercase
        safe_project = re.sub(r'[^A-Za-z0-9_.-]', '_', project_name).lower()
        self.safe_project = safe_project
        print(f"Using compose project name: {self.safe_project}")

        # build docker compose base command including --project-name for predictability
        self.docker_compose_commands = ['docker', 'compose', '--project-name', self.safe_project]
        if not docker_compose_file_paths:
            print("No docker compose files provided.")
            return
        # keep original file paths for modification later
        self.compose_file_paths = docker_compose_file_paths
        for compose_path in docker_compose_file_paths:
            self.docker_compose_commands.extend(["-f", compose_path])

    def modify_compose_files(self):
        """
        For each compose file path, inject SSLKEYLOG env var and a volume mapping
        into every service if not already present.
        """
        if not getattr(self, 'compose_file_paths', None):
            return
        for orig_path in self.compose_file_paths:
            try:
                p = Path(orig_path)
                if not p.is_absolute():
                    p = Path(self.repo_dir) / orig_path
                if not p.exists():
                    print(f"Compose file not found, skipping: {p}")
                    continue
                if yaml is None:
                    print("PyYAML not available — skipping compose modification.")
                    return
                with open(p, 'r') as fh:
                    data = yaml.safe_load(fh) or {}
                services = data.get('services') or {}
                modified = False
                for svc_name, svc_def in services.items():
                    if svc_def is None:
                        svc_def = {}
                    # Environment can be dict or list
                    env_val = svc_def.get('environment')
                    ssl_env_entry = 'SSLKEYLOGFILE=/var/log/ssl_secrets/sslkey.log'
                    if env_val is None:
                        svc_def['environment'] = [ssl_env_entry]
                        modified = True
                    else:
                        if isinstance(env_val, dict):
                            if 'SSLKEYLOGFILE' not in env_val:
                                env_val['SSLKEYLOGFILE'] = '/var/log/ssl_secrets/sslkey.log'
                                svc_def['environment'] = env_val
                                modified = True
                        elif isinstance(env_val, list):
                            if not any(str(x).startswith('SSLKEYLOGFILE=') for x in env_val):
                                env_val.append(ssl_env_entry)
                                svc_def['environment'] = env_val
                                modified = True
                    # Volumes
                    vol_val = svc_def.get('volumes')
                    vol_entry = '../ssl_logs:/var/log/ssl_secrets'
                    if vol_val is None:
                        svc_def['volumes'] = [vol_entry]
                        modified = True
                    else:
                        if isinstance(vol_val, list):
                            if vol_entry not in vol_val:
                                vol_val.append(vol_entry)
                                svc_def['volumes'] = vol_val
                                modified = True
                        else:
                            # unexpected format; skip
                            pass
                    services[svc_name] = svc_def
                if modified:
                    data['services'] = services
                    # write back YAML
                    with open(p, 'w') as fh:
                        yaml.safe_dump(data, fh, default_flow_style=False)
                    print(f"Modified compose file: {p}")
            except Exception as e:
                print(f"Failed to modify compose file {orig_path}: {e}")

    def cleanup_repository(self) -> None:
        # no global chdir needed; use absolute paths and subprocess cwd where required

        repo_dir = getattr(self, "repo_dir", None)
        if not repo_dir:
            return
        try:
            print(f"Deleting repository directory: {repo_dir}")
            subprocess.run(["sudo", "rm", "-rf", repo_dir])
            print("Deletion successful.")
        except Exception:
            pass
        
    def run(self,pcap_time):
        '''
        Initiate process to take a PCAP of the repository.
        '''
        test_result, e = self.run_docker_compose(pcap_time)
        return e

    def start_pcap(self, pcap_time):
        hostname = socket.gethostname()
        safe_repo_owner = (self.repo_owner or "unknown_owner").replace("/", "_")
        safe_repo_name = (self.repo_name or "unknown_repo").replace("/", "_")
        default_pcap_name = f"{safe_repo_owner}_{safe_repo_name}_{hostname}_run_0.pcap"
        if self.pcap_location:
            final_pcap_path = Path(self.pcap_location)
        else:
            final_pcap_path = self.project_root / "pcap" / default_pcap_name
        final_pcap_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f"{final_pcap_path.stem}_", suffix=".pcap")
        os.close(tmp_fd)
        tmp_pcap_path = Path(tmp_name)

        tsharkcmd = ["tshark"]
        for bridge in self.bridges:
            tsharkcmd.append("-i")
            tsharkcmd.append(bridge)
        tsharkcmd.append("-w")
        tsharkcmd.append(str(tmp_pcap_path))

        #Force it to save in .pcap format instead of pcapng
        tsharkcmd.append("-F")
        tsharkcmd.append("pcap")

        tsharkcmd.append("-a")
        tsharkcmd.append(f"duration:{pcap_time}")

        #Ignore currently running containers:
        if self.bridges == ["docker0"]:
            tsharkcmd.append("-f")
            tsharkcmd.append("not host 172.17.0.2")

        print("Listening on bridges: ", self.bridges)
        print(tsharkcmd)
        self.start_time = int(time.time())
        os.chmod(tmp_pcap_path, 0o666)
        proc = subprocess.Popen(tsharkcmd)
        self.tmp_pcap_path = tmp_pcap_path
        self.final_pcap_path = final_pcap_path
        self.tshark_proc = proc
        return proc


    def stop_pcap(self, pcap_time: int) -> None:
        time.sleep(pcap_time)
        subprocess.run(
            self.docker_compose_commands + ["down", "--remove-orphans", "--volumes", "--rmi", "all"],
            cwd=self.repo_dir,
        )

    def get_repo_details(self, repo_path) -> str:
        repo_owner, repo_name = repo_path.rstrip("/").split("/")
        self.repo_owner = repo_owner
        self.repo_name = repo_name

    def run_docker_compose(self, pcap_time):
        '''
        Generate pcaps based from the docker compose files by running them with docker.
        '''
        def _size_check(limit: int) -> bool:
            size_bytes = self.tmp_pcap_path.stat().st_size
            print("Pcap has size: ", size_bytes)
            if size_bytes < limit:
                return False
            return True
        
        cwd = os.getcwd()
        repo_cwd = self.repo_dir
        # Attempt to modify compose files in-place to inject SSL log env and mount
        try:
            self.modify_compose_files()
        except Exception as e:
            print(f"compose modification skipped: {e}")
        '''
        Run commands generated from previous steps. If there is no commmands available, try to run it with a default command.
        '''
        try:
            build_command = self.docker_compose_commands + ["up", "--no-start"] #Build the containers
            build_result = subprocess.run(
                build_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=repo_cwd,
                timeout=300
            )
            if build_result.returncode != 0:
                error_output = (build_result.stdout or "").strip() or None
                payload = {
                    "status": "error",
                    "reason": "image_build_failed",
                    "exit_code": build_result.returncode,
                    "output": error_output,
                }
                message = json.dumps(payload)
                print(message)
                try:
                    subprocess.run(
                        self.docker_compose_commands + ["down"],
                        check=True,
                        cwd=repo_cwd,
                    )
                except Exception as cleanup_error:
                    print(f"Attempted cleanup after docker-compose failure raised: {cleanup_error}")
                return False, message

            # get network ID and name using docker's --format for predictable parsing
            try:
                # use the sanitized project name when filtering networks
                networks = subprocess.check_output([
                    "docker",
                    "network",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={getattr(self, 'safe_project', (self.repo_name or '').lower())}",
                    "--format",
                    "{{.ID}} {{.Name}}",
                ]).decode()
            except subprocess.CalledProcessError:
                networks = ""
            print(f"networks (by label): {networks}")
        except subprocess.CalledProcessError as e:
            payload = {
                "status": "error",
                "reason": "network_inspect_failed",
                "detail": str(e),
            }
            try:
                payload["stderr"] = e.stderr
                payload["stdout"] = e.stdout
            except Exception:
                pass
            message = json.dumps(payload)
            print(message)
            return False, message
            
        bridges = []
        subnets = []

        # Parse the formatted output lines of "<id> <name>" reliably
        lines = [ln.strip() for ln in (networks or "").splitlines() if ln.strip()]
        if lines:
            for ln in lines:
                try:
                    network_id, network_name = ln.split(None, 1)
                except ValueError:
                    continue
                # Docker bridge interface name is typically br-<network-id>
                bridges.append("br-" + network_id)
                try:
                    out = subprocess.check_output(["docker", "network", "inspect", network_id])
                    info = json.loads(out)[0]
                    cfgs = (info.get("IPAM") or {}).get("Config") or []
                    for cfg in cfgs:
                        subnet = cfg.get("Subnet")
                        if subnet:
                            subnets.append((network_name, subnet))
                except Exception:
                    # ignore inspect failures for individual networks
                    continue
        else:
            # Fallback: try to find networks whose name contains the repo name or owner
            try:
                all_networks = subprocess.check_output([
                    "docker",
                    "network",
                    "ls",
                    "--format",
                    "{{.ID}} {{.Name}}",
                ]).decode()
            except subprocess.CalledProcessError:
                all_networks = ""
            candidates = []
            repo_name_lower = (self.repo_name or "").lower()
            repo_owner_lower = (self.repo_owner or "").lower()
            for ln in (all_networks or "").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    nid, nname = ln.split(None, 1)
                except ValueError:
                    continue
                # also check for the sanitized project name in network names
                project_lower = getattr(self, 'safe_project', '').lower()
                if repo_name_lower in nname.lower() or repo_owner_lower in nname.lower() or (project_lower and project_lower in nname.lower()):
                    candidates.append((nid, nname))
            if candidates:
                for network_id, network_name in candidates:
                    bridges.append("br-" + network_id)
                    try:
                        out = subprocess.check_output(["docker", "network", "inspect", network_id])
                        info = json.loads(out)[0]
                        cfgs = (info.get("IPAM") or {}).get("Config") or []
                        for cfg in cfgs:
                            subnet = cfg.get("Subnet")
                            if subnet:
                                subnets.append((network_name, subnet))
                    except Exception:
                        continue
            else:
                print("Network ID and names not found by label or name search.")

        if bridges == []:
            bridges = ["docker0"]

        self.bridges = bridges
        self.subnets = subnets

        # Start capture before starting containers so startup packets are captured
        print("Starting pcap")
        try:
            self.start_pcap(pcap_time)
            time.sleep(2) #Give pcap time to start capturing
        except Exception as e:
            print(f"Failed to start pcap: {e}")
        up_command = self.docker_compose_commands + ["up", "-d"]
        print("Attempting to up containers with command: ", up_command)
        up_result = subprocess.run(
            up_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=repo_cwd,
        )
        print("up_command has completed")
        if up_result.returncode != 0:
            error_output = (up_result.stdout or "").strip() or None
            payload = {
                "status": "error",
                "reason": "docker_compose_up_failed",
                "exit_code": up_result.returncode,
                "output": error_output,
            }
            message = json.dumps(payload)
            print(message)
            try:
                subprocess.run(
                    self.docker_compose_commands + ["down", "--remove-orphans", "--volumes"],
                    check=False,
                    cwd=repo_cwd,
                )
            except Exception as cleanup_error:
                print(f"Attempted cleanup after docker-compose failure raised: {cleanup_error}")
            # terminate tshark if it was started
            try:
                proc = getattr(self, 'tshark_proc', None)
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                pass
            return False, message

        # give containers a few seconds to initialize, then check logs for errors
        print("Waiting for containers to initialize...")
        time.sleep(5)
        error_re = re.compile(r"\bERROR\b|Traceback|Exception|FATAL|panic|failed", re.I)
        logs_cmd = self.docker_compose_commands + ["logs", "--no-color"]
        try:
            logs_result = subprocess.run(
                logs_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=repo_cwd,
                timeout=30,
            )
            logs_out = (logs_result.stdout or "").splitlines()
        except Exception:
            logs_out = []

        errors = []
        services = set()
        for line in logs_out:
            if error_re.search(line):
                # try to extract service name from the logs line (format: "service | message")
                service = "unknown_service"
                m = re.match(r"\s*([^|\s]+)\s*\|\s*(.*)", line)
                if m:
                    service = m.group(1).strip()
                services.add(service)
                errors.append((service, line.strip()))
        print("Checking for errors..")
        if errors:
            print("errors found in compose logs.")
            # terminate tshark if running (once)
            try:
                proc = getattr(self, 'tshark_proc', None)
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            except Exception:
                pass
            # remove temp pcap if present
            try:
                if getattr(self, 'tmp_pcap_path', None):
                    self.tmp_pcap_path.unlink(missing_ok=True)
            except Exception:
                pass
            # bring containers down
            try:
                subprocess.run(
                    self.docker_compose_commands + ["down", "--remove-orphans", "--volumes"],
                    check=False,
                    cwd=repo_cwd,
                )
            except Exception:
                pass
            services_list = sorted(list(services))
            payload = {
                "status": "error",
                "reason": "service_errors",
                "services": services_list,
                "errors": [{"service": s, "line": l} for s, l in errors],
            }
            return False, json.dumps(payload)
        print("No errors.")
        os.chdir(cwd) #to set the location of the pcap.
        # Stop pcap after the configured duration and bring containers down
        self.stop_pcap(pcap_time)
        print("Stopping pcap")
        os.chdir(cwd)
        '''
        Fail condition: PCAP fails the size check.
        '''
        if _size_check(self.pcap_size_check_limit) == False:
            try:
                self.tmp_pcap_path.unlink(missing_ok=True)
            except OSError:
                pass
            e = "PCAP has not met the size requirements."
            return False, e
        self.final_pcap_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Copying temp pcap {self.tmp_pcap_path} -> final {self.final_pcap_path}")
        try:
            shutil.copy2(self.tmp_pcap_path, self.final_pcap_path)
        except Exception as e:
            print(f"Failed to copy pcap to final location: {e}")
            try:
                self.tmp_pcap_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"pcap_copy_failed: {e}"
        exists = self.final_pcap_path.exists()
        size = None
        try:
            if exists:
                size = self.final_pcap_path.stat().st_size
        except Exception:
            size = None
        print(f"Final pcap exists: {exists}, size: {size}")
        try:
            self.tmp_pcap_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.pcap_location = str(self.final_pcap_path)
        print(f"Set pcap_location -> {self.pcap_location}")
        return True, None

if __name__ == '__main__':
    load_dotenv()
    GITHUB_API = "https://api.github.com/repos/"
    TOKEN = os.getenv("GITHUB_TOKEN")  # or paste your PAT directly
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
    }
    logger = CustomLogger("Traffic Check", logfile_name="stage3:no_further_setup")
    count = 0
    while True:
        row = db_helper.get_unchecked_repository(stage=3)
        # row = db_helper.get_github_repository(repository_id=12724) #For testing purposes only
        if len(row.cleaned_docker_compose_filepath) == 0:
            traffic_parameters_update_parameters = {'repository_id': row.id,
                                        'failure_reason': "Repository has no valid docker compose filepaths.",
                                        'one_minute_check': False}
            logger.info(f"Repository ID {row.id} has no valid docker compose filepaths. Setting one_minute_check to False.")
            db_helper.update_traffic_parameters(**traffic_parameters_update_parameters)
            count += 1
            continue
        text = None
        status_code = None
        ###############################################################
        #Error handling: unknown occurrence of directory affecting git clone process
        path = os.path.expanduser("~/.gitconfig")
        if os.path.isdir(path):
            print("Found .gitconfig directory — removing it...")
            shutil.rmtree(path)
        ###############################################################
        if row: #Used to handle HTTP rate limit exceptions
            logger.info(f"Retrieved row: {row.id}: {row.name}")
            owner = row.name
            try:
                r = requests.get(f"https://api.github.com/repos/{owner}", headers=headers) #Do error handling for private repos
            except Exception as e:
                logger.info(f"error {e}.\n Attempting again")
                time.sleep(5)
                r = requests.get(f"https://api.github.com/repos/{owner}", headers=headers)
            while r.ok != True:
                if r.headers.get("X-RateLimit-Remaining",0) == '0':
                    reset_at = int(r.headers.get("X-RateLimit-Reset", 0))
                    now = int(time.time())
                    sleep_time = max(0, reset_at - now)
                    logger.info(f"Rate limit hit. Sleeping for {sleep_time}")
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    r = requests.get(f"https://api.github.com/repos/{owner}", headers=headers)
                elif r.status_code == 404 or r.status_code == 451:
                    status_code = r.status_code
                    text = r.text
                    break
                else:
                    logger.warning(f"error {r.status_code}: {r.text}")
                    sys.exit()
            r = r.json()
            if status_code == 404 or status_code == 451:
                logger.info(f"error: {status_code} {text}. Setting database record to False")
                traffic_parameters_update_parameters = {'repository_id': row.id,
                                                        'failure_reason': f"{status_code}: {text}",
                                                        'one_minute_check': False}
            elif r['private'] == False: #This is not a private repository
                repo = Repository(repo_url=row.url, repo_path=row.name, docker_compose_file_paths=row.cleaned_docker_compose_filepath, pcap_time=60)
                if repo.error_message != None:
                    logger.info(f"error: {repo.error_message}. Setting database record to False")
                    traffic_parameters_update_parameters = {'repository_id': row.id,
                                                            'failure_reason': repo.error_message,
                                                            'one_minute_check': False}
                else:
                    traffic_parameters_update_parameters = {'repository_id': row.id,
                                                            'subnets': repo.subnets,
                                                            'one_minute_check': True}
                if getattr(repo,"pcap_location", None) is not None:
                    try:
                        subprocess.run(["mv",repo.pcap_location,"pcap/"], check=True)
                    except:
                        pass
            elif r['private'] == True: #This is a private repository
                traffic_parameters_update_parameters = {'repository_id': row.id,
                                                        'failure_reason': "Repository has been set to private.",
                                                        'one_minute_check': False}
            traffic_parameters_update_parameters['processing_host'] = socket.gethostname()
            def _strip_nuls(value):
                if isinstance(value, str):
                    return value.replace("\x00", "")
                if isinstance(value, list):
                    return [_strip_nuls(item) for item in value]
                if isinstance(value, tuple):
                    return tuple(_strip_nuls(item) for item in value)
                if isinstance(value, dict):
                    return {k: _strip_nuls(v) for k, v in value.items()}
                return value

            traffic_parameters_update_parameters = _strip_nuls(traffic_parameters_update_parameters)
            db_helper.update_traffic_parameters(**traffic_parameters_update_parameters)
            count += 1
            if count > 10:
                #Makes sure the disk space doesnt run out
                subprocess.run(["docker","image","prune","-af"])
                subprocess.run(["docker","builder","prune", "-f"])
                count = 0
        else:
            time.sleep(10)
