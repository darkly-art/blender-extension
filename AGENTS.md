# Darkly, agent guide

Blender extension (`darkly/`) that streams the viewport or a camera view to Darkly over a localhost HTTP server. User-facing docs live in [README.md](README.md).

## Run this after any change

CI (`.github/workflows/ci.yml`) runs exactly these on every PR and master push. A master push that skips them and fails validation blocks the release job.

```bash
# Unit tests (no Blender needed; needs numpy, Pillow, OpenImageIO from pip)
python3 -m unittest discover -s tests -v

# Manifest validation + extension build (needs blender 5.1+ on PATH)
blender --command extension validate darkly
mkdir -p dist && blender --command extension build --source-dir darkly --output-dir dist
```

## Known CI breakers

- Blender's manifest spec caps several string fields at 64 characters, including `tagline` and each `[permissions]` value. Both have broken CI already. `extension validate` catches this locally.
- There is deliberately no `[permissions]` table: per the spec, `network` means internet access, and this extension only listens on localhost / the local network. Do not add it back.

## Releases

A green CI run on a master push publishes a GitHub release tagged `v<version>` from the manifest, with the built zip attached, unless that tag already exists. To ship: bump `version` in `darkly/blender_manifest.toml` and push to master. Pushes without a bump build but skip the release.

## Headless smoke test

Registers the add-on from this source tree inside Blender. `--factory-startup` is required if the extension is also installed in your Blender, or class registration collides.

```bash
blender --background --factory-startup --python-expr "
import sys; sys.path.insert(0, '.')
import darkly
darkly.register()
import bpy
print(darkly.start_stream(bpy.context.scene))
darkly.unregister()"
```

## Conventions

- The extension bundles nothing. Only libraries Blender ships (numpy, OpenImageIO, PyOpenColorIO) plus the stdlib.
- Everything off the main thread (`server.py`, `encode.py`, `readback.py`, `colormanage.py`) must stay `bpy`-free, and is unit-tested that way.
- Logging follows Blender's own convention: bare `logging.getLogger(__name__)` per module, no handlers, no config. Warnings and errors for diagnostics, `self.report()` for operator results. `print()` only for console output the user explicitly asked for (the Profile toggle, Benchmark).
- Docs are practical: no marketing filler, no implementation bragging, no em dashes.
