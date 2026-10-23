"""Optional Blender sidebar for existing ce-shared command-line tools."""
bl_info = {
    "name": "Cube Escape Werkzeuge",
    "author": "Cube Escape",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D-Ansicht > Seitenleiste > CE Tools",
    "description": "Vorhandene Preflight- und Turnaround-Werkzeuge in isolierten Prozessen starten",
    "category": "3D View",
}

from .addon import register, unregister
