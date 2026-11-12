"""freebusy: email meeting scheduling for Hermes Agent.

The invariant: every offer comes from free/busy only, every slot is held
before it is offered, and a time is never shown in a zone the other person
did not give.

This package is the ontology and the policy. Hermes wiring lives in the
repository root ``__init__.py``; adapters are thin and declared.
"""

__version__ = "0.3.0"
