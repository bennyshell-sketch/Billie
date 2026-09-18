"""Voice endpoints: transcribe on-device, speak through ElevenLabs, clone once."""

from __future__ import annotations

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth
from billie.confirmations import ConfirmationError
from billie.permissions import VOICE_CLONE, VOICE_SPEAK
from billie.schemas import CloneVoiceRequest, RejectRequest, SpeakRequest
from billie.voice.speech import SpeechError, SpeechNotConfigured
from billie.voice.transcribe import TranscriptionError

router = APIRouter(prefix="/api/voice", tags=["voice"], dependencies=[Depends(require_auth)])


def _voice(assistant: Assistant):
    if assistant.voice is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice is not available in this build.",
        )
    return assistant.voice


def _handle(error: Exception) -> HTTPException:
    if isinstance(error, SpeechNotConfigured):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, (SpeechError, TranscriptionError, ConfirmationError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


@router.get("/status")
async def voice_status(assistant: Assistant = Depends(get_assistant)) -> dict:
    return _voice(assistant).status()


# --- speech in ------------------------------------------------------------


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    """Turn a recording into text, on this machine. The audio is not kept."""
    voice = _voice(assistant)
    data = await audio.read()
    try:
        # Whisper is CPU-bound and blocking, so it runs off the event loop.
        result = await to_thread.run_sync(
            lambda: voice.transcribe(data, language=language or None)
        )
    except TranscriptionError as exc:
        raise _handle(exc)
    return result.to_dict()


# --- speech out -----------------------------------------------------------


@router.get("/voices")
async def voices(assistant: Assistant = Depends(get_assistant)) -> dict:
    try:
        found = await _voice(assistant).voices()
    except SpeechError as exc:
        raise _handle(exc)
    return {"voices": [voice.to_dict() for voice in found]}


@router.post("/speak")
async def speak(
    payload: SpeakRequest, assistant: Assistant = Depends(get_assistant)
) -> Response:
    """Send text to ElevenLabs and return the audio. Needs voice.speak."""
    try:
        result = await _voice(assistant).speak(payload.text, voice_id=payload.voice_id)
    except SpeechError as exc:
        raise _handle(exc)
    return Response(
        content=result.audio,
        media_type=result.media_type,
        headers={
            "X-Billie-Voice-Id": result.voice_id,
            "X-Billie-Characters": str(result.characters),
            "Cache-Control": "no-store",
        },
    )


# --- cloning --------------------------------------------------------------


@router.get("/samples")
async def samples(assistant: Assistant = Depends(get_assistant)) -> dict:
    voice = _voice(assistant)
    return {"samples_dir": str(voice.samples_dir), "samples": voice.list_samples()}


@router.post("/clone/propose")
async def propose_clone(
    payload: CloneVoiceRequest, assistant: Assistant = Depends(get_assistant)
) -> dict:
    """Prepare a clone. Nothing is uploaded until this is approved."""
    try:
        action = _voice(assistant).propose_clone(
            payload.name,
            payload.samples,
            i_am_the_speaker=payload.i_am_the_speaker,
            description=payload.description,
        )
    except (SpeechError, ConfirmationError) as exc:
        raise _handle(exc)
    return {"action": action.to_dict(), "requires_permission": VOICE_CLONE}


@router.get("/actions")
async def actions(assistant: Assistant = Depends(get_assistant)) -> dict:
    return {
        "actions": [
            action.to_dict() for action in assistant.confirmations.pending("voice.")
        ]
    }


@router.post("/actions/{action_id}/approve")
async def approve(action_id: str, assistant: Assistant = Depends(get_assistant)) -> dict:
    """The only endpoint that uploads your recordings anywhere."""
    assistant.permissions.require(VOICE_CLONE)
    try:
        return await assistant.confirmations.approve(action_id)
    except (ConfirmationError, SpeechError) as exc:
        raise _handle(exc)


@router.post("/actions/{action_id}/reject")
async def reject(
    action_id: str,
    payload: RejectRequest | None = None,
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    try:
        return assistant.confirmations.reject(action_id, payload.reason if payload else "")
    except ConfirmationError as exc:
        raise _handle(exc)


@router.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str, assistant: Assistant = Depends(get_assistant)) -> dict:
    try:
        return await _voice(assistant).delete_voice(voice_id)
    except SpeechError as exc:
        raise _handle(exc)
