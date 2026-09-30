# documentation binding

Kept documentation is exactly these living documents: the root `README.md`; under `docs/`,
`design.md`, `roadmap.md`, `pipeline/` with its `index.md`, one file per job, and `launch.md`,
`architecture.md`, `writing-a-plugin.md`, and `runbooks/per-agent-instance.md`; and each plugin's
`plugins/<plugin>/README.md`. Update a kept document whenever a change affects what it describes.
The enumeration is the rule, and the set is closed. Only the operator adds a kept document; never
add one as a default home for new prose.

`architecture.md`, `writing-a-plugin.md`, `runbooks/per-agent-instance.md`, and each plugin's
`README.md` are kept documents whether or not they exist. The first pull request with something to
describe creates each one.

## What each document holds

- `design.md` holds the operator's closed decisions for the proof of concept. An item's pull request
  edits only the `design.md` sections its roadmap entry names. Every other change to it is PM's, and
  a change to a closed decision needs the operator.
- `roadmap.md` is an ordered roster under the decision the project `CLAUDE.md` records.
- `architecture.md` describes what is built and cites the code by path.
- `spike/FINDINGS.md` is a dated record: cite it by check ID, and never edit it.

Everything else resolves to the workspace root: the root `documentation` topic for retrospective
reports, the `implementation` skill for a work item's working documents, the `task-management` topic
for the pull request as the item's record, and the workspace root `CLAUDE.md`'s Writing section for
the style every kept document must satisfy.
