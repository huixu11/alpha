"""quantdesk - a pre-registered, auditable daily record of one deterministic rule.

The rule lives in docs/strategy_spec.md. Where this code and that document
disagree, the document wins and this code is a bug.
"""

from quantdesk.config import SPEC_VERSION

__all__ = ["SPEC_VERSION"]
