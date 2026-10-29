"""Current-desktop directory selection; arbitrary paths never enter HTTP DTOs."""
import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from .errors import ServiceError


class NativePicker:
    def __init__(self, runtime, enabled=False):
        self.runtime = runtime
        self.executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.available = enabled and os.name == "nt" and self.executable.is_file()
        self._gate = threading.Lock()

    def select(self, purpose, project_id=None):
        if not self.available:
            raise ServiceError(501, "picker:unavailable")
        context = self.runtime.require(project_id) if purpose == "import_root" else None
        if purpose != "import_root" and project_id is not None:
            raise ServiceError(422, "path:invalid_project_binding")
        if not self._gate.acquire(blocking=False):
            raise ServiceError(409, "picker:busy")
        def work(task):
            path = self.choose(purpose, task.cancel)
            if path is None or task.cancel.is_set():
                task.cancel.set()
                return None
            return self.runtime.grant(path, purpose, project_id)
        try:
            return self.runtime.supervisor.submit("filesystem.pick", work, context=context, finalizer=self._gate.release)
        except BaseException:
            self._gate.release()
            raise

    def choose(self, purpose, cancel):
        if purpose == "project_create":
            selection = """$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = '新建 StyleLora 项目：选择位置并输入项目文件夹名称'
$dialog.Filter = 'StyleLora 项目 (*.slproj)|*.slproj'
$dialog.DefaultExt = 'slproj'
$dialog.AddExtension = $true
$dialog.OverwritePrompt = $false
$dialog.CheckFileExists = $false
$property = 'FileName'
"""
        else:
            title = "选择已有 .slproj 项目文件夹" if purpose == "project_open" else "选择图片与同名 txt 所在的文件夹"
            selection = f"$dialog = New-Object System.Windows.Forms.FolderBrowserDialog\n$dialog.Description = '{title}'\n$dialog.ShowNewFolderButton = $false\n$property = 'SelectedPath'"
        script = """$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Width = 1
$owner.Height = 1
try {
""" + selection + """
$owner.Show()
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    @{path = $dialog.$property} | ConvertTo-Json -Compress
} else { @{path = $null} | ConvertTo-Json -Compress }
} finally { if ($dialog) { $dialog.Dispose() }; $owner.Dispose() }
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # SW_SHOWNORMAL for the GUI; the console stays hidden.
        startup.lpDesktop = 'winsta0\\default'
        child = subprocess.Popen([str(self.executable), "-NoProfile", "-STA", "-EncodedCommand", encoded],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                                 startupinfo=startup)
        deadline = time.monotonic()+600
        try:
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    child.terminate()
                    child.communicate(timeout=5)
                    return None
                try:
                    # Drain both redirected streams while the modal is open.
                    # PowerShell startup CLIXML can otherwise fill stderr and
                    # block before the native dialog is shown.
                    output, _ = child.communicate(timeout=.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if child.returncode or len(output) > 65536:
                raise ServiceError(503, "picker:failed")
            return json.loads(output.decode("utf-8-sig"))["path"]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(503, "picker:failed") from exc
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
