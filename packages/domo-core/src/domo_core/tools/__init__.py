"""The core's own tools: what Domo can do in the household, through the policy barrier.

A tool (``context.InternalTool``) does two things. ``describe`` reads the arguments the model sent
and says, in the barrier's terms, what the call would do (a ``DescribedAction``); it rejects
arguments that do not make sense (``InvalidArguments``). ``execute`` does it, once the barrier
allowed it (or a person said yes), and tells what came of it (``ToolOutcome``): a result for the
model and a card for the people. ``registry`` lists the tools, and says which ones a role sees.
"""
