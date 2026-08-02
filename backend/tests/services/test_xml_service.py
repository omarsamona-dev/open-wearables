"""
Regression tests for XMLService (xml_service.py).

Covers:
- Steps records land under SeriesType.steps (not heart_rate or any other type)
- Heart rate records land under SeriesType.heart_rate (not steps)
- Unsupported HK category types increment the skip counter (Bug B regression)
- Parse stats balance: read == processed + skipped for every run
"""

import textwrap
from pathlib import Path

import pytest

from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import HeartRateSampleCreate, StepSampleCreate
from app.schemas.providers.apple.apple_xml.stats import XMLParseStats
from app.services.apple.apple_xml.xml_service import XMLService


# A minimal but complete Apple Health XML fixture:
#  - 2 heart_rate records
#  - 3 step_count records
#  - 1 resting_heart_rate record
#  - 2 unsupported category-type records (AppleStandHour, MindfulSession)
#  - 1 heart_rate record with an invalid decimal value
FIXTURE_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <HealthData locale="en_US">
      <Record type="HKQuantityTypeIdentifierHeartRate"
        sourceName="Apple Watch" unit="count/min"
        startDate="2026-01-01 08:00:00 +0000"
        endDate="2026-01-01 08:00:05 +0000" value="72"/>
      <Record type="HKQuantityTypeIdentifierHeartRate"
        sourceName="Apple Watch" unit="count/min"
        startDate="2026-01-01 08:01:00 +0000"
        endDate="2026-01-01 08:01:05 +0000" value="75"/>
      <Record type="HKQuantityTypeIdentifierStepCount"
        sourceName="iPhone" unit="count"
        startDate="2026-01-01 08:00:00 +0000"
        endDate="2026-01-01 08:05:00 +0000" value="120"/>
      <Record type="HKQuantityTypeIdentifierStepCount"
        sourceName="iPhone" unit="count"
        startDate="2026-01-01 09:00:00 +0000"
        endDate="2026-01-01 09:05:00 +0000" value="200"/>
      <Record type="HKQuantityTypeIdentifierStepCount"
        sourceName="iPhone" unit="count"
        startDate="2026-01-01 10:00:00 +0000"
        endDate="2026-01-01 10:05:00 +0000" value="150"/>
      <Record type="HKQuantityTypeIdentifierRestingHeartRate"
        sourceName="Apple Watch" unit="count/min"
        startDate="2026-01-01 07:00:00 +0000"
        endDate="2026-01-01 07:00:01 +0000" value="58"/>
      <Record type="HKCategoryTypeIdentifierAppleStandHour"
        sourceName="Apple Watch" unit=""
        startDate="2026-01-01 09:00:00 +0000"
        endDate="2026-01-01 10:00:00 +0000" value="1"/>
      <Record type="HKCategoryTypeIdentifierMindfulSession"
        sourceName="iPhone" unit=""
        startDate="2026-01-01 10:00:00 +0000"
        endDate="2026-01-01 10:10:00 +0000" value="1"/>
      <Record type="HKQuantityTypeIdentifierHeartRate"
        sourceName="Apple Watch" unit="count/min"
        startDate="2026-01-01 08:30:00 +0000"
        endDate="2026-01-01 08:30:05 +0000" value="INVALID"/>
    </HealthData>
""")

USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture()
def fixture_xml(tmp_path: Path) -> Path:
    xml_file = tmp_path / "test_export.xml"
    xml_file.write_text(FIXTURE_XML, encoding="utf-8")
    return xml_file


@pytest.fixture()
def parsed_output(fixture_xml: Path, caplog: pytest.LogCaptureFixture):
    import logging

    service = XMLService(fixture_xml, logging.getLogger("test_xml_service"))
    all_records = []
    all_workouts = []
    for ts_records, workouts, _ in service.parse_xml(USER_ID):
        all_records.extend(ts_records)
        all_workouts.extend(workouts)
    return all_records, all_workouts, service.stats


class TestXMLServiceTypeAttribution:
    """Steps and HR records must land under their own series types, not each other's."""

    def test_steps_land_as_steps(self, parsed_output):
        records, _, _ = parsed_output
        step_records = [r for r in records if r.series_type == SeriesType.steps]
        assert len(step_records) == 3, (
            f"Expected 3 step records, got {len(step_records)}; "
            f"types seen: {[r.series_type for r in records]}"
        )

    def test_steps_are_StepSampleCreate_instances(self, parsed_output):
        records, _, _ = parsed_output
        step_records = [r for r in records if r.series_type == SeriesType.steps]
        for r in step_records:
            assert isinstance(r, StepSampleCreate), (
                f"Step record is {type(r).__name__}, expected StepSampleCreate"
            )

    def test_heart_rate_lands_as_heart_rate(self, parsed_output):
        records, _, _ = parsed_output
        hr_records = [r for r in records if r.series_type == SeriesType.heart_rate]
        # 2 valid HR records; 1 INVALID-value HR is skipped
        assert len(hr_records) == 2, (
            f"Expected 2 heart_rate records, got {len(hr_records)}; "
            f"types seen: {[r.series_type for r in records]}"
        )

    def test_heart_rate_are_HeartRateSampleCreate_instances(self, parsed_output):
        records, _, _ = parsed_output
        hr_records = [r for r in records if r.series_type == SeriesType.heart_rate]
        for r in hr_records:
            assert isinstance(r, HeartRateSampleCreate), (
                f"HR record is {type(r).__name__}, expected HeartRateSampleCreate"
            )

    def test_no_steps_in_heart_rate_bucket(self, parsed_output):
        records, _, _ = parsed_output
        hr_records = [r for r in records if r.series_type == SeriesType.heart_rate]
        step_values = {120, 200, 150}
        for r in hr_records:
            assert float(r.value) not in step_values, (
                f"Step value {r.value} found in heart_rate bucket — type misattribution!"
            )

    def test_no_heart_rate_in_steps_bucket(self, parsed_output):
        records, _, _ = parsed_output
        step_records = [r for r in records if r.series_type == SeriesType.steps]
        hr_values = {72, 75}
        for r in step_records:
            assert float(r.value) not in hr_values, (
                f"Heart rate value {r.value} found in steps bucket — type misattribution!"
            )


class TestXMLServiceSkipCounter:
    """Every non-landed record must appear in the skip counter with a reason (Bug B regression)."""

    def test_unsupported_types_increment_skip_counter(self, parsed_output):
        _, _, stats = parsed_output
        # 2 unsupported category records + 1 invalid-value HR = 3 skips total
        assert stats.records.skipped == 3, (
            f"Expected 3 skipped records, got {stats.records.skipped}. "
            "Unsupported-type records may be silently dropped (Bug B)."
        )

    def test_unsupported_type_reason_is_named(self, parsed_output):
        _, _, stats = parsed_output
        reasons = stats.records.reasons
        unsupported_reasons = [r for r in reasons if r.startswith("unsupported_type:")]
        assert len(unsupported_reasons) >= 1, (
            f"No 'unsupported_type:*' reason in skip counter; got: {dict(reasons)}"
        )

    def test_invalid_value_reason_is_named(self, parsed_output):
        _, _, stats = parsed_output
        reasons = stats.records.reasons
        invalid_reasons = [r for r in reasons if r.startswith("invalid_value:")]
        assert len(invalid_reasons) >= 1, (
            f"No 'invalid_value:*' reason in skip counter; got: {dict(reasons)}"
        )

    def test_skip_counter_balance_read_equals_processed_plus_skipped(self, parsed_output):
        _, _, stats = parsed_output
        assert stats.records.is_balanced(), (
            f"Balance fail: read={stats.records.read}, "
            f"processed={stats.records.processed}, "
            f"skipped={stats.records.skipped}. "
            f"read != processed + skipped — silent drops remain."
        )

    def test_malformed_record_increments_skip_counter(self, tmp_path: Path):
        """Feeding one unmappable record confirms the skip counter increments (7.3c)."""
        import logging

        xml_content = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <HealthData locale="en_US">
              <Record type="HKCategoryTypeIdentifierAppleStandHour"
                sourceName="Apple Watch" unit=""
                startDate="2026-01-01 09:00:00 +0000"
                endDate="2026-01-01 10:00:00 +0000" value="1"/>
            </HealthData>
        """)
        xml_file = tmp_path / "malformed.xml"
        xml_file.write_text(xml_content, encoding="utf-8")

        service = XMLService(xml_file, logging.getLogger("test_malformed"))
        for _ in service.parse_xml(USER_ID):
            pass

        assert service.stats.records.skipped == 1, (
            "One unsupported-type record must increment skip counter to 1; "
            f"got {service.stats.records.skipped}. Bug B is not fixed."
        )
        reasons = service.stats.records.reasons
        assert any(r.startswith("unsupported_type:") for r in reasons), (
            f"No 'unsupported_type:*' reason in skip counter; got: {dict(reasons)}"
        )
