"""Claude, through Amazon Bedrock or the Anthropic API. Imported only after tracing.setup()."""
from __future__ import annotations

import os
import re

_PREF = ("opus", "sonnet", "haiku")


def _rank(model_id: str) -> tuple:
    stamp = re.search(r"(\d{8})", model_id)
    tier = next((i for i, n in enumerate(_PREF) if n in model_id), len(_PREF))
    return (tier != 2, stamp.group(1) if stamp else "", -tier)


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
