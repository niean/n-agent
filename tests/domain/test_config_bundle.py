import pytest

from app.domain.config_bundle import (
    BUNDLE_SCHEMA_VERSION,
    SECTION_NAMES,
    ConfigBundleValidationError,
    ImportMode,
    ImportOutcome,
    ImportItemReport,
    ImportReport,
    SecretValue,
)


def test_schema_version_is_one():
    assert BUNDLE_SCHEMA_VERSION == 1


def test_section_names_cover_all_ten_config_tables():
    assert SECTION_NAMES == (
        "providers",
        "knowledge_bases",
        "mcp_sites",
        "external_memory_providers",
        "external_memory_global_config",
        "plugins",
        "skills",
        "scheduled_tasks",
        "gateway_home_targets",
        "task_config",
    )


@pytest.mark.parametrize(
    "raw,kind",
    [(None, "redacted"), ("", "cleared"), ("sk-x", "set")],
)
def test_secret_value_classifies_three_states(raw, kind):
    assert SecretValue.classify(raw).kind == kind


def test_secret_value_absent_means_unchanged():
    assert SecretValue.absent().kind == "unchanged"


def test_secret_value_never_reprs_its_payload():
    assert "sk-x" not in repr(SecretValue.classify("sk-x"))


def test_import_report_exit_code_is_zero_when_only_degraded():
    report = ImportReport(
        mode=ImportMode.MERGE,
        items=[
            ImportItemReport("providers", "p1", ImportOutcome.CREATED),
            ImportItemReport("providers", "p2", ImportOutcome.DEGRADED, reasons=["active taken"]),
        ],
    )
    assert report.exit_code == 0
    assert report.counts[ImportOutcome.DEGRADED] == 1


def test_import_report_exit_code_is_one_on_failure():
    report = ImportReport(
        mode=ImportMode.MERGE,
        items=[ImportItemReport("providers", "p1", ImportOutcome.FAILED, reasons=["boom"])],
    )
    assert report.exit_code == 1


def test_import_item_report_rejects_duplicate_natural_key_in_same_section():
    with pytest.raises(ValueError):
        ImportReport(
            mode=ImportMode.MERGE,
            items=[
                ImportItemReport("providers", "p1", ImportOutcome.CREATED),
                ImportItemReport("providers", "p1", ImportOutcome.SKIPPED),
            ],
        )


def test_validation_error_carries_section_and_reason_without_payload():
    err = ConfigBundleValidationError("providers", "two active providers in bundle")
    assert err.section == "providers"
    assert "two active" in str(err)
