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
        from . import is_running, status_text, has_live_resources, has_failed

        layout = self.layout
        props = context.scene.darkly_stream
        running = is_running()

        # Stop stays reachable while *any* runtime resource is still held, not
        # just while healthy - after a failure (or a half-failed stop) it is
        # the recovery path that releases the port.
        if running or has_live_resources():
            layout.operator("darkly.stream_stop", icon="PAUSE")
        else:
            layout.operator("darkly.stream_start", icon="PLAY")

        box = layout.box()
        box.label(text=status_text(), icon="ERROR" if has_failed() else "INFO")

        col = layout.column()
        col.enabled = not running  # settings are fixed while streaming
        col.prop(props, "source")
        if props.source == "VIEWPORT":
            col.prop(props, "viewport")
        else:
            col.prop(props, "camera")
        col.prop(props, "port")
        col.prop(props, "listen_all")
        col.prop(props, "fps")
        col.prop(props, "compression")

        # Film transparency (a scene render setting) makes Rendered-mode output
        # transparent, so Darkly composites the 3D view over the canvas instead
        # of the world background - the transparent-background premise the whole
        # add-on is built on. Mirrored here for convenience (it otherwise lives
        # in Render Properties > Film) as a live toggle: it takes effect on the
        # next redraw, so it's safe to flip while streaming.
        layout.prop(context.scene.render, "film_transparent", text="Film Transparency")

        # Object Outline (a Solid-shading setting) is drawn by the workbench
        # engine straight into the captured scene colour buffer, in the theme
        # outline colour (black by default), baking black pixels into the
        # anti-aliased silhouette alpha edge - a dark fringe when Darkly
        # composites the stream. Turn it off for clean compositing. Mirrored
        # here (it otherwise lives in Viewport Shading > Options) as a live
        # toggle that takes effect on the next redraw. Blender keeps the
        # displayed workbench shading on the space normally, but on the scene's
        # display when the viewport is Rendered under BLENDER_WORKBENCH.
        shading = context.space_data.shading
        if shading.type == "RENDERED" and context.scene.render.engine == "BLENDER_WORKBENCH":
            shading = context.scene.display.shading
        layout.prop(shading, "show_object_outline", text="Object Outline")

        if running:
            url = f"http://localhost:{props.port}/stream"
            layout.label(text=url, icon="URL")
