# 风格：命名抽象化 + 轻微表达式改写 | 布局：保持接口不变 | 命名：content_flavor + label_bundle + artifact_record
from fastapi import APIRouter, HTTPException, status, Depends, UploadFile, File, Form, Query
from typing import Optional, List
from models import MediaResponse, MediaUpdate, MediaListResponse
from auth import get_current_user_id
from database import cosmos_db
from storage import blob_storage
from utils import validate_file_type, validate_file_size, generate_thumbnail, notify_logic_app
from datetime import datetime
import uuid
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["Media Management"])


@router.post("", response_model=MediaResponse, status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_id),
):
    """
    Upload a new image or video file
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        # Validate file type
        content_flavor = validate_file_type(file)

        # Validate file size
        payload_size = validate_file_size(file)

        # Parse tags if provided
        label_bundle = None
        if tags:
            try:
                label_bundle = json.loads(tags)
                if not isinstance(label_bundle, list):
                    raise ValueError("Tags must be an array")
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid tags format. Must be a JSON array.",
                )

        # Read file content
        raw_buffer = await file.read()
        await file.seek(0)

        # Upload to blob storage
        blob_handle, blob_pointer = blob_storage.upload_file(
            file.file, user_id, file.filename, file.content_type
        )

        # Generate thumbnail for images
        thumb_pointer = None
        if content_flavor == "image":
            thumb_bytes = generate_thumbnail(raw_buffer)
            if thumb_bytes:
                try:
                    import io
                    thumb_stream = io.BytesIO(thumb_bytes)
                    _, thumb_pointer = blob_storage.upload_file(
                        thumb_stream,
                        user_id,
                        f"thumb_{file.filename}",
                        "image/jpeg",
                    )
                except Exception as e:
                    logger.warning(f"Failed to upload thumbnail: {e}")

        # Create media document
        artifact_id = str(uuid.uuid4())
        timestamp_now = datetime.utcnow().isoformat()
        artifact_record = {
            "id": artifact_id,
            "userId": user_id,
            "fileName": blob_handle,
            "originalFileName": file.filename,
            "mediaType": content_flavor,
            "fileSize": payload_size,
            "mimeType": file.content_type,
            "blobUrl": blob_pointer,
            "thumbnailUrl": thumb_pointer,
            "description": description,
            "tags": label_bundle,
            "uploadedAt": timestamp_now,
            "updatedAt": timestamp_now,
        }

        # Save to database
        created_record = cosmos_db.create_media(artifact_record)

        # Return response
        return MediaResponse(**created_record)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload media: {str(e)}",
        )


@router.get("/search", response_model=MediaListResponse, status_code=status.HTTP_200_OK)
async def search_media(
    query: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
):
    """
    Search media files by filename, description, or tags
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        items, total = cosmos_db.search_media(
            user_id=user_id, query=query, page=page, page_size=pageSize
        )

        shaped_items = [MediaResponse(**item) for item in items]

        return MediaListResponse(
            items=shaped_items, total=total, page=page, pageSize=pageSize
        )

    except Exception as e:
        logger.error(f"Search media error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search media",
        )


@router.get("", response_model=MediaListResponse, status_code=status.HTTP_200_OK)
async def get_media_list(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    mediaType: Optional[str] = Query(None, regex="^(image|video)$"),
    user_id: str = Depends(get_current_user_id),
):
    """
    Retrieve paginated list of user's media files
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        items, total = cosmos_db.get_user_media(
            user_id=user_id, page=page, page_size=pageSize, media_type=mediaType
        )

        shaped_items = [MediaResponse(**item) for item in items]

        return MediaListResponse(
            items=shaped_items, total=total, page=page, pageSize=pageSize
        )

    except Exception as e:
        logger.error(f"Get media list error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve media list",
        )


@router.get("/{media_id}", response_model=MediaResponse, status_code=status.HTTP_200_OK)
async def get_media_by_id(
    media_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Retrieve details of a specific media file
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        media_record = cosmos_db.get_media_by_id(media_id, user_id)

        if not media_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Media not found"
            )

        # Verify ownership
        if media_record["userId"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to access this media",
            )

        return MediaResponse(**media_record)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get media error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve media",
        )


@router.put("/{media_id}", response_model=MediaResponse, status_code=status.HTTP_200_OK)
async def update_media_metadata(
    media_id: str,
    update_data: MediaUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """
    Update description and tags of a media file
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        # Get existing media
        stored_entry = cosmos_db.get_media_by_id(media_id, user_id)

        if not stored_entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Media not found"
            )

        # Verify ownership
        if stored_entry["userId"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to update this media",
            )

        # Prepare updates
        changeset = {"updatedAt": datetime.utcnow().isoformat()}

        if update_data.description is not None:
            changeset["description"] = update_data.description

        if update_data.tags is not None:
            changeset["tags"] = update_data.tags

        # Update in database
        patched_entry = cosmos_db.update_media(media_id, user_id, changeset)

        return MediaResponse(**patched_entry)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        )
    except Exception as e:
        logger.error(f"Update media error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update media",
        )


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Delete a media file and its metadata
    """
    # Notify Logic App
    await notify_logic_app()

    try:
        # Get existing media
        stored_entry = cosmos_db.get_media_by_id(media_id, user_id)

        if not stored_entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Media not found"
            )

        # Verify ownership
        if stored_entry["userId"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to delete this media",
            )

        # Delete from blob storage
        blob_storage.delete_file(stored_entry["fileName"])

        # Delete thumbnail if exists
        if stored_entry.get("thumbnailUrl"):
            # Extract blob name from thumbnail URL
            try:
                thumb_blob_name = stored_entry["fileName"].replace(
                    stored_entry["originalFileName"].split("/")[-1],
                    f"thumb_{stored_entry['originalFileName'].split('/')[-1]}",
                )
                blob_storage.delete_file(thumb_blob_name)
            except Exception as e:
                logger.warning(f"Failed to delete thumbnail: {e}")

        # Delete from database
        cosmos_db.delete_media(media_id, user_id)

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete media error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete media",
        )
