"""Phase 3: skills as playbooks (SKILL.md files) and self-written skills staged with tests."""

import asyncio
import os

import pytest

from agent import approvals, playbooks, skill_builder
from agent.context import new_session
from agent.tools import registry


@pytest.fixture
def own(tmp_path, monkeypatch):
    """The user's own skills folder -> a temp dir; the registry is rebuilt afterwards."""
    monkeypatch.setattr(playbooks, "OWN_DIR", str(tmp_path))
    monkeypatch.setattr(skill_builder, "OWN_DIR", str(tmp_path))
    yield tmp_path
    monkeypatch.undo()
    registry().reload()


@pytest.fixture
def approve(monkeypatch):
    async def yes(tool, args):
        return True
    monkeypatch.setattr(approvals, "request", yes)


def _write(base, name, text):
    os.makedirs(base / name, exist_ok=True)
    (base / name / "SKILL.md").write_text(text, encoding="utf-8")


def test_parse_frontmatter():
    pb = playbooks.parse("---\nname: x_y\ndescription: Do  it.\ntools: a, b\n---\n1. step")
    assert pb == {"name": "x_y", "description": "Do it.", "tools": ["a", "b"], "body": "1. step", "source": "", "patch": False}
    learned = playbooks.parse("---\nname: x_y\ndescription: D.\nsource: learned\npatch: yes\n---\n1. step")
    assert learned["source"] == "learned" and learned["patch"]


def test_shipped_playbooks_use_real_tools():
    books = playbooks.all_playbooks()
    assert {"good_night", "leaving_home", "morning_routine", "research_purchase", "plan_day"} <= set(books)
    names = set(registry().funcs)
    for b in books.values():
        assert b["description"] and b["body"]
        assert set(b["tools"]) <= names, (b["name"], set(b["tools"]) - names)


def test_catalog_in_stable_prompt():
    from agent.responder import build_system_parts
    stable, volatile = build_system_parts()
    assert "=== PLAYBOOKS" in stable and "- good_night:" in stable and "PLAYBOOKS" not in volatile


def test_own_playbook_wins_hot_reload_and_proposed_hidden(own):
    assert "movie_time" not in playbooks.catalog()
    _write(own, "movie_time", "---\nname: movie_time\ndescription: Film night.\n---\n1. silent")
    _write(own, "good_night", "---\nname: good_night\ndescription: My own bedtime.\n---\n1. mine")
    _write(own / "_proposed", "draft", "---\nname: draft\ndescription: Not live.\n---\n1. x")
    cat = playbooks.catalog()
    assert "- movie_time: Film night." in cat and "My own bedtime." in cat and "draft" not in cat
    assert "1. mine" in playbooks.load("good night")


def test_use_skill_tool():
    out = asyncio.run(registry().call("use_skill", {"name": "plan_day"}))
    assert out.startswith("PLAYBOOK plan_day") and "calendar_events" in out
    assert asyncio.run(registry().call("use_skill", {"name": "nope"})).startswith("[ERROR]")
    # The model sometimes calls the playbook by its name, as if it were a tool.
    assert asyncio.run(registry().call("morning_routine", {})).startswith("PLAYBOOK morning_routine")


def test_use_skill_description_names_playbooks(own):
    desc = lambda: next(s for s in registry().schemas if s["function"]["name"] == "use_skill")["function"]["description"]
    assert "good_night (Bedtime routine." in desc() and "movie_time" not in desc()
    _write(own, "movie_time", "---\nname: movie_time\ndescription: Film night.\n---\n1. silent")
    assert "movie_time (Film night.)" in desc()          # hot reload, no restart


def test_save_playbook_needs_approval(own):
    async def run():
        new_session()                                # nobody to ask -> declined, nothing written
        return await registry().call("save_playbook", {"name": "movie_time", "when": "Film night.", "steps": "1. silent"})
    assert asyncio.run(run()).startswith("[DECLINED]")
    assert not (own / "movie_time").exists()


def test_save_playbook(own, approve):
    args = {"name": "Movie Time", "when": "When he says movie time.", "steps": "1. Phone on silent.\n2. Play liked songs.",
            "tools": "phone_settings, phone_music"}
    assert asyncio.run(registry().call("save_playbook", args)).startswith("[OK]")
    pb = playbooks.all_playbooks()["movie_time"]
    assert pb["own"] and pb["tools"] == ["phone_settings", "phone_music"] and "Play liked songs" in pb["body"]
    bad = asyncio.run(registry().call("save_playbook", {**args, "tools": "make_coffee"}))
    assert bad.startswith("[ERROR]") and "make_coffee" in bad
    assert asyncio.run(registry().call("remove_skill", {"name": "movie_time"})).startswith("[OK]")
    assert "movie_time" not in playbooks.all_playbooks()
    # For a day the per-turn context says it is gone (the chat still shows its old runs); saving it again clears that.
    from agent.responder import build_system_parts
    assert playbooks.recently_removed() == ["movie_time"] and "REMOVED ROUTINES" in build_system_parts()[1]
    assert asyncio.run(registry().call("save_playbook", args)).startswith("[OK]")
    assert playbooks.recently_removed() == []


# ── create_skill: staged with tests, live only after approval + passing tests ──

SKILL = '''
def double_it(n: int) -> str:
    """Double a number.

    Args:
        n: The number.
    """
    return str(n * 2)
'''
PASSING = "from double_math import double_it\n\ndef test_double():\n    assert double_it(4) == '8'\n"
FAILING = "from double_math import double_it\n\ndef test_double():\n    assert double_it(4) == '9'\n"


def test_create_skill_requires_tests():
    problems, _ = skill_builder.check_proposal(SKILL, "")
    assert any("test_" in p for p in problems)
    problems, _ = skill_builder.check_proposal(SKILL, "import subprocess\ndef test_x():\n    assert True\n")
    assert any("subprocess" in p for p in problems)


def test_create_skill_failing_tests_stay_proposed(own, approve):
    out = asyncio.run(registry().call("create_skill", {"name": "double_math", "code": SKILL, "tests": FAILING}))
    assert out.startswith("[ERROR]") and "NOT installed" in out and "FAILED test_double" in out
    assert (own / "_proposed" / "double_math" / "double_math.py").exists()
    assert "double_it" not in registry().funcs


def test_create_skill_passing_tests_installs(own, approve):
    out = asyncio.run(registry().call("create_skill", {"name": "double_math", "code": SKILL, "tests": PASSING}))
    assert out.startswith("[OK]"), out
    assert (own / "double_math" / "double_math.py").exists() and (own / "double_math" / "test_double_math.py").exists()
    assert not (own / "_proposed" / "double_math").exists()
    assert asyncio.run(registry().call("double_it", {"n": 5})) == "10"
    assert "double_math (self-written)" in asyncio.run(registry().call("list_skills", {}))
    # Updating the same skill is not a name clash.
    assert asyncio.run(registry().call("create_skill", {"name": "double_math", "code": SKILL, "tests": PASSING})).startswith("[OK]")
    assert asyncio.run(registry().call("remove_skill", {"name": "double_math"})).startswith("[OK]")
    assert "double_it" not in registry().funcs


def test_create_skill_declined_runs_nothing(own):
    async def run():
        new_session()
        return await registry().call("create_skill", {"name": "double_math", "code": SKILL, "tests": PASSING})
    assert asyncio.run(run()).startswith("[DECLINED]")
    assert not os.listdir(own)
