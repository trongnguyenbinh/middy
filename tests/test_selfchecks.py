"""The self-checks the modules already carry under `if __name__ == "__main__"`, run as tests (no model needed)."""
import os
import runpy

import pytest

from conftest import PROTO


@pytest.mark.parametrize("module", ["glossary", "blocks", "stamps", "mom_c", "ask", "speakers", "store", "ui_source_check"])
def test_module_selfcheck(module, capsys):
    runpy.run_path(os.path.join(PROTO, f"{module}.py"), run_name="__main__")
    assert "OK" in capsys.readouterr().out


def test_docx_template_selfcheck(capsys):
    import docx_mom
    docx_mom._selftest()
    assert "docx_mom self-check OK" in capsys.readouterr().out
