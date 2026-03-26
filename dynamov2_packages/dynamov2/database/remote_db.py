import inspect
import os
from typing import Any, Dict, Optional

import requests
from types import SimpleNamespace

from .db_helper import db_helper


DEFAULT_BASE_URL = "http://192.168.15.60:8000"


def _build_params(target_func, args, kwargs) -> Dict[str, Any]:
    """Bind args/kwargs to the target function signature to produce a param dict."""
    sig = inspect.signature(target_func)
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    params = dict(bound.arguments)
    # Flatten **kwargs entries (they appear under their var-keyword parameter name, e.g., "kwargs").
    for name, param in sig.parameters.items():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            extra = params.pop(name, {}) or {}
            if isinstance(extra, dict):
                params.update(extra)
    return params


def call_db_api(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Invoke the FastAPI /db endpoint with the given db_helper method and params.

    Args:
        method: Name of the db_helper method to call (e.g., "get_github_repository").
        params: Dict of parameters to pass to the method.
        base_url: Base URL of the FastAPI server (defaults to env DB_API_BASE_URL or localhost).
        api_key: Optional X-API-Key header value.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        requests.HTTPError if the response status is not OK.
    """
    params = params or {}
    base = (base_url or os.getenv("DB_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/db"

    headers = {"Content-Type": "application/json"}
    api_key = api_key or os.getenv("DB_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key

    response = requests.post(url, json={"method": method, "params": params}, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _coerce_remote(obj: Any) -> Any:
    """Convert nested dicts to SimpleNamespace so attribute access works like ORM rows."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _coerce_remote(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_coerce_remote(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_coerce_remote(v) for v in obj)
    if isinstance(obj, set):
        return {_coerce_remote(v) for v in obj}
    return obj


class RemoteDBHelper:
    """
    Proxy that mirrors db_helper methods and forwards calls via the FastAPI /db endpoint.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 30):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def __getattr__(self, name: str):
        target = getattr(db_helper, name, None)
        if not callable(target):
            raise AttributeError(f"db_helper has no callable '{name}'")

        def _wrapper(*args: Any, **kwargs: Any):
            params = _build_params(target, args, kwargs)
            resp = call_db_api(
                method=name,
                params=params,
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
            # Unwrap the "result" payload for convenience when available.
            if isinstance(resp, dict) and "result" in resp:
                # Raise if server signaled an error state to avoid silent misuse.
                if resp.get("state") not in (None, "ok"):
                    raise RuntimeError(f"db api error: {resp}")
                return _coerce_remote(resp["result"])
            return _coerce_remote(resp)

        _wrapper.__name__ = name
        _wrapper.__doc__ = getattr(target, "__doc__", None)
        return _wrapper


def get_remote_db_helper(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> RemoteDBHelper:
    """
    Convenience constructor for a RemoteDBHelper proxy.
    """
    return RemoteDBHelper(base_url=base_url, api_key=api_key, timeout=timeout)
