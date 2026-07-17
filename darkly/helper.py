"""Helper subprocess entry point - the serve/encode pipeline, `bpy`-free.

Launched by `bridge.HelperProcess` with Blender's bundled Python
(`sys.executable helper.py <argv>`), talking to the Blender process over its own
stdin (frames in) and stdout (events out). Running the HTTP server and the PNG
encode here, in a child, is what lets the shipped extension import **zero**
`threading` and `queue` in the Blender process - the reviewer's hard constraint.

On the `run_in_executor` threads: the encode (~30 ms, CPU-bound) and the
blocking stdin reads run via `loop.run_in_executor(None, ...)` - asyncio's own
thread pool - so heartbeats and frame intake stay live while a frame encodes.
That executor *is* threads, but here in the child, where they cannot crash
Blender (the reviewer's stated concern), and the shipped **source** still has no
`threading` or `queue` import of its own (asyncio using the stdlib internally is
not the extension importing it). Everything the extension writes is
single-threaded asyncio.

Side benefit: if Blender crashes or is killed, this process sees EOF on stdin
and exits, ending every HTTP response, so the port always comes back.

Import pattern: runnable as a script, so `from . import ...` fails and falls
back to top-level imports of the sibling modules (the `encode.py` precedent) -
the launcher puts this file's directory on `sys.path`.
"""

import argparse
import asyncio
import json
import os
import struct
import sys

import numpy as np

try:  # package context; as a script the sibling dir is on sys.path
    from . import server, encode, colormanage
except ImportError:
    import server
    import encode
    import colormanage


def _parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--compression", type=int, default=1)
    parser.add_argument("--heartbeat", type=float, default=server.HEARTBEAT_INTERVAL)
    parser.add_argument("--ocio", default=None)
    return parser.parse_args(argv)


def _read_exactly(stream, n):
    """Blocking read of exactly `n` bytes from `stream`; `None` on EOF. Runs in
    an executor so the loop stays live while it waits for the next frame."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


class _Emitter:
    """Writes newline-delimited JSON events to the private event fd (the child's
    original stdout, duped aside before stdout was redirected to stderr)."""

    def __init__(self, fd):
        self._fd = fd

    def emit(self, obj):
        try:
            os.write(self._fd, (json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass  # parent gone; EOF on stdin will drive our shutdown

    def fatal(self, exc):
        """Containment boundary: report and hard-exit. `os._exit` skips
        `asyncio.run`'s shutdown, which would otherwise wait on a still-pending
        blocking stdin read in the executor (non-daemon) until the parent's
        terminate() fallback fires."""
        self.emit({"event": "fatal", "error": str(exc) or type(exc).__name__})
        try:
            os.fsync(self._fd)
        except OSError:
            pass
        os._exit(1)


async def _stdin_reader(loop, stdin, latest, frame_ready, stop, emitter):
    """Read length-prefixed frames from stdin into the single latest slot. EOF
    (parent closed stdin, or Blender died) initiates a clean shutdown."""
    try:
        while True:
            head = await loop.run_in_executor(None, _read_exactly, stdin, 4)
            if head is None:
                break
            (header_len,) = struct.unpack(">I", head)
            header_bytes = await loop.run_in_executor(None, _read_exactly, stdin, header_len)
            if header_bytes is None:
                break
            header = json.loads(header_bytes)
            payload = await loop.run_in_executor(None, _read_exactly, stdin, header["size"])
            if payload is None:
                break
            dtype = np.float32 if header["dtype"] == "f4" else np.uint8
            rgba = np.frombuffer(payload, dtype=dtype).reshape(
                header["height"], header["width"], 4
            )
            view = header["view"]
            view_settings = colormanage.ViewSettings(**view) if view is not None else None
            latest[0] = (header["width"], header["height"], rgba, view_settings)
            frame_ready.set()
    except Exception as exc:  # noqa: BLE001 - unexpected reader failure is fatal
        emitter.fatal(exc)
        return
    stop.set()


async def _encoder_loop(loop, encoder, hub, latest, frame_ready, stop, emitter):
    """Drain the latest captured frame, encode it off the loop, and publish it.
    Sequential (one temp file per encoder). Per-frame errors are logged and
    skipped, as the old worker did; anything past that guard is fatal."""
    try:
        while not stop.is_set():
            await frame_ready.wait()
            frame_ready.clear()
            if stop.is_set():
                break
            item = latest[0]
            latest[0] = None
            if item is None:
                continue
            width, height, rgba, view_settings = item
            try:
                png = await loop.run_in_executor(
                    None, encoder.encode, width, height, rgba, view_settings
                )
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the helper
                print(f"darkly helper: encode error: {exc}", file=sys.stderr, flush=True)
                emitter.emit({"event": "encode_error", "error": str(exc)})
                continue
            await hub.publish(png)
    except Exception as exc:  # noqa: BLE001 - anything past the per-frame guard
        emitter.fatal(exc)


async def main(argv):
    args = _parse_args(argv)

    # Claim the event channel before anything can write to stdout: dup the real
    # stdout aside as the private event fd, then point fd 1 at stderr so a stray
    # write (an OIIO/OCIO warning, a `warnings` emission, a future `print`)
    # lands on stderr instead of corrupting the event stream.
    event_fd = os.dup(1)
    os.dup2(2, 1)
    emitter = _Emitter(event_fd)

    stop = asyncio.Event()
    hub = server.FrameHub(
        on_client_count=lambda count: emitter.emit({"event": "clients", "count": count})
    )
    try:
        srv = await server.start_server(
            args.host, args.port, hub, heartbeat_interval=args.heartbeat
        )
    except OSError as exc:
        emitter.emit({"event": "bind_error", "error": str(exc)})
        return
    emitter.emit({"event": "bound"})

    encoder = encode.FrameEncoder(compression=args.compression, ocio_config_path=args.ocio)
    loop = asyncio.get_event_loop()
    latest = [None]  # single latest-frame slot
    frame_ready = asyncio.Event()

    reader_task = asyncio.create_task(
        _stdin_reader(loop, sys.stdin.buffer, latest, frame_ready, stop, emitter)
    )
    encoder_task = asyncio.create_task(
        _encoder_loop(loop, encoder, hub, latest, frame_ready, stop, emitter)
    )

    await stop.wait()

    # Clean EOF shutdown - no hammer needed. EOF already unblocked the reader,
    # so no executor read is stuck; end the HTTP responses and drain the tasks.
    await srv.aclose()
    frame_ready.set()  # wake the encoder so it observes `stop`
    reader_task.cancel()
    await asyncio.gather(reader_task, encoder_task, return_exceptions=True)
    encoder.free()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
