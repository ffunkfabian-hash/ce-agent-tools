"""Nonblocking Blender UI; the open scene is never changed or saved."""
import os
from pathlib import Path
import shutil
import textwrap

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from .jobs import Job, current_asset, prepare_request

# Approximate UI character width and icon/padding reserve; Blender reports physical region pixels.
_LABEL_CHARACTER_WIDTH = 7
_LABEL_PADDING_CHARACTERS = 6

_job = None
_last_result = None
_last_directory = ""
_registered = False


def _default_tools():
    return str(Path(__file__).resolve().parents[3] / "plugins" / "ce-shared" / "tools")


def _default_cache():
    base = Path.home() / "Library" / "Caches" if os.sys.platform == "darwin" else Path.home() / ".cache"
    return str(base / "CubeEscape" / "blender-tools")


class CEBlenderSettings(bpy.types.PropertyGroup):
    tools_root: StringProperty(name="ce-shared/tools", subtype="DIR_PATH", default=_default_tools())
    python: StringProperty(name="Python 3", subtype="FILE_PATH", default=shutil.which("python3") or "")
    cache: StringProperty(name="Ausgabeablage", subtype="DIR_PATH", default=_default_cache())
    source: EnumProperty(name="Eingabe", items=[("CURRENT", "Aktuelle gespeicherte Datei", "Nur gespeichert und ohne Änderungen"),
                                               ("FILE", "Datei vom Datenträger", "Ausdrücklich den gespeicherten Stand prüfen")])
    asset: StringProperty(name="Assetdatei", subtype="FILE_PATH")
    preflight_profile: StringProperty(name="Rig-/Preflight-Profil", subtype="FILE_PATH")
    turnaround_profile: StringProperty(name="Turnaround-Profil", subtype="FILE_PATH")
    frames: IntProperty(name="Bilder", default=4, min=1, max=360)
    resolution: IntProperty(name="Auflösung", default=256, min=16, max=8192)
    clip: BoolProperty(name="MP4 zusätzlich", default=False)
    fps: IntProperty(name="MP4-Bildrate", default=12, min=1, max=60)
    status: StringProperty(default="Bereit")


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _poll():
    global _job, _last_result, _last_directory
    if _job is None:
        return None
    result = _job.poll()
    if result is None:
        return 0.25
    _last_result = result
    _last_directory = str(_job.directory)
    _job = None
    settings = getattr(bpy.context.window_manager, "ce_blender_tools", None)
    if _registered and settings is not None:
        settings.status = result["summary"]
    _redraw()
    return None


class CE_OT_run_tool(bpy.types.Operator):
    bl_idname = "ce_tools.run"
    bl_label = "Werkzeug starten"
    kind: EnumProperty(items=[(key, key, "") for key in ("profile_preflight", "profile_turnaround", "preflight", "turnaround")])

    @classmethod
    def poll(cls, context):
        return _job is None

    def execute(self, context):
        global _job, _last_result, _last_directory
        settings = context.window_manager.ce_blender_tools
        try:
            asset = ""
            if not self.kind.startswith("profile_"):
                asset = current_asset(bpy.data.filepath, bpy.data.is_saved, bpy.data.is_dirty) if settings.source == "CURRENT" else bpy.path.abspath(settings.asset)
            profile = settings.preflight_profile if "preflight" in self.kind else settings.turnaround_profile
            python = bpy.path.abspath(settings.python)
            if Path(python).resolve() == Path(bpy.app.binary_path).resolve():
                raise ValueError("Python 3 muss ein externer Python-Interpreter sein, nicht Blender.")
            command, directory = prepare_request(tools_root=bpy.path.abspath(settings.tools_root), python=python,
                blender=bpy.app.binary_path, cache=bpy.path.abspath(settings.cache), kind=self.kind,
                profile=bpy.path.abspath(profile), asset=asset, frames=settings.frames,
                resolution=settings.resolution, clip=settings.clip, fps=settings.fps)
            _job = Job(command, directory)
            _last_result = None
            _last_directory = str(directory)
            settings.status = "Läuft / wartet ggf. auf Blender-Sperre …"
            bpy.app.timers.register(_poll, first_interval=0.25, persistent=True)
            return {"FINISHED"}
        except (OSError, ValueError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class CE_OT_cancel_tool(bpy.types.Operator):
    bl_idname = "ce_tools.cancel"
    bl_label = "Auftrag abbrechen"

    @classmethod
    def poll(cls, context):
        return _job is not None and not _job.cancelling

    def execute(self, context):
        _job.cancel()
        context.window_manager.ce_blender_tools.status = "Abbruch läuft; warte auf Prozessgruppe …"
        return {"FINISHED"}


class CE_OT_open_result(bpy.types.Operator):
    bl_idname = "ce_tools.open_result"
    bl_label = "Ausgabeordner öffnen"

    def execute(self, context):
        if _last_directory and Path(_last_directory).is_dir():
            bpy.ops.wm.path_open(filepath=_last_directory)
            return {"FINISHED"}
        return {"CANCELLED"}


class CE_PT_tools(bpy.types.Panel):
    bl_label = "Cube Escape Werkzeuge"
    bl_idname = "CE_PT_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CE Tools"

    def draw(self, context):
        settings = context.window_manager.ce_blender_tools
        layout = self.layout
        width = max(12, int(context.region.width / (_LABEL_CHARACTER_WIDTH * context.preferences.system.ui_scale)) - _LABEL_PADDING_CHARACTERS)

        def note(target, text, icon="INFO"):
            for line in textwrap.wrap(text, width=width):
                target.label(text=line, icon=icon)

        controls = layout.column()
        controls.enabled = _job is None
        config = controls.box()
        config.label(text="Lokale Werkzeuge")
        for key in ("tools_root", "python", "cache"):
            config.prop(settings, key)
        controls.prop(settings, "source")
        if settings.source == "FILE":
            controls.prop(settings, "asset")
            note(controls, "Geprüft wird der Stand auf dem Datenträger.")
        elif not bpy.data.is_saved:
            note(controls, "Ungespeichert: selbst speichern oder Datei wählen.", "ERROR")
        elif bpy.data.is_dirty:
            note(controls, "Änderungen offen: selbst speichern oder Datei wählen.", "ERROR")
        box = controls.box()
        box.label(text="Rig / Preflight")
        box.prop(settings, "preflight_profile")
        row = box.row()
        row.operator("ce_tools.run", text="Profil prüfen").kind = "profile_preflight"
        row.operator("ce_tools.run", text="Datei prüfen").kind = "preflight"
        box = controls.box()
        box.label(text="Turnaround")
        box.prop(settings, "turnaround_profile")
        box.operator("ce_tools.run", text="Profil prüfen").kind = "profile_turnaround"
        row = box.row()
        row.prop(settings, "frames")
        row.prop(settings, "resolution")
        box.prop(settings, "clip")
        if settings.clip:
            box.prop(settings, "fps")
        box.operator("ce_tools.run", text="Vorschau rendern").kind = "turnaround"
        for line in textwrap.wrap(settings.status, width=width) or [""]:
            layout.label(text=line, icon="TIME" if _job else "INFO")
        if _job:
            layout.operator("ce_tools.cancel")
        if _last_result:
            for issue in _last_result.get("issues", [])[:12]:
                for line in textwrap.wrap(str(issue), width=width):
                    layout.label(text=line, icon="ERROR")
            if len(_last_result.get("issues", [])) > 12:
                layout.label(text="Weitere Befunde im JSON-Bericht.")
        if _last_directory:
            layout.operator("ce_tools.open_result")
        note(layout, "Die geöffnete Szene wird nicht gespeichert.")


_CLASSES = (CEBlenderSettings, CE_OT_run_tool, CE_OT_cancel_tool, CE_OT_open_result, CE_PT_tools)


def register():
    global _registered
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.ce_blender_tools = PointerProperty(type=CEBlenderSettings)
    _registered = True
    if _job is not None:
        bpy.context.window_manager.ce_blender_tools.status = "Abbruch läuft; warte auf Prozessgruppe …" if _job.cancelling else "Auftrag läuft …"


def unregister():
    global _registered
    _registered = False
    if _job is not None:
        _job.cancel()
        # Keep the timer alive without touching the unregistered RNA classes.
        # Re-enabling the add-on cannot launch a job until the supervisor exits.
        if not bpy.app.timers.is_registered(_poll):
            bpy.app.timers.register(_poll, first_interval=0.25, persistent=True)
    elif bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    if hasattr(bpy.types.WindowManager, "ce_blender_tools"):
        del bpy.types.WindowManager.ce_blender_tools
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
