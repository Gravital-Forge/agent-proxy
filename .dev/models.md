# models binding

The operator's choice for this project is Opus 5.5 for every agent. This binding displaces the root
topic's per-role tiers.

## How to use it

- Baton's project model is `claude-opus-5-5`. Every baton worker runs on it: each job's worker, a
  diagnosis worker, and a resumed worker.
- A job's `report_lifecycle` call omits `model`, so the next worker runs on the project model.
- Dispatch every subagent with `model: "opus"`.

Everything else resolves to the workspace root's `models` topic: the roles it names, each of which
runs on Opus here.
