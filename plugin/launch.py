"""KiCad IPC eklenti giris noktasi. KiCad bunu eklentinin Python ortaminda
(venv) calistirir; pencereyi ayni ortamin pythonw'si ile ayri surec olarak
acip hemen cikar, boylece KiCad beklemez."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(pyw):
    pyw = sys.executable

subprocess.Popen([pyw, os.path.join(HERE, "run.pyw")], cwd=HERE, close_fds=True,
                 creationflags=0x00000008 | 0x00000200 | 0x08000000)  # DETACHED | NEW_GROUP | NO_WINDOW
