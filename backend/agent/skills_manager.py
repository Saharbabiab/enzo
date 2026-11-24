import os
import sys
import json
import asyncio
import importlib.util
import inspect
from loguru import logger

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# Cache to store loaded functions
_SKILLS_CACHE = {}

def load_skills():
    """Initializes and loads all Python scripts in the skills/ directory."""
    global _SKILLS_CACHE
    _SKILLS_CACHE.clear()
    
    if not os.path.exists(SKILLS_DIR):
        os.makedirs(SKILLS_DIR, exist_ok=True)
        
    from agent import skill_builder as sb
    files = [os.path.join(SKILLS_DIR, f) for f in os.listdir(SKILLS_DIR) if f.endswith(".py") and f != "__init__.py"]
    # Self-written skills: data/skills/<name>/<name>.py (kept across updates; _proposed ones are not live yet).
    if os.path.isdir(sb.OWN_DIR):
        files += [sb._own_file(d) for d in sorted(os.listdir(sb.OWN_DIR)) if not d.startswith("_") and os.path.exists(sb._own_file(d))]
    for file_path in files:
            filename = os.path.basename(file_path)
            module_name = filename[:-3]
            
            try:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                
                # Extract all public functions
                for name, obj in inspect.getmembers(module, inspect.isfunction):
                    if not name.startswith("_") and obj.__module__ == module_name:
                        _SKILLS_CACHE[name] = obj
                logger.info(f"🧩 Loaded skills from {filename}")
            except Exception as e:
                logger.error(f"Failed to load skill {filename}: {e}")

def get_skill_descriptions() -> str:
    """Returns a formatted string of all available skills for the LLM prompt."""
    if not _SKILLS_CACHE:
        load_skills()
        
    if not _SKILLS_CACHE:
        return "No dynamic skills loaded."
        
    lines = ["Available Dynamic Skills (Use action='execute_skill'):"]
    for name, func in _SKILLS_CACHE.items():
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description provided."
        lines.append(f"- {name}{sig}: {doc}")
        
    return "\n".join(lines)

def is_skill(name: str) -> bool:
    if not _SKILLS_CACHE:
        load_skills()
    return name in _SKILLS_CACHE


def resolve_skill_call(action: str, inp) -> tuple[str | None, dict]:
    """
    Small models call skills in many shapes. Normalise all of them to (name, kwargs):
      {"action": "get_current_weather", "input": {"city_name": "X"}}
      {"action": "execute_skill", "input": {"skill_name": "...", "kwargs": {...}}}
      {"action": "execute_skill", "input": {"skill": "...", "city_name": "X"}}
      {"action": "execute_skill", "input": {"city_name": "X"}}   ← name inferred from params
    """
    if not _SKILLS_CACHE:
        load_skills()
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {}
    if not isinstance(inp, dict):
        inp = {}

    if action in _SKILLS_CACHE:
        return action, inp

    name = inp.get("skill_name") or inp.get("skill") or inp.get("name") or inp.get("function")
    if isinstance(name, str):
        name = name.removesuffix(".py")
    kwargs = inp.get("kwargs") or inp.get("args") or inp.get("arguments")
    if not isinstance(kwargs, dict):
        kwargs = {k: v for k, v in inp.items()
                  if k not in ("skill_name", "skill", "name", "function", "kwargs", "args", "arguments")}

    if name not in _SKILLS_CACHE:
        # Infer: the one skill whose parameters match the given keys.
        keys = set(kwargs)
        matches = [n for n, f in _SKILLS_CACHE.items()
                   if keys and keys <= set(inspect.signature(f).parameters)]
        name = matches[0] if len(matches) == 1 else name
    return name, kwargs


async def execute_skill(skill_name: str, kwargs: dict) -> str:
    """Safely executes a dynamic skill."""
    if not _SKILLS_CACHE:
        load_skills()
        
    if skill_name not in _SKILLS_CACHE:
        return f"[SKILL ERROR]: Skill '{skill_name}' not found."
        
    func = _SKILLS_CACHE[skill_name]
    try:
        # Check if it's an async function
        if inspect.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            # Sync skills often do network I/O — keep the event loop free.
            result = await asyncio.to_thread(func, **kwargs)
            
        return f"[SKILL OUTPUT]:\n{result}"
    except TypeError as e:
        return f"[SKILL ERROR]: Invalid arguments provided for {skill_name}: {e}"
    except Exception as e:
        return f"[SKILL ERROR]: {e}"
