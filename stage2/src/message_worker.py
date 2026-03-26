#!/usr/bin/env python3
"""RabbitMQ worker that exposes consume/publish helpers via a class."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pika
from pika.adapters.blocking_connection import BlockingChannel

_REQUIRE_QUEUE = "require_processing"
_COMPLETE_QUEUE = "processing_complete"


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
    channel.queue_declare(queue=_REQUIRE_QUEUE, durable=True)
    channel.queue_declare(queue=_COMPLETE_QUEUE, durable=True)


def _decode_message(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return body.decode("utf-8")


def process_message(message: Any) -> Any:
    """Stub processing that annotates the message."""
    return {"original": message, "processed": True}


def _parse_json_argument(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"Invalid JSON payload: {exc}") from exc


class MessageWorker:
    """Abstraction for consuming tasks and publishing results."""

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
        assert self._channel is not None
        return self._channel

    def consume_task(self) -> Any | None:
        """Return the next task from require_processing, if available."""
        method_frame, _, body = self.channel.basic_get(_REQUIRE_QUEUE, auto_ack=True)
        if not method_frame:
            return None
        return _decode_message(body)

    def publish_result(self, payload: Any) -> None:
        """Publish a message to processing_complete."""
        body = json.dumps(payload).encode("utf-8")
        self.channel.basic_publish(
            exchange="",
            routing_key=_COMPLETE_QUEUE,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
            ),
        )

    def run(self, processor: Callable[[Any], Any] = process_message) -> None:
        """Continuously consume tasks and publish processed results."""

        channel = self.channel

        def _handle_message(
            ch: BlockingChannel,
            method: Any,
            properties: pika.BasicProperties,
            body: bytes,
        ) -> None:
            message = _decode_message(body)
            try:
                result = processor(message)
            except Exception as exc:  # pragma: no cover - user-defined processing may fail
                print(f"Failed to process message {message!r}: {exc}")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            body_bytes = json.dumps(result).encode("utf-8")
            ch.basic_publish(
                exchange="",
                routing_key=_COMPLETE_QUEUE,
                body=body_bytes,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                ),
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            print(f"Processed message: {message!r}")

        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=_REQUIRE_QUEUE, on_message_callback=_handle_message)
        print(
            f"Worker consuming messages from '{_REQUIRE_QUEUE}' "
            f"and publishing to '{_COMPLETE_QUEUE}'."
        )
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            print("Worker interrupted, shutting down...")
        finally:
            if channel.is_open:
                channel.stop_consuming()

    def close(self) -> None:
        if self._connection and self._connection.is_open:
            self._connection.close()
        self._channel = None
        self._connection = None

    def __enter__(self) -> "MessageWorker":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Interact with the RabbitMQ worker queues")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file containing RabbitMQ settings",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "consume-task", help="Consume a single task from the require_processing queue"
    )

    publish_parser = subcommands.add_parser(
        "publish-result", help="Publish a result to the processing_complete queue"
    )
    publish_parser.add_argument(
        "payload",
        type=_parse_json_argument,
        help="JSON payload to publish",
    )

    subcommands.add_parser(
        "run", help="Continuously consume tasks and publish processed results"
    )
    args = parser.parse_args()

    with MessageWorker(env_file=args.env_file) as worker:
        if args.command == "consume-task":
            task = worker.consume_task()
            if task is None:
                print("Queue is empty")
            else:
                print(json.dumps(task, indent=2))
            return

        if args.command == "publish-result":
            worker.publish_result(args.payload)
            print("Published result to 'processing_complete'")
            return

        if args.command == "run":
            worker.run()
            return


if __name__ == "__main__":
    main()
