# used for local buildkit emulation only
import subprocess  # nosec
from typing import TYPE_CHECKING, cast, Final
from collections.abc import Sequence

import redis


if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from datetime import timedelta
    from typing import Literal

    type BuildType = Literal["deploy", "image", "omnibus", "release"]
    type EntityType = Literal["image_build", "release_build", "omnibus_build", "deploy"]
    type LogLine = tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]

_LOG_STREAM_TTL: Final = 3600  # 1 hour
_HEARTBEAT_TTL: Final = 90  # seconds


def stream_key(build_type: BuildType, build_job_id: str) -> str:
    return f"buildlog:{build_type}:{build_job_id}"


def publish_log_line(redis_client: redis.Redis[bytes], key: str, line: str) -> None:
    redis_client.xadd(key, {"line": line})


def publish_end(
    redis_client: redis.Redis[bytes], key: str, error: bool = False
) -> None:
    redis_client.xadd(key, {"line": "__END__", "error": "1" if error else "0"})
    _ = redis_client.expire(key, _LOG_STREAM_TTL)


def read_log_stream(
    redis_client: redis.Redis[bytes], key: str, timeout_ms: int = 5000
) -> Generator[str | None]:
    last_id = "0-0"
    while True:
        results = cast(
            "list[LogLine]",
            redis_client.xread({key: last_id}, count=100, block=timeout_ms),
        )
        if not results:
            yield None  # timeout, caller can check if WS is still open
            continue
        for _stream_name, messages in results:
            for msg_id, fields in messages:
                last_id = msg_id
                line = fields.get(b"line", b"").decode()
                if line == "__END__":
                    return
                yield line


def heartbeat_key(entity_type: EntityType, entity_id: str) -> str:
    return f"heartbeat:{entity_type}:{entity_id}"


def refresh_heartbeat(
    redis_client: redis.Redis[bytes],
    entity_type: EntityType,
    entity_id: str,
    ttl: float | timedelta | None = None,
) -> None:
    key = heartbeat_key(entity_type, entity_id)
    _ = redis_client.set(key, "1", ex=ttl or _HEARTBEAT_TTL)


def get_redis_client(broker_url: str | Sequence[str]) -> redis.Redis[bytes]:
    broker_url = broker_url if isinstance(broker_url, str) else broker_url[0]
    return redis.Redis.from_url(broker_url)


def run_and_stream(
    command: list[str],
    env: dict[str, str],
    cwd: str,
    broker_url: str,
    build_type: BuildType,
    build_job_id: str,
    heartbeat_type: EntityType | None = None,
    heartbeat_id: str | None = None,
) -> str:
    """Run a subprocess, stream output to Redis, return accumulated output.

    Raises subprocess.CalledProcessError on non-zero exit.
    """
    redis_client = get_redis_client(broker_url)
    log_key = stream_key(build_type, build_job_id)

    cmd_line = " ".join(command)
    publish_log_line(redis_client, log_key, cmd_line)
    output_lines = [cmd_line]

    proc = subprocess.Popen(  # nosec - local buildkit emulation only
        command,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in cast("Iterable[str]", proc.stdout) or []:
        line = line.rstrip("\n")
        publish_log_line(redis_client, log_key, line)
        output_lines.append(line)
        if heartbeat_type and heartbeat_id:
            refresh_heartbeat(redis_client, heartbeat_type, heartbeat_id)
    _ = proc.wait()

    if proc.returncode != 0:
        publish_end(redis_client, log_key, error=True)
        raise subprocess.CalledProcessError(
            proc.returncode, command, output="\n".join(output_lines)
        )

    publish_end(redis_client, log_key)
    return "\n".join(output_lines)
