# Persona files

These two files are the fixed start of every prompt Enzo gets. You can edit them by hand.

- `ENZO.md`: who Enzo is and how it talks (personality, tone, rules for answers).
- `USER.md`: lasting facts about you that Enzo should always know.

**To change them on the NAS without rebuilding:** put your own copy in the stack's `data/` folder
(`data/ENZO.md`, `data/USER.md`). A file in `data/` wins over the one here and survives updates.
Changes are picked up on the next message; no restart needed.

Keep both short. Every line is read by the local model on every request, so a long file makes each answer slower
and leaves less room for the conversation. Facts Enzo learns by itself go to its memory, not here.

After changing `ENZO.md`, run `python -m agent.bench --loop` and compare with the last score: the wording of the
prompt changes how well the model picks tools.
