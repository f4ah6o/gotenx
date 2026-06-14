"""Gotenx: deterministic Panel -> Judge -> Eval -> Proposal pipeline.

Implements the Gotenx v1.2 Frozen Baseline (spec P1-P24). The core is fully
deterministic: metrics are computed from the ID provenance graph only, never
from text matching. LLM judgement (provenance_faithful) is confined to the Eval
layer and sampled, so the metric layer stays deterministic (P11/P23).
"""

__version__ = "1.2.0"
SCHEMA_VERSION = "gotenx.config.policy.v2"
