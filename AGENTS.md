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

## Releases

A green CI run on a master push publishes to two targets, each self-gated on its own state, so they publish and catch up independently. To ship: bump `version` in `darkly/blender_manifest.toml` and push to master. Pushes without a bump build but publish nothing new.

- **GitHub release** `v<version>` (with the built zip attached) is created unless that tag already exists.
- **Blender Extensions Platform** (extensions.blender.org) receives the version via its [REST API](https://developer.blender.org/docs/features/extensions/ci_cd/), gated on the platform's own currently published version — not on the GitHub release. So a run that failed or ran before the token existed is retried on the next push, rather than skipped forever because the git tag already exists. It needs a `BLENDER_EXTENSIONS_TOKEN` repository secret (generate one at extensions.blender.org/settings/tokens); the step skips cleanly when the secret is absent. It reuses the GitHub release notes as the marketplace release notes.

Prerequisites for the Blender publish: the extension must already be registered on the platform (the first submission is manual and goes through moderation), and the manifest `id` must match its platform slug. Caveat: the platform listing only reflects *approved* versions, so re-pushing master while a freshly uploaded version is still awaiting moderation attempts a duplicate upload and fails until that version is approved.

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
