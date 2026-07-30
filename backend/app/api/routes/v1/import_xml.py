import json
import os
import uuid
from json import JSONDecodeError

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from pydantic import ValidationError

from app.integrations.celery.tasks.process_xml_upload_task import process_xml_upload
from app.schemas.providers.apple.apple_xml import (
    PresignedURLRequest,
    PresignedURLResponse,
    SNSNotification,
)
from app.schemas.responses.upload import UploadDataResponse
from app.services import ApiKeyDep
from app.services.apple.apple_xml.presigned_url_service import presigned_url_service
from app.services.apple.apple_xml.sns_service import sns_service

router = APIRouter()

XML_UPLOAD_DIR = "/data/xml-uploads"
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB


@router.post("/users/{user_id}/import/apple/xml/s3")
def import_xml_presigned_url(
    user_id: str,
    request: PresignedURLRequest,
    _api_key: ApiKeyDep,
) -> PresignedURLResponse:
    """Generate presigned URL for XML file upload and trigger processing task."""
    return presigned_url_service.create_presigned_url(user_id, request)


@router.post("/users/{user_id}/import/apple/xml/direct")
def import_xml_file(
    user_id: str,
    file: UploadFile,
    _api_key: ApiKeyDep,
) -> dict[str, str]:
    """Stream uploaded XML to shared volume; enqueue only the file path via Celery."""
    filename = file.filename or "upload.xml"
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    dest_path = os.path.join(XML_UPLOAD_DIR, unique_name)

    os.makedirs(XML_UPLOAD_DIR, exist_ok=True)

    bytes_written = 0
    try:
        with open(dest_path, "wb") as out:
            while chunk := file.file.read(8 * 1024 * 1024):  # 8 MB chunks
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Upload exceeds {MAX_UPLOAD_BYTES} byte limit",
                    )
                out.write(chunk)
    except HTTPException:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise

    task = process_xml_upload.delay(file_path=dest_path, filename=filename, user_id=user_id)

    return {
        "status": "processing",
        "task_id": task.id,
        "user_id": user_id,
    }


@router.post("/sns/notification", status_code=status.HTTP_202_ACCEPTED)
async def receive_sns_notification(
    request: Request,
) -> UploadDataResponse:
    """Handle all SNS messages (subscription confirmation + S3 upload notifications)."""
    body = await request.body()
    try:
        notification = SNSNotification.model_validate(json.loads(body))
    except (ValidationError, JSONDecodeError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    result = await sns_service.handle_sns_notification(notification)

    if result.status_code not in (status.HTTP_200_OK, status.HTTP_202_ACCEPTED):
        raise HTTPException(status_code=result.status_code, detail=result.response)
    return result
