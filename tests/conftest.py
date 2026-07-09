"""Global test configuration — ensure tests use known default settings."""

import atexit
import os
import shutil
import tempfile

# Override env vars before any module imports Settings, so tests
# don't pick up values from the user's .env file. Note that Settings also
# falls back to the pre-rename OCTOPUS_* names (docs/plans/rename-owlery.md),
# so a stale one in the developer's environment would otherwise leak in.
os.environ["OWLERY_AUTH_TOKEN"] = "changeme"
os.environ.pop("OCTOPUS_AUTH_TOKEN", None)

# Isolate per-agent state (the canonical memory dir each agent gets) under a
# throwaway temp root, so creating agents in tests never litters the
# developer's real ~/.owlery/agents (docs/plans/memory.md). Set before any
# import of Settings; cleaned at process exit. Per-test fixtures may still
# point agents_dir at their own tmp_path — that just overrides this default.
#
# `home_dir` is deliberately left at its `~/.owlery` default: the dirs derived
# from it expand `~` at USE time, which is what lets tests relocate the whole
# tree by monkeypatching $HOME.
_TEST_AGENTS_DIR = tempfile.mkdtemp(prefix="owlery-test-agents-")
os.environ["OWLERY_AGENTS_DIR"] = _TEST_AGENTS_DIR
atexit.register(lambda: shutil.rmtree(_TEST_AGENTS_DIR, ignore_errors=True))

# Same for deep-research reports: the research tests write real report files,
# which would otherwise land in the developer's ~/.owlery/research.
_TEST_RESEARCH_DIR = tempfile.mkdtemp(prefix="owlery-test-research-")
os.environ["OWLERY_RESEARCH_DIR"] = _TEST_RESEARCH_DIR
atexit.register(lambda: shutil.rmtree(_TEST_RESEARCH_DIR, ignore_errors=True))

# `fork_dir` is deliberately NOT pinned here: test_session_duplicate.py asserts
# fork copies land under `$HOME/.owlery/fork`, relocating them by monkeypatching
# $HOME. That works because `resolved_fork_dir` expands `~` at USE time.

# Disable the Octopus→Owlery first-boot migration by default: a test that spins
# up the real lifespan would otherwise MOVE the developer's live ~/.octopus out
# from under their running install. tests/test_legacy_rename.py exercises it
# with home_dir + legacy_home_dir both pinned inside tmp_path.
os.environ["OWLERY_LEGACY_HOME_DIR"] = ""

# Real-CLI availability gates live in tests/cli_gate.py (imported by the
# *_real.py suites as `from tests.cli_gate import …`); they're not here because
# `import conftest` isn't reliably resolvable under pytest collection.
