"""Every check_* in agent/proactive.py must be in the loop (merges have dropped one before)."""
import inspect
import re

from agent import proactive


def test_every_check_runs_in_the_loop():
    checks = {n for n, f in vars(proactive).items() if n.startswith("check_") and inspect.iscoroutinefunction(f)}
    loop = inspect.getsource(proactive.proactive_loop)
    in_loop = set(re.findall(r"check_\w+", re.search(r"for check in \((.*?)\):", loop, re.S).group(1)))
    assert checks == in_loop
