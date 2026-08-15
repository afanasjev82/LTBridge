"""Guard: ``ltbridge.core`` imports without Litestar installed.

Task 12's P2/P3 mount contract requires `core/` to stay transport-free — no
server dependency anywhere under it. Poisoning `sys.modules['litestar']` in a
subprocess makes any accidental `import litestar` (direct or transitive) fail
loudly, rather than silently passing because the real package happened to
already be importable.
"""

import subprocess
import sys


def test_core_imports_without_litestar() -> None:
    code = (
        "import sys;"
        "sys.modules['litestar'] = None;"  # poison: any litestar import will fail loudly
        "import ltbridge.core.prompt, ltbridge.core.languages,"
        " ltbridge.core.token_budget, ltbridge.core.errors, ltbridge.core.formatting;"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr
