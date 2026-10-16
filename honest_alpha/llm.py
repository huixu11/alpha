"""Claude, through Amazon Bedrock or the Anthropic API. Imported only after tracing.setup()."""
from __future__ import annotations

import os
import re

_TIER = {"opus": 3, "sonnet": 1, "haiku": 0}


def _rank(model_id: str) -> tuple:
    """Haiku last, then the highest version (newer ids carry no date), Opus first, US profiles before global."""
    name = model_id.split("anthropic.", 1)[-1]
    parts = re.split(r"[-:]", name)
    version = tuple(int(p) for p in parts if p.isdigit() and len(p) <= 2)[:2]
    tier = next((v for k, v in _TIER.items() if k in parts), 2)
    stamp = next((p for p in parts if p.isdigit() and len(p) == 8), "")
    prefix = {"us": 2, "global": 0}.get(model_id.split(".", 1)[0], 1)
    return (tier != 0, version + (0,) * (2 - len(version)), tier, stamp, prefix)


def bedrock_models(region: str) -> list[str]:
    """Claude ids this account can call on demand, best first: newest, Haiku last."""
    import boto3
    br = boto3.client("bedrock", region_name=region)
    ids: list[str] = []
    try:
        pages = br.get_paginator("list_inference_profiles").paginate(typeEquals="SYSTEM_DEFINED")
        ids = [p["inferenceProfileId"] for page in pages for p in page["inferenceProfileSummaries"]
               if "anthropic.claude" in p["inferenceProfileId"] and p.get("status") == "ACTIVE"]
    except Exception:
        pass
    if not ids:
        ids = [m["modelId"] for m in br.list_foundation_models(byProvider="Anthropic")["modelSummaries"]
               if "ON_DEMAND" in m.get("inferenceTypesSupported", [])]
    return sorted(set(ids), key=_rank, reverse=True)


def client():
    """(client, model, where) from the environment; Bedrock unless ANTHROPIC_API_KEY is set."""
    import anthropic
    model = os.environ.get("HONEST_ALPHA_MODEL", "").strip()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic(), model or "claude-opus-5", "anthropic"
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("no Claude credentials: put the AWS lab credentials and AWS_REGION, "
                           "or ANTHROPIC_API_KEY, in .env")
    if not model:
        found = bedrock_models(region)
        if not found:
            raise RuntimeError(f"this AWS account lists no Claude model in {region}; set HONEST_ALPHA_MODEL")
        model = found[0]
    return anthropic.AnthropicBedrock(aws_region=region), model, f"bedrock:{region}"
