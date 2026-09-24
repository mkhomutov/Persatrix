"""The EXP-001 harness: runs the pre-registered experiment and scores it.

EXP-001 compares five ways of getting advice on a plan, from one model call
to a governed persona discussion with memory. What it runs, and how the
scores decide, is fixed in ``docs/experiments/EXP-001-preregistration.md``
and its part 2; this package only carries those documents out.

- :mod:`evaluators.exp001.materials` reads and checks the series files.
- :mod:`evaluators.exp001.panel` reads and checks the panel: the advisers and
  arm A's instructions and template.
- :mod:`evaluators.exp001.costs` records each model call, prices it with the
  fixed table, and draws each series' arm order.
- :mod:`evaluators.exp001.runtime` gives each agent process its meeting's
  clock and call-log settings, and reads the call log back.
- :mod:`evaluators.exp001.arm_a` holds arm A's meetings: one model call each.
- :mod:`evaluators.exp001.deployment` writes a local deployment of the
  advisers for the channel arms: their config, the orchestrator's and the
  meeting's channel.
- :mod:`evaluators.exp001.processes` starts and stops a deployment's
  orchestrator and adviser processes.
- :mod:`evaluators.exp001.orchestrator` is what the harness asks a
  deployment's orchestrator over REST, and reads in its log.
- :mod:`evaluators.exp001.channel_arm` holds the meetings of arms B and C: the
  discussion, then the memo turn.
- :mod:`evaluators.exp001.packets` builds the blinded packets the raters score.
- :mod:`evaluators.exp001.scoring` cuts memos, turns scores into quality and
  measures how well the raters agree.
- :mod:`evaluators.exp001.decision` compares the arms and picks the rule.

Only ``runtime``, ``arm_a``, ``deployment`` and the modules that use them
import from the agents' runtime: the call log and the clock; for arm A the
model client and the persona prompt code, so arm A reads the advisers in the
persona agents' own words; and for a deployment the config validator.
Importing any of them loads most of the ``agents`` package. The other modules
import none of it, so they can be tested and used without starting any agent.
"""
