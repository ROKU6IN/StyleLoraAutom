"""Interactive Windows picker acceptance. Select only the printed fixture paths."""
import json
from pathlib import Path
import threading
import base64
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from stylelora.runtime.picker import NativePicker


def main():
    base = Path(__file__).resolve().parents[1]/'.test-tmp/native-picker'/uuid4().hex[:12]
    base.mkdir(parents=True, exist_ok=True)
    project = base/'原生 选择.slproj'
    inputs = base/'图片 输入'; inputs.mkdir(exist_ok=True)
    results = []
    picker = NativePicker(None, enabled=True)
    original = subprocess.Popen
    def traced(args, **kwargs):
        script = base64.b64decode(args[-1]).decode('utf-16-le')
        # Preselect only this disposable fixture so the human can confirm the
        # native control without navigating a long path. Selection still goes
        # through the production modal and its returned value is checked below.
        initial = str(expected or inputs).replace("'", "''")
        if purpose == 'project_create':
            parent = str(base).replace("'", "''")
            setup = f"$dialog.InitialDirectory = '{parent}'\n$dialog.FileName = '{project.name}'\n"
        else:
            setup = f"$dialog.SelectedPath = '{initial}'\n"
        script = script.replace('$owner.Show()', setup+'$owner.Show()')
        if '--trace' in sys.argv:
            target = str(base/'trace.txt').replace("'", "''")
            for statement, label in [('Add-Type -AssemblyName System.Windows.Forms', 'assembly'),
                                     ('$owner.Show()', 'owner'),
                                     ('if ($dialog.ShowDialog($owner)', 'dialog')]:
                marker = "[System.IO.File]::AppendAllText('"+target+"', '"+label+"'+[Environment]::NewLine)\n"
                script = script.replace(statement, marker+statement)
        args = [*args[:-1], base64.b64encode(script.encode('utf-16-le')).decode()]
        return original(args, **kwargs)
    for purpose, expected in [('project_create', project), ('project_open', project),
                              ('import_root', inputs), ('import_root', None)]:
        print(json.dumps({'purpose': purpose, 'select': str(expected) if expected else 'CANCEL'}, ensure_ascii=False), flush=True)
        with patch('stylelora.runtime.picker.subprocess.Popen', side_effect=traced):
            selected = picker.choose(purpose, threading.Event())
        passed = selected is None if expected is None else selected is not None and Path(selected).resolve() == expected.resolve()
        result = {'purpose': purpose, 'canceled': expected is None, 'selected': selected, 'passed': passed}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        (base/'result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        if not passed:
            raise RuntimeError('Picker result did not match the fixture')
        if purpose == 'project_create':
            project.mkdir(exist_ok=True)
    print('All four native chooser checks passed', flush=True)


if __name__ == '__main__':
    main()
