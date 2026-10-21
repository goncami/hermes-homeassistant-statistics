import importlib.util
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("homeassistant_statistics", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

@pytest.fixture(autouse=True)
def isolated_secrets():
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    token = set_secret_scope({})
    try:
        yield
    finally:
        reset_secret_scope(token)


class _RepoRootIsNotATestPackage:
    """Collect the root as a directory so pytest doesn't break its relative imports.

    This must be a global plugin: the root collector uses its parent's hooks,
    which cannot see a conftest inside the root.
    """

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("statistics-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "statistics-repo-root")
