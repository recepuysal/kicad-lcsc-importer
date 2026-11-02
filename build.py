"""KiCad Eklenti ve Icerik Yoneticisi (PCM) icin kurulabilir zip uretir:
dist/kicad-lcsc-importer-<surum>.zip  ->  metadata.json, plugins/..., resources/icon.png"""
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
meta = json.load(open(os.path.join(ROOT, "pcm", "metadata.json"), encoding="utf-8"))
ver = meta["versions"][0]["version"]
os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
out = os.path.join(ROOT, "dist", "kicad-lcsc-importer-%s.zip" % ver)

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(ROOT, "pcm", "metadata.json"), "metadata.json")
    z.write(os.path.join(ROOT, "pcm", "resources", "icon.png"), "resources/icon.png")
    src = os.path.join(ROOT, "plugin")
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            p = os.path.join(d, f)
            z.write(p, "plugins/" + os.path.relpath(p, src).replace(os.sep, "/"))
print(out)
