"""GPU readback `Buffer` -> numpy, isolated from `bpy` so it is unit-testable.

This module exists to enforce one subtlety that produced a whole-frame corruption
bug: a `gpu.types.Buffer` returned by `framebuffer.read_color` is
*multi-dimensional* (height, width, channels), and converting it straight to
numpy misreads its per-row strides on some Blender/numpy builds. The raw Buffer
is correct (indexing it yields the right pixels) but `np.array(buffer)` mangles
it into striped garbage. Flattening the Buffer to 1D first - exactly what
Blender's own offscreen readback does (`export_uv_png.py`) - makes the byte
layout unambiguous; we then impose the real shape in numpy.

Regression-tested in `tests/test_readback.py`.
"""

import numpy as np


def buffer_to_pixels(buffer, width, height, dtype=np.uint8):
    """Copy a `(height, width, 4)` gpu readback `Buffer` into a numpy array.

    `dtype` matches the `read_color` format: `np.uint8` for `'UBYTE'` reads,
    `np.float32` for `'FLOAT'` reads.

    Flattens the Buffer to 1D before the numpy conversion (the multi-dimensional
    conversion is the fragile path), then reshapes. `buffer` need only support
    `.dimensions =` and the numpy array/buffer protocol - both of which the real
    `gpu.types.Buffer` provides."""
    buffer.dimensions = width * height * 4
    return np.array(buffer, dtype=dtype).reshape(height, width, 4)
