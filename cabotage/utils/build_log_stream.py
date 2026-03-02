# used for local buildkit emulation only
import subprocess  # nosec

import redis


_LOG_STREAM_TTL = 3600  # 1 hour
_HEARTBEAT_TTL = 90  # seconds


def stream_key(build_type, build_job_id):
    return f"buildlog:{build_type}:{build_job_id}"


def publish_log_line(redis_client, key, line):
    redis_client.xadd(key, {"line": line})


def publish_end(redis_client, key, error=False):
    redis_client.xadd(key, {"line": "__END__", "error": "1" if error else "0"})
    redis_client.expire(key, _LOG_STREAM_TTL)


def read_log_stream(redis_client, key, timeout_ms=5000):
    last_id = "0-0"
    while True:
        results = redis_client.xread({key: last_id}, count=100, block=timeout_ms)
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


def heartbeat_key(entity_type, entity_id):
    return f"heartbeat:{entity_type}:{entity_id}"


def refresh_heartbeat(redis_client, entity_type, entity_id, ttl=None):
    key = heartbeat_key(entity_type, entity_id)
    redis_client.set(key, "1", ex=ttl or _HEARTBEAT_TTL)


def get_redis_client(broker_url):
    if isinstance(broker_url, (tuple, list)):
        broker_url = broker_url[0]
    return redis.Redis.from_url(broker_url)


def run_and_stream(
    command,
    env,
    cwd,
    broker_url,
    build_type,
    build_job_id,
    heartbeat_type=None,
    heartbeat_id=None,
):
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
    for line in proc.stdout:
        line = line.rstrip("\n")
        publish_log_line(redis_client, log_key, line)
        output_lines.append(line)
        if heartbeat_type and heartbeat_id:
            refresh_heartbeat(redis_client, heartbeat_type, heartbeat_id)
    proc.wait()

    if proc.returncode != 0:
        publish_end(redis_client, log_key, error=True)
        raise subprocess.CalledProcessError(
            proc.returncode, command, output="\n".join(output_lines)
        )

    publish_end(redis_client, log_key)
    return "\n".join(output_lines)
