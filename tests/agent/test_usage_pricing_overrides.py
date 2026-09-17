from decimal import Decimal

import pytest

import hermes_cli.config as hermes_config
from agent import usage_pricing
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry


@pytest.fixture()
def overrides(monkeypatch):
    state = {"pricing": {"overrides": {}}}

    def set_overrides(mapping):
        state["pricing"]["overrides"] = mapping

    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: state)
    monkeypatch.setattr(hermes_config, "read_raw_config_readonly", lambda: state)
    return set_overrides


def test_no_block_means_no_override(overrides):
    overrides({})
    assert get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1") is None


def test_exact_match_prices_unknown_model(overrides):
    overrides({"gpt-5.4-mini": {"input": 0.40, "output": 1.60, "cache_read": 0.10}})
    entry = get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1")
    assert entry is not None
    assert entry.source == "user_override"
    assert entry.pricing_version == "user-override"
    assert entry.input_cost_per_million == Decimal("0.40")
    assert entry.cache_read_cost_per_million == Decimal("0.10")
    assert entry.cache_write_cost_per_million == Decimal("0")


def test_vendor_prefix_and_case_are_tolerated(overrides):
    overrides({"gpt-5.4-mini": {"input": 1, "output": 2}})
    assert get_pricing_entry("openai/GPT-5.4-Mini", provider="openai").source == "user_override"


def test_override_wins_over_catalog(overrides):
    overrides({"gpt-4o": {"input": 99, "output": 99}})
    entry = get_pricing_entry("gpt-4o", provider="openai", base_url="https://api.openai.com/v1")
    assert entry.source == "user_override" and entry.input_cost_per_million == Decimal("99")


def test_malformed_entry_is_ignored(overrides):
    overrides({"gpt-5.4-mini": {"input": "lots", "output": 1}, "other": "nope"})
    assert get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1") is None


def test_estimate_uses_override(overrides):
    overrides({"gpt-5.4-mini": {"input": 1.0, "output": 2.0}})
    result = estimate_usage_cost("gpt-5.4-mini", CanonicalUsage(input_tokens=1_000_000, output_tokens=500_000),
                                 provider="openai", base_url="https://api.openai.com/v1")
    assert result.status == "estimated"
    assert result.source == "user_override"
    assert result.amount_usd == Decimal("2.0")


def test_non_finite_rates_are_ignored(overrides):
    overrides({"gpt-5.4-mini": {"input": float("nan"), "output": 1},
               "gpt-5.4": {"input": float("inf"), "output": 1},
               "gpt-5.5": {"input": "Infinity", "output": 1}})
    for model in ("gpt-5.4-mini", "gpt-5.4", "gpt-5.5"):
        assert get_pricing_entry(model, provider="openai", base_url="https://api.openai.com/v1") is None


def test_parsed_overrides_are_memoized(overrides):
    overrides({"gpt-5.4-mini": {"input": 1, "output": 2}})
    first = get_pricing_entry("gpt-5.4-mini", provider="openai")
    second = get_pricing_entry("gpt-5.4-mini", provider="openai")
    assert first is second
