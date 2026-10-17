#!/usr/bin/env python3
"""Single-model OmniVoice Studio HTTP application (pinned upstream API)."""

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
import fcntl
import importlib.util
import logging
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Literal
import uuid
import wave

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.exceptions import HTTPException as StarletteHTTPException

MAX_REFERENCE_BYTES = 32 * 1024 * 1024
LOGGER = logging.getLogger("uvicorn.error")
MAX_PLAN_BYTES = 65536
MAX_REQUEST_BYTES = MAX_REFERENCE_BYTES + MAX_PLAN_BYTES + 65536
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
JOB_TTL = 30 * 60
MAX_FINISHED_JOBS = 8
LIMITS = {
    "max_phrases": 24,
    "max_text_chars": 6000,
    "max_reference_bytes": MAX_REFERENCE_BYTES,
    "max_reference_seconds": 60,
}
# Read only the upstream's data module: metadata mode must not import torch/models.
_package = importlib.util.find_spec("omnivoice")
if _package is None or not _package.submodule_search_locations:
    raise RuntimeError("OmniVoice package is required")
_lang_map = runpy.run_path(str(Path(next(iter(_package.submodule_search_locations))) / "utils" / "lang_map.py"))
LANGUAGES = ["Auto"] + sorted(_lang_map["lang_display_name"](name) for name in _lang_map["LANG_NAMES"])
LANGUAGE_SET = frozenset(LANGUAGES)
del _lang_map


class Phrase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=2000)
    language: str
    whisper: bool
    pitch: Literal["ปกติ", "ต่ำ", "สูง"]
    speed: float = Field(ge=0.7, le=1.3)
    pause: float

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("กรุณากรอกข้อความที่จะสร้างเสียง")
        return value

    @field_validator("language")
    @classmethod
    def supported_language(cls, value):
        if value not in LANGUAGE_SET:
            raise ValueError("กรุณาเลือกภาษาจากรายการที่รองรับ")
        return value

    @field_validator("pause")
    @classmethod
    def supported_pause(cls, value):
        if value not in (0, 0.3, 0.5, 1):
            raise ValueError("ระยะเว้นวรรคต้องเป็น 0, 0.3, 0.5 หรือ 1 วินาที")
        return value


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["clone", "design"]
    gender: Literal["unspecified", "female", "male"]
    reference_text: str = Field(max_length=6000)
    phrases: list[Phrase] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def total_text(self):
        if sum(len(phrase.text) for phrase in self.phrases) > LIMITS["max_text_chars"]:
            raise ValueError("ข้อความรวมต้องไม่เกิน 6,000 ตัวอักษร")
        return self


@dataclass
class Job:
    id: str
    directory: Path
    total: int
    status: str = "running"
    completed: int = 0
    error: str | None = None
    finished: float | None = None
    readers: int = 0

    def public(self):
        result = {"id": self.id, "status": self.status, "completed": self.completed, "total": self.total}
        if self.error:
            result["error"] = self.error
        if self.status == "done":
            result["audio_url"] = f"/api/jobs/{self.id}/audio"
        return result


async def receive_form(request: Request, directory: Path):
    """Stream directly into the private job dir; never spool user audio elsewhere."""
    media_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if media_type != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise HTTPException(400, "กรุณาส่งแผนและไฟล์เสียงแบบ multipart/form-data")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            raise HTTPException(400, "ขนาดคำขอไม่ถูกต้อง") from None
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise HTTPException(413, "ไฟล์เสียงต้องมีขนาดไม่เกิน 32 MiB")

    fields = set()
    headers = {}
    header_name = bytearray()
    header_value = bytearray()
    header_size = 0
    part_name = None
    plan_bytes = bytearray()
    audio_file = None
    audio_size = 0
    ended = False
    part_count = 0

    def part_begin():
        nonlocal header_size, part_name, part_count
        part_count += 1
        if part_count > 2:
            raise HTTPException(400, "ส่งได้เฉพาะ plan และ reference_audio อย่างละหนึ่งรายการ")
        headers.clear()
        header_name.clear()
        header_value.clear()
        header_size = 0
        part_name = None

    def header_field(data, start, end):
        nonlocal header_size
        header_size += end - start
        if header_size > 8192:
            raise HTTPException(400, "ส่วนหัวไฟล์เสียงยาวเกินไป")
        header_name.extend(data[start:end])

    def header_data(data, start, end):
        nonlocal header_size
        header_size += end - start
        if header_size > 8192:
            raise HTTPException(400, "ส่วนหัวไฟล์เสียงยาวเกินไป")
        header_value.extend(data[start:end])

    def header_end():
        key = bytes(header_name).lower()
        if key in headers:
            raise HTTPException(400, "ส่วนหัวแบบฟอร์มซ้ำกัน")
        headers[key] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished():
        nonlocal part_name, audio_file
        disposition, values = parse_options_header(headers.get(b"content-disposition", b""))
        name = values.get(b"name")
        if disposition != b"form-data" or name not in (b"plan", b"reference_audio") or name in fields:
            raise HTTPException(400, "แบบฟอร์มต้องมี plan และ reference_audio ไม่ซ้ำกัน")
        fields.add(name)
        part_name = name
        if name == b"plan":
            if b"filename" in values:
                raise HTTPException(400, "plan ต้องเป็นข้อความ JSON ไม่ใช่ไฟล์")
        else:
            if not values.get(b"filename"):
                raise HTTPException(400, "กรุณาแนบไฟล์เสียงอ้างอิงที่ถูกต้อง")
            audio_file = (directory / "upload").open("xb")

    def part_data(data, start, end):
        nonlocal audio_size
        if part_name == b"plan":
            if len(plan_bytes) + end - start > MAX_PLAN_BYTES:
                raise HTTPException(413, "แผนข้อความยาวเกินไป กรุณาลดข้อความ")
            plan_bytes.extend(data[start:end])
        elif part_name == b"reference_audio":
            audio_size += end - start
            if audio_size > MAX_REFERENCE_BYTES:
                raise HTTPException(413, "ไฟล์เสียงต้องมีขนาดไม่เกิน 32 MiB")
            audio_file.write(data[start:end])
        else:
            raise HTTPException(400, "แบบฟอร์มไม่ถูกต้อง")

    def form_end():
        nonlocal ended
        ended = True

    parser = MultipartParser(boundary, {
        "on_part_begin": part_begin, "on_header_field": header_field,
        "on_header_value": header_data, "on_header_end": header_end,
        "on_headers_finished": headers_finished, "on_part_data": part_data,
        "on_end": form_end,
    })
    try:
        received = 0
        async with asyncio.timeout(120):
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "ไฟล์เสียงต้องมีขนาดไม่เกิน 32 MiB")
                parser.write(chunk)
            parser.finalize()
        if not ended or b"plan" not in fields:
            raise HTTPException(400, "แบบฟอร์มไม่ครบ กรุณาส่ง plan และลองอีกครั้ง")
        if b"reference_audio" in fields and audio_size == 0:
            raise HTTPException(400, "ไฟล์เสียงว่าง กรุณาเลือกไฟล์ใหม่")
        try:
            plan = Plan.model_validate_json(plan_bytes)
        except ValidationError as exc:
            error = exc.errors(include_input=False, include_url=False)[0]
            message = error.get("msg", "")
            if message.startswith("Value error, "):
                detail = message.removeprefix("Value error, ")
            else:
                detail = "แผนไม่ถูกต้อง: ตรวจสอบข้อความ (1–24 วลี วลีละไม่เกิน 2,000 ตัวอักษร) ภาษา ความเร็ว (0.7–1.3) และตัวเลือกเสียง"
            raise HTTPException(422, detail) from None
        return plan, b"reference_audio" in fields
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(400, "อัปโหลดหมดเวลา กรุณาลองส่งไฟล์อีกครั้ง") from None
    except Exception:
        raise HTTPException(400, "อ่านแบบฟอร์มไม่ได้ กรุณาเลือกไฟล์และส่งใหม่") from None
    finally:
        if audio_file is not None:
            audio_file.close()
        parser.close()


def normalize_reference(directory: Path, sampling_rate: int):
    output = directory / "reference.wav"
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,mp3,ogg,matroska,webm,mov,aac,flac,aiff,au",
            "-threads", "1", "-i", str(directory / "upload"),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", "61",
            "-ac", "1", "-ar", str(sampling_rate), "-c:a", "pcm_s16le",
            "-threads", "1", "-f", "wav", str(output),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60, check=True)
        with wave.open(str(output), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if duration <= 0:
                raise HTTPException(400, "ไฟล์ไม่มีเสียง กรุณาบันทึกหรือเลือกไฟล์ใหม่")
            if duration > 60:
                raise HTTPException(413, "เสียงอ้างอิงต้องยาวไม่เกิน 60 วินาที กรุณาตัดไฟล์ก่อนอัปโหลด")
    except HTTPException:
        raise
    except (subprocess.SubprocessError, OSError, wave.Error):
        raise HTTPException(400, "อ่านเสียงไม่ได้ รองรับ WAV, MP3, M4A, FLAC, OGG และ WebM กรุณาเลือกไฟล์เสียงที่ไม่เสียหาย") from None
    finally:
        (directory / "upload").unlink(missing_ok=True)
    return output


@contextmanager
def job_workspace():
    base = Path(os.environ.get("OMNIVOICE_TEMP_DIR") or Path(tempfile.gettempdir()) / "omnivoice-studio")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / ".studio.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("มีแอปอื่นใช้พื้นที่ชั่วคราวนี้อยู่ กรุณาใช้หนึ่งโปรเซสต่อโฟลเดอร์") from None
        for stale in base.glob("omnivoice-studio-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        with tempfile.TemporaryDirectory(prefix="omnivoice-studio-", dir=base) as temporary:
            yield Path(temporary)


def create_app(args):
    model = None
    sampling_rate = 24000
    jobs = {}
    state_lock = threading.Lock()
    active = None
    tasks = set()
    root = None

    def cleanup():
        with state_lock:
            finished = sorted((job for job in jobs.values() if job.finished is not None), key=lambda job: job.finished)
            excess = max(0, len(finished) - MAX_FINISHED_JOBS)
            for index, job in enumerate(finished):
                if job.readers == 0 and (time.monotonic() - job.finished >= JOB_TTL or index < excess):
                    jobs.pop(job.id, None)
                    shutil.rmtree(job.directory, ignore_errors=True)

    async def janitor():
        while True:
            await asyncio.sleep(30)
            cleanup()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model, sampling_rate, root
        with job_workspace() as temporary:
            root = temporary
            if not args.metadata_only:
                import torch
                from omnivoice import OmniVoice
                from omnivoice.utils.common import get_best_device
                device = args.device or get_best_device()
                try:
                    model = OmniVoice.from_pretrained(
                        args.model, device_map=device,
                        dtype=torch.float32 if str(device).split(":")[0] == "cpu" else torch.float16,
                        load_asr=not args.no_asr, asr_model_name=args.asr_model,
                    )
                    model.eval()
                    sampling_rate = int(model.sampling_rate)
                except Exception as exc:
                    LOGGER.error("Model startup failed: type=%s", type(exc).__name__)
                    # Third-party exception text can contain tokens and local paths.
                    raise RuntimeError("โหลดโมเดลไม่สำเร็จ กรุณาตรวจสอบสิทธิ์เข้าถึงโมเดลและหน่วยความจำ") from None
            cleaner = asyncio.create_task(janitor())
            try:
                yield
            finally:
                cleaner.cancel()
                with suppress(asyncio.CancelledError):
                    await cleaner
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                model = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, str) and any("\u0e00" <= c <= "\u0e7f" for c in exc.detail) else "ไม่พบรายการหรือคำขอไม่ถูกต้อง กรุณาลองใหม่"
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return JSONResponse({"detail": "ข้อมูลคำขอไม่ถูกต้อง กรุณาตรวจสอบและลองใหม่"}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"detail": "เกิดข้อผิดพลาดในเซิร์ฟเวอร์ กรุณาลองใหม่"}, status_code=500)

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).with_name("index.html"), media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    async def config():
        return {"ready": model is not None, "asr_enabled": model is not None and not args.no_asr,
                "model": args.model if not Path(args.model).is_absolute() else "OmniVoice",
                "sampling_rate": sampling_rate, "languages": LANGUAGES, "limits": LIMITS}

    @app.get("/healthz")
    async def health():
        return JSONResponse({"ready": model is not None}, status_code=200 if model is not None else 503)

    def synthesize(job, plan):
        prompt = None
        phase = "reference" if plan.mode == "clone" else "generation"
        try:
            from omnivoice import OmniVoiceGenerationConfig
            import numpy as np
            if plan.mode == "clone":
                prompt = model.create_voice_clone_prompt(
                    ref_audio=str(job.directory / "reference.wav"), ref_text=plan.reference_text.strip() or None,
                )
            output = job.directory / "audio.wav"
            generation_config = OmniVoiceGenerationConfig(num_step=args.steps)
            phase = "output"
            with wave.open(str(output), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(sampling_rate)
                for index, phrase in enumerate(plan.phrases):
                    instructions = []
                    if plan.mode == "design" and plan.gender != "unspecified":
                        instructions.append("Female" if plan.gender == "female" else "Male")
                    if phrase.whisper:
                        instructions.append("Whisper")
                    if phrase.pitch != "ปกติ":
                        instructions.append("Low Pitch" if phrase.pitch == "ต่ำ" else "High Pitch")
                    kwargs = {"text": phrase.text.strip(), "language": None if phrase.language == "Auto" else phrase.language,
                              "speed": phrase.speed, "generation_config": generation_config}
                    if prompt is not None:
                        kwargs["voice_clone_prompt"] = prompt
                    if instructions:
                        kwargs["instruct"] = ", ".join(instructions)
                    phase = "generation"
                    audio = model.generate(**kwargs)
                    phase = "output"
                    waveform = np.asarray(audio[0], dtype=np.float32)
                    if waveform.ndim != 1 or waveform.size == 0 or not np.isfinite(waveform).all():
                        raise ValueError("invalid generated audio")
                    pause_samples = round(phrase.pause * sampling_rate) if index < len(plan.phrases) - 1 else 0
                    if (writer.getnframes() + waveform.size + pause_samples) * 2 > MAX_OUTPUT_BYTES:
                        raise ValueError("output limit")
                    pcm = (np.clip(waveform, -1, 1) * 32767).astype("<i2")
                    writer.writeframes(pcm.tobytes())
                    if pause_samples:
                        writer.writeframes(b"\x00\x00" * pause_samples)
                    del audio, waveform, pcm, kwargs
                    with state_lock:
                        job.completed = index + 1
            with state_lock:
                job.status = "done"
        except Exception as exc:
            LOGGER.error("Synthesis failed: job=%s phase=%s type=%s", job.id, phase, type(exc).__name__)
            (job.directory / "audio.wav").unlink(missing_ok=True)
            with state_lock:
                job.status = "error"
                job.error = "สร้างเสียงไม่สำเร็จ กรุณาลดข้อความ ตรวจสอบเสียงอ้างอิง แล้วลองใหม่ หากยังไม่สำเร็จให้ตรวจสอบหน่วยความจำของเครื่อง"
        finally:
            prompt = None
            # Only the final WAV may outlive inference; never retain voice prompts/text.
            for name in ("reference.wav", "upload"):
                (job.directory / name).unlink(missing_ok=True)
            with state_lock:
                job.finished = time.monotonic()

    async def run_job(job, plan):
        nonlocal active
        try:
            await asyncio.to_thread(synthesize, job, plan)
        finally:
            active = None
            cleanup()

    @app.post("/api/jobs", status_code=202)
    async def submit(request: Request):
        nonlocal active
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "ไม่อนุญาตให้สร้างเสียงจากเว็บไซต์อื่น กรุณาเปิด OmniVoice Studio โดยตรง")
        if request.headers.get("x-omnivoice-request") != "1":
            raise HTTPException(403, "กรุณาส่งงานจาก OmniVoice Studio หรือระบุ X-OmniVoice-Request: 1")
        try:
            identifier = str(uuid.UUID(request.headers.get("idempotency-key", "")))
        except ValueError:
            raise HTTPException(400, "กรุณาระบุ Idempotency-Key เป็น UUID ของงาน") from None
        cleanup()
        with state_lock:
            existing = jobs.get(identifier)
            if existing is not None:
                return existing.public()
        # ponytail: one process, one reserved slot; a queue is unnecessary here.
        if active is not None:
            raise HTTPException(409, "กำลังสร้างเสียงอยู่ กรุณารอให้งานปัจจุบันเสร็จก่อน")
        active = identifier
        directory = root / identifier
        scheduled = False
        try:
            directory.mkdir(mode=0o700)
            plan, has_reference = await receive_form(request, directory)
            if plan.mode == "clone" and not has_reference:
                raise HTTPException(400, "กรุณาอัปโหลดหรือบันทึกเสียงอ้างอิงสำหรับโคลนเสียง")
            if plan.mode == "design" and has_reference:
                raise HTTPException(400, "โหมดออกแบบเสียงไม่ใช้ไฟล์อ้างอิง กรุณาลบไฟล์หรือเลือกโหมดโคลนเสียง")
            if plan.mode == "clone" and args.no_asr and not plan.reference_text.strip():
                raise HTTPException(400, "เซิร์ฟเวอร์ปิดการถอดเสียงอัตโนมัติ กรุณากรอกข้อความที่พูดในเสียงอ้างอิง")
            if model is None:
                raise HTTPException(503, "โมเดลยังไม่พร้อมใช้งาน เซิร์ฟเวอร์นี้อยู่ในโหมดข้อมูลเท่านั้น")
            if has_reference:
                await asyncio.to_thread(normalize_reference, directory, sampling_rate)
            cleanup()
            job = Job(identifier, directory, len(plan.phrases))
            with state_lock:
                jobs[identifier] = job
                response = job.public()
            task = asyncio.create_task(run_job(job, plan))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            scheduled = True
            return response
        finally:
            if not scheduled:
                active = None
                shutil.rmtree(directory, ignore_errors=True)

    def find_job(identifier):
        cleanup()
        with state_lock:
            job = jobs.get(identifier)
            if job is None or (job.finished is not None and time.monotonic() - job.finished >= JOB_TTL):
                raise HTTPException(404, "ไม่พบงานหรืองานหมดอายุแล้ว กรุณาสร้างเสียงใหม่")
            return job

    @app.get("/api/jobs/{identifier}")
    async def poll(identifier: str):
        if active == identifier:
            with state_lock:
                if identifier not in jobs:
                    return {"id": identifier, "status": "running", "completed": 0, "total": 0}
        job = find_job(identifier)
        with state_lock:
            return job.public()

    @app.get("/api/jobs/{identifier}/audio")
    async def download(identifier: str):
        job = find_job(identifier)
        with state_lock:
            if job.status != "done":
                raise HTTPException(409, "เสียงยังไม่พร้อมดาวน์โหลด กรุณารอให้งานเสร็จ")
            job.readers += 1

        class Download(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    with state_lock:
                        job.readers -= 1

        return Download(job.directory / "audio.wav", media_type="audio/wav", filename="omnivoice.wav",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return app


def main():
    parser = argparse.ArgumentParser(description="OmniVoice Studio")
    parser.add_argument("--model", default="k2-fsa/OmniVoice")
    parser.add_argument("--device", default=None)
    parser.add_argument("--ip", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-asr", action="store_true")
    parser.add_argument("--asr-model", default="openai/whisper-large-v3-turbo")
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    if not 4 <= args.steps <= 64:
        parser.error("--steps must be between 4 and 64")
    import uvicorn
    uvicorn.run(create_app(args), host=args.ip, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
