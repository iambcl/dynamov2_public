#!/usr/bin/env python3
"""RabbitMQ helper that hides queue names behind a simple class interface."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pika
from pika.adapters.blocking_connection import BlockingChannel

_REQUIRE_QUEUE = "require_processing"
_COMPLETE_QUEUE = "processing_complete"
_QUEUE_NAMES = (_REQUIRE_QUEUE, _COMPLETE_QUEUE)


@dataclass
class RabbitMQConfig:
    host: str
    port: int
    username: str
    password: str
    virtual_host: str

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "RabbitMQConfig":
        values = _load_env_values(env_file)
        missing = [key for key in _ENV_KEYS if key not in values]
        if missing:
            missing_str = ", ".join(missing)
            raise RuntimeError(
                f"Missing RabbitMQ settings in {env_file}: {missing_str}"
            )

        return cls(
            host=values["RABBITMQ_HOST"],
            port=int(values["RABBITMQ_PORT"]),
            username=values["RABBITMQ_USERNAME"],
            password=values["RABBITMQ_PASSWORD"],
            virtual_host=values["RABBITMQ_VHOST"],
        )


_ENV_KEYS = {
    "RABBITMQ_HOST",
    "RABBITMQ_PORT",
    "RABBITMQ_USERNAME",
    "RABBITMQ_PASSWORD",
    "RABBITMQ_VHOST",
}


def _load_env_values(env_file: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    path = Path(env_file)
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, _, value = stripped.partition("=")
            if not _:
                continue
            values[key.strip()] = value.strip()

    for key in _ENV_KEYS:
        if key not in values and key in os.environ:
            values[key] = os.environ[key]

    return values


def _connect(config: RabbitMQConfig) -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(config.username, config.password)
    parameters = pika.ConnectionParameters(
        host=config.host,
        port=config.port,
        virtual_host=config.virtual_host,
        credentials=credentials,
    )
    return pika.BlockingConnection(parameters)


def _declare_queues(channel: BlockingChannel) -> None:
    for name in _QUEUE_NAMES:
        channel.queue_declare(queue=name, durable=True)


def _decode_message(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return body.decode("utf-8")


class MessageQueueServer:
    """High-level helpers for the project queues."""

    def __init__(self, config: Optional[RabbitMQConfig] = None, *, env_file: str = ".env"):
        self._config = config or RabbitMQConfig.from_env(env_file)
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[BlockingChannel] = None

    def connect(self) -> None:
        if self._connection and self._connection.is_open:
            return
        self._connection = _connect(self._config)
        self._channel = self._connection.channel()
        _declare_queues(self._channel)

    @property
    def channel(self) -> BlockingChannel:
        self.connect()
        assert self._channel is not None  # narrow type for mypy
        return self._channel

    def publish_task(self, payload: Any) -> None:
        """Publish a message onto the require_processing queue."""
        body = json.dumps(payload).encode("utf-8")
        self.channel.basic_publish(
            exchange="",
            routing_key=_REQUIRE_QUEUE,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
            ),
        )

    def require_queue_length(self) -> int:
        """Return the current message count for the require_processing queue."""
        method = self.channel.queue_declare(queue=_REQUIRE_QUEUE, passive=True)
        return int(method.method.message_count)

    def consume_result(self) -> Any | None:
        """Fetch a single message from the processing_complete queue."""
        method_frame, _, body = self.channel.basic_get(_COMPLETE_QUEUE, auto_ack=True)
        if not method_frame:
            return None
        return _decode_message(body)

    def close(self) -> None:
        if self._connection and self._connection.is_open:
            self._connection.close()
        self._channel = None
        self._connection = None

    def __enter__(self) -> "MessageQueueServer":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401 - standard context signature
        self.close()


def _parse_json_argument(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"Invalid JSON payload: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interact with RabbitMQ queues")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file containing RabbitMQ settings",
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    publish_parser = subcommands.add_parser(
        "publish-task", help="Publish a message to the require_processing queue"
    )
    publish_parser.add_argument(
        "payload",
        type=_parse_json_argument,
        help="JSON payload to enqueue",
    )

    subcommands.add_parser(
        "consume-result",
        help="Consume a single message from the processing_complete queue",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    with MessageQueueServer(env_file=args.env_file) as server:
        if args.command == "publish-task":
            server.publish_task(args.payload)
            print("Enqueued message onto 'require_processing'")
            return

        if args.command == "consume-result":
            result = server.consume_result()
            if result is None:
                print("Queue is empty")
            else:
                print(json.dumps(result, indent=2))
            return


if __name__ == "__main__":
    main()
