"""
Regression test for CC-1278 / task #202419.

The ActivitySummary writer in process_xml_upload_task must resolve to the same
no-device Apple data_source as the Record path (source="apple_health_xml").
Before the fix it passed source=None, which minted a second no-device
data_source for the same user and split the activity rings from the series rows.
"""

import textwrap
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_xml_upload_task import _import_xml_data
from app.models import ActivitySummary, DataSource
from app.schemas.enums import ProviderName
from tests.factories import UserFactory

RECORD_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <HealthData locale="en_US">
      <Record type="HKQuantityTypeIdentifierStepCount"
        sourceName="iPhone" unit="count"
        startDate="2026-01-01 08:00:00 +0000"
        endDate="2026-01-01 08:05:00 +0000" value="120"/>
    </HealthData>
""")

ACTIVITY_SUMMARY_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <HealthData locale="en_US">
      <ActivitySummary dateComponents="2026-01-01"
        activeEnergyBurned="500" activeEnergyBurnedGoal="600"
        activeEnergyBurnedUnit="kcal"
        appleMoveTime="30" appleMoveTimeGoal="30"
        appleExerciseTime="40" appleExerciseTimeGoal="30"
        appleStandHours="12" appleStandHoursGoal="12"/>
    </HealthData>
""")


def test_activity_summary_reuses_record_path_data_source(db: Session, tmp_path: Path) -> None:
    user = UserFactory()

    records_file = tmp_path / "records.xml"
    records_file.write_text(RECORD_XML)
    summary_file = tmp_path / "summary.xml"
    summary_file.write_text(ACTIVITY_SUMMARY_XML)

    _import_xml_data(db, str(records_file), str(user.id))
    _import_xml_data(db, str(summary_file), str(user.id))
    db.flush()

    no_device_sources = (
        db.query(DataSource)
        .filter(
            DataSource.user_id == user.id,
            DataSource.provider == ProviderName.APPLE,
            DataSource.device_model.is_(None),
        )
        .all()
    )
    assert len(no_device_sources) == 1
    assert no_device_sources[0].source == "apple_health_xml"

    summaries = db.query(ActivitySummary).all()
    assert len(summaries) == 1
    assert all(s.data_source_id == no_device_sources[0].id for s in summaries)
