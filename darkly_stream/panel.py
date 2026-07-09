"""View3D N-panel - Start/Stop plus the stream settings."""

import bpy


class DARKLY_PT_stream_panel(bpy.types.Panel):
    bl_label = "Darkly Stream"
    bl_idname = "DARKLY_PT_stream_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Darkly"

    def draw(self, context):
        # Imported lazily to avoid a circular import at module load (the package
        # __init__ imports this module).
        from . import is_running, status_text

        layout = self.layout
        props = context.scene.darkly_stream
        running = is_running()

        if running:
            layout.operator("darkly.stream_stop", icon="PAUSE")
        else:
            layout.operator("darkly.stream_start", icon="PLAY")

        box = layout.box()
        box.label(text=status_text(), icon="INFO")

        col = layout.column()
        col.enabled = not running  # settings are fixed while streaming
        col.prop(props, "source")
        if props.source == "CAMERA":
            col.prop(props, "camera")
        col.prop(props, "port")
        col.prop(props, "fps")
        col.prop(props, "compression")

        # Profiling can be toggled live while streaming.
        layout.prop(props, "profile")

        # Stream-independent profiling: times the pipeline directly and reports
        # to the status bar + console. Needs an open 3D viewport (and a camera,
        # for the camera source), but no server or client.
        layout.operator("darkly.stream_benchmark", icon="TIME")

        if running:
            url = f"http://localhost:{props.port}/stream"
            layout.label(text=url, icon="URL")
