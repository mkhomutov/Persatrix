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
- :mod:`evaluators.exp001.packets` builds the blinded packets the raters score.
- :mod:`evaluators.exp001.scoring` cuts memos, turns scores into quality and
  measures how well the raters agree.
- :mod:`evaluators.exp001.decision` compares the arms and picks the rule.

Only ``runtime`` and ``arm_a`` import from the agents' runtime: the call log
and the clock, and for arm A the model client and the persona prompt code, so
arm A reads the advisers in the persona agents' own words. Importing either
loads most of the ``agents`` package. The other modules import none of it, so
they can be tested and used without starting any agent.
"""
