import os, shutil, yaml

def absolutize_compose_paths(compose_file_path: str) -> None:
    """
    Convert all relative host-side paths in a docker-compose YAML to absolute paths.
    Creates a .bak backup next to the original and overwrites the original file.

    Handles:
      - services.*.build (string or dict)
          * build.context  -> absolute
          * build.dockerfile -> normalized path inside context
      - services.*.volumes (short syntax "host:container[:mode]" and long syntax with type: bind)
          * host side becomes absolute
      - services.*.env_file (str or list[str]) -> absolute
      - root-level configs.*.file / secrets.*.file -> absolute
    """

    def _expand_abs(path: str, base_dir: str) -> str:
        if not path:
            return path
        path = os.path.expanduser(path)
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(base_dir, path))

    def _fix_build(build, base_dir):
        # build can be a string (context) or a dict
        if isinstance(build, str):
            return _expand_abs(build, base_dir)
        if isinstance(build, dict):
            if "context" in build:
                build["context"] = _expand_abs(build["context"], base_dir)
            # dockerfile must be relative to context; normalize it against context if present
            if "dockerfile" in build:
                ctx = build.get("context", base_dir)
                # If dockerfile is absolute, make it relative to context when possible; otherwise normalize under context
                df = build["dockerfile"]
                if os.path.isabs(df):
                    # Keep as is if it already lives under context; else just normalize to absolute
                    try:
                        rel = os.path.relpath(df, ctx)
                        # If rel doesn't escape up, prefer relative within context
                        if not rel.startswith(".."):
                            build["dockerfile"] = rel.replace("\\", "/")
                        else:
                            # Fallback: absolute (Compose accepts absolute here in recent versions, but many users keep it relative)
                            build["dockerfile"] = df
                    except Exception:
                        build["dockerfile"] = df
                else:
                    # relative to context
                    build["dockerfile"] = os.path.normpath(os.path.join(ctx, df)).replace("\\", "/")
        return build

    def _fix_env_file(env_file, base_dir):
        if isinstance(env_file, list):
            return [_expand_abs(p, base_dir) for p in env_file]
        if isinstance(env_file, str):
            return _expand_abs(env_file, base_dir)
        return env_file

    def _parse_short_volume(vol: str, base_dir: str) -> str:
        """
        Convert short syntax "host:container[:mode]" where host may be relative.
        We only absolutize the host side if it looks like a path (has /, ./, ../, or startswith ~).
        """
        # Split only first two colons, so container[:mode] stays together
        parts = vol.split(":", 2)
        if len(parts) == 1:
            # anonymous volume or container-only path; nothing to change
            return vol

        host, rest1 = parts[0], parts[1]
        rest = [rest1] + (parts[2:] if len(parts) == 3 else [])

        # Heuristic: treat as filesystem path if it looks like one
        looks_like_path = (
            host.startswith("~")
            or host.startswith(".")
            or host.startswith("/")
            or (os.name == "nt" and len(host) >= 2 and host[1] == ":")  # Windows drive
        )
        if looks_like_path:
            host_abs = _expand_abs(host, base_dir)
            return ":".join([host_abs] + rest)
        return vol

    def _fix_volumes(volumes, base_dir):
        if isinstance(volumes, list):
            fixed = []
            for v in volumes:
                if isinstance(v, str):
                    fixed.append(_parse_short_volume(v, base_dir))
                elif isinstance(v, dict):
                    # long syntax
                    # Example:
                    # - type: bind
                    #   source: ./hostdir
                    #   target: /container
                    #   read_only: true
                    if v.get("type") == "bind" and "source" in v:
                        v["source"] = _expand_abs(v["source"], base_dir)
                    fixed.append(v)
                else:
                    fixed.append(v)
            return fixed
        return volumes

    def _fix_root_files(section_dict, base_dir):
        # For root-level configs/secrets entries with "file:"
        for _, entry in section_dict.items():
            if isinstance(entry, dict) and "file" in entry:
                entry["file"] = _expand_abs(entry["file"], base_dir)

    # -------- main body starts here --------
    compose_file_path = os.path.abspath(compose_file_path)
    base_dir = os.path.dirname(compose_file_path)
    backup_path = compose_file_path + ".bak"

    # Backup
    shutil.copy2(compose_file_path, backup_path)

    with open(compose_file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Compose file root must be a mapping.")

    # Compose v2 ignores/complains about the top-level version field; drop it.
    removed_version = False
    if "version" in data:
        data.pop("version", None)
        removed_version = True

    # services
    services = data.get("services", {})
    for _, svc in (services or {}).items():
        if not isinstance(svc, dict):
            continue

        # build
        if "build" in svc:
            svc["build"] = _fix_build(svc["build"], base_dir)

        # volumes
        if "volumes" in svc:
            svc["volumes"] = _fix_volumes(svc["volumes"], base_dir)

        # env_file
        if "env_file" in svc:
            svc["env_file"] = _fix_env_file(svc["env_file"], base_dir)

    # root-level configs/secrets
    if isinstance(data.get("configs"), dict):
        _fix_root_files(data["configs"], base_dir)
    if isinstance(data.get("secrets"), dict):
        _fix_root_files(data["secrets"], base_dir)

    # Strip external: true from any top-level resources (networks, volumes, etc.)
    for section_name in ("networks", "volumes", "configs", "secrets"):
        section = data.get(section_name)
        if isinstance(section, dict):
            for _, entry in section.items():
                if isinstance(entry, dict) and entry.get("external") is True:
                    entry.pop("external", None)

    # Write back (overwrite)
    with open(compose_file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    print(f"📂 Backup created: {backup_path}")
    print(f"✅ Updated in place: {compose_file_path}")
    if removed_version:
        print("⚠️  Removed top-level 'version' key for Compose v2 compatibility")
