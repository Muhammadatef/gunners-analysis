"""The analyst DAGs must parse cheaply.

Airflow's scheduler re-imports every DAG file on a loop. This project has 39
fixture DAGs plus the maintenance one, so a heavy import at module scope in any
file is paid over and over — and `dspy`, `mlflow` and `fastembed` are heavy
enough that `fastembed` would load an ONNX model on every parse.

The convention (documented in `airflow/DAGS.md` and followed by every existing
DAG) is that such imports live inside the `@task` bodies. This test enforces it
by reading the AST rather than by importing, so it runs without Airflow
installed and cannot itself be slow.
"""

import ast
import glob
import os

import pytest

DAGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'airflow', 'dags')

# Importing any of these at module scope costs real time on every scheduler
# parse. They belong inside task functions.
HEAVY = {'dspy', 'mlflow', 'fastembed', 'torch', 'sentence_transformers',
         'litellm', 'onnxruntime', 'transformers', 'matplotlib', 'mplsoccer',
         'selenium', 'pandas', 'numpy'}

AGENT_DAGS = sorted(glob.glob(os.path.join(DAGS_DIR, 'agent_*.py')))


def test_there_is_an_agent_dag_file():
    assert AGENT_DAGS, f'no agent DAG files under {DAGS_DIR}'


@pytest.mark.parametrize('path', AGENT_DAGS, ids=os.path.basename)
def test_the_dag_file_parses(path):
    with open(path) as fh:
        ast.parse(fh.read(), filename=path)


def _module_scope_imports(path) -> set[str]:
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in tree.body:                      # module scope only
        if isinstance(node, ast.Import):
            found |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split('.')[0])
    return found


@pytest.mark.parametrize('path', AGENT_DAGS, ids=os.path.basename)
def test_no_heavy_imports_at_module_scope(path):
    offenders = _module_scope_imports(path) & HEAVY
    assert not offenders, (
        f'{os.path.basename(path)} imports {sorted(offenders)} at module scope; '
        f'move them inside the @task bodies — the scheduler pays this on every '
        f'parse of every DAG file')


@pytest.mark.parametrize('path', AGENT_DAGS, ids=os.path.basename)
def test_every_dag_declares_an_explicit_schedule(path):
    """A DAG with no schedule argument inherits a default that is rarely what
    anyone meant. Manual DAGs must say `schedule=None` deliberately."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)

    dag_calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'dag']
    assert dag_calls, 'no @dag decorators found'
    for call in dag_calls:
        keywords = {kw.arg for kw in call.keywords}
        assert 'schedule' in keywords, 'a @dag is missing an explicit schedule'
        assert 'catchup' in keywords, 'a @dag is missing an explicit catchup'


@pytest.mark.parametrize('path', AGENT_DAGS, ids=os.path.basename)
def test_dag_ids_are_namespaced(path):
    """`agent_*` so they sort away from the 39 fixture DAGs in the UI."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'dag'):
            for kw in node.keywords:
                if kw.arg == 'dag_id' and isinstance(kw.value, ast.Constant):
                    assert kw.value.value.startswith('agent_'), kw.value.value
