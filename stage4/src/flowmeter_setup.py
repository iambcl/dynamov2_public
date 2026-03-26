import docker, os
from pathlib import Path

def setup_flowmeter():
    client = docker.from_env()
    name = "flowmeter_container"
    try:
        container = client.containers.get(name)
        if container.status != "running":
            container.start()
    except docker.errors.NotFound:
        build_context = Path(__file__).resolve().parent
        image, logs = client.images.build(
            path=str(build_context),
            dockerfile="dockerfile",
            tag="flowmeter",
        )
        for log in logs:
            print(log)
        workdir = str(os.getcwd())+ "/pcap"
        nas_out = Path("/mnt/NAS/dynamov2/cicflowmeter_output").resolve()
        client.containers.run(
            "flowmeter",
            name=name,
            command="sleep infinity",
            volumes={
                workdir: {"bind": "/app/pcap", "mode": "rw"},  # mount CWD
                str(nas_out): {"bind": "/app/cicflowmeter_output", "mode": "rw"},
            },
            working_dir="/app",
            stdin_open=True,
            tty=True,
            detach=True,
        )
    except Exception as e:
        print(f"Error encountered: {e}")

if __name__ == '__main__':
    setup_flowmeter()
