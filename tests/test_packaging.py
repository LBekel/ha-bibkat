"""Check integration packaging and translation consistency."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "bibkat"


def leaf_keys(value, prefix=""):
    for key, item in value.items():
        path = f"{prefix}.{key}"
        if isinstance(item, dict):
            yield from leaf_keys(item, path)
        else:
            yield path


def test_translations_cover_the_same_keys():
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    for language in ("en", "de"):
        translations = json.loads(
            (INTEGRATION / "translations" / f"{language}.json").read_text(
                encoding="utf-8"
            )
        )
        assert set(leaf_keys(strings)) == set(leaf_keys(translations))
        if language == "en":
            assert strings == translations


def test_manifest_and_hacs_metadata():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert manifest["domain"] == "bibkat"
    assert manifest["version"] == "0.1.2"
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["config_flow"]
    assert manifest["requirements"] == []
    assert manifest["codeowners"] == ["@lbekel"]
    assert hacs["homeassistant"] == "2026.10.4"
    assert (
        (INTEGRATION / "brand" / "icon.png")
        .read_bytes()
        .startswith(b"\x89PNG\r\n\x1a\n")
    )


def test_renewal_action_fields_match_translations():
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    translations = json.loads((INTEGRATION / "strings.json").read_text())
    assert set(services) == {"renew"}
    assert (
        set(services["renew"]["fields"])
        == set(translations["services"]["renew"]["fields"])
        == {"config_entry_id", "medium_id", "expected_due_date"}
    )
    assert all(field["required"] for field in services["renew"]["fields"].values())
