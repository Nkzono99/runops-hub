## runops hub

This project is also reachable from the hub at `{hub}`
(`./hub list`, `./hub sessions` there give the cross-project view). Work here as usual.

- Several agents can work here at once when each owns its own runs, cases and
  `notes/<topic>.md`. Add to `LOG.md` with `cat >> LOG.md <<'EOF'`; appends from
  different agents do not overwrite each other.
- When this project is a git repository, commit the files you changed once a piece of
  work is done, staging them by path (no need to ask), and leave other agents' work in
  progress out. hub auto commits what its agent wrote after each tick. Simulation outputs and
  large files stay out of git (list them in `.gitignore`). Push only when the user asks.
- Decide reversible things yourself and report them: the analysis method and figures,
  conclusions in `LOG.md`, notes and analysis READMEs, the wording of board rows and
  reports, and any choice you can recommend. Go with your recommendation, list what you
  decided at the end of the report, and write the reason and the options not taken in
  `LOG.md`. Ask about simulation submits that need
  confirmation, push, release, deletion, changes to the claims of a submission-track
  paper, and results that no agreed branch covers.
- A run follows the track of the paper it answers. The tracks are in the paper table of
  the hub's `AGENTS.md`; name the paper in the run's `why`. Runs for a submission-track
  paper keep every check. For runs for a progress-track paper, and runs that no paper
  uses, skip the steady-state, convergence and seed-repeat checks the main claim does
  not need, and list them in `LOG.md` as 投稿前 (before submission).
- Carry out branches agreed in advance. Write "if this happens, do that" in the run's
  `why` when you create it, or with `runo note` when its submit is confirmed. When the
  result meets the condition, do the analysis, `LOG.md` and progress-paper updates
  without asking. If the branch needs a simulation, show its condition and a core-h cap
  together with the parent's submit; once approved, record
  `runo note RUN "branch (approved YYYY-MM-DD, cap N core-h): ..."`. Then submit it
  without asking when the condition is met and the estimate is within the cap, and
  report it. Ask when the condition is unclear or the cap would be exceeded.
