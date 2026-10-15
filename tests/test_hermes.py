import yaml
import pytest

from modelradar.adapters.hermes import HermesAdapter


def _config(home, target, text):
    if target == "global":
        p = home / "config.yaml"
    else:
        p = home / "profiles" / target / "config.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_read_global_primary_and_free_fallback(tmp_path):
    _config(tmp_path, "global",
            "model:\n"
            "  provider: openrouter\n"
            "  default: qwen/qwen3-flash\n"
            "  context_length: 1000000\n"
            "fallback_providers:\n"
            "  - provider: openrouter\n"
            "    model: deepseek/deepseek-v4:free\n")
    a = HermesAdapter({"home": str(tmp_path)})
    assert a.targets() == ["global"]
    p = a.read("global")
    assert p.provider == "openrouter"
    assert p.model == "qwen/qwen3-flash"
    assert p.context_length == 1000000
    assert p.free_fallback == "deepseek/deepseek-v4:free"


def test_read_named_profile(tmp_path):
    _config(tmp_path, "coder",
            "model:\n  provider: openrouter\n  default: x/y\n")
    a = HermesAdapter({"home": str(tmp_path)})
    assert a.targets() == ["coder"]
    assert a.read("coder").model == "x/y"


def test_duplicate_key_rejected(tmp_path):
    _config(tmp_path, "global", "model: a\nmodel: b\n")
    a = HermesAdapter({"home": str(tmp_path)})
    with pytest.raises(ValueError):
        a.read("global")


def test_arbitrary_python_object_tag_rejected(tmp_path):
    _config(tmp_path, "global",
            "model: !!python/object/apply:os.system ['echo pwned']\n")
    a = HermesAdapter({"home": str(tmp_path)})
    with pytest.raises(yaml.YAMLError):
        a.read("global")
