import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


logger = logging.getLogger(__name__)

# As of March 25, 2026, Google's official Gemini API docs list gemini-2.5-pro
# as the stable Pro model. Keep these overrideable so deployment can switch later
# without another code change.
DEFAULT_GEMINI_MODEL = os.environ.get("YOUTUBE_QUIZ_GEMINI_MODEL", "gemini-2.5-pro")
DEFAULT_GEMINI_VIDEO_MODEL = os.environ.get(
    "YOUTUBE_QUIZ_GEMINI_VIDEO_MODEL", DEFAULT_GEMINI_MODEL
)
GEMINI_API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

DEFAULT_LANGUAGE_PREFERENCES = ["en", "en-US", "en-GB"]

# Gemini 2.5 Pro paid-tier Developer API pricing, configurable via env vars so
# Render can stay accurate if Google updates rates later.
INPUT_RATE_UNDER_200K = float(
    os.environ.get("YOUTUBE_QUIZ_INPUT_RATE_UNDER_200K_USD_PER_MILLION", "1.25")
)
INPUT_RATE_OVER_200K = float(
    os.environ.get("YOUTUBE_QUIZ_INPUT_RATE_OVER_200K_USD_PER_MILLION", "2.50")
)
OUTPUT_RATE_UNDER_200K = float(
    os.environ.get("YOUTUBE_QUIZ_OUTPUT_RATE_UNDER_200K_USD_PER_MILLION", "10.00")
)
OUTPUT_RATE_OVER_200K = float(
    os.environ.get("YOUTUBE_QUIZ_OUTPUT_RATE_OVER_200K_USD_PER_MILLION", "15.00")
)
YOUTUBE_URL_PREVIEW_COST_DISPLAY = os.environ.get(
    "YOUTUBE_QUIZ_YOUTUBE_URL_PREVIEW_DISPLAY", "[$0.00 preview]"
)

QUIZ_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "options": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                    "correctAnswer": {"type": "STRING"},
                    "explanation": {"type": "STRING"},
                },
                "required": [
                    "question",
                    "options",
                    "correctAnswer",
                    "explanation",
                ],
            },
        },
    },
    "required": ["title", "questions"],
}


@dataclass
class TranscriptPayload:
    video_id: str
    text: str
    language: str
    language_code: str
    is_generated: bool
    segment_count: int


class YouTubeQuizError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def extract_youtube_video_id(raw_url_or_id: str) -> str:
    candidate = (raw_url_or_id or "").strip()
    if not candidate:
        raise YouTubeQuizError("Please enter a YouTube URL.", status_code=400)

    if re.fullmatch(r"[\w-]{11}", candidate):
        return candidate

    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if hostname in {"youtu.be", "www.youtu.be"} and path_parts:
        return path_parts[0]

    if hostname.endswith("youtube.com") or hostname.endswith("youtube-nocookie.com"):
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        if query_id:
            return query_id

        for prefix in ("shorts", "embed", "live", "v"):
            if prefix in path_parts:
                idx = path_parts.index(prefix)
                if idx + 1 < len(path_parts):
                    return path_parts[idx + 1]

    raise YouTubeQuizError("That does not look like a valid YouTube URL.", status_code=400)


def _clean_transcript_text(text: str) -> str:
    text_no_timestamps = re.sub(r"\d{2}:\d{2}:\d{2}[.,]\d{3}", "", text)
    cleaned_text = re.sub(r"\s+", " ", text_no_timestamps)
    return cleaned_text.strip()


def fetch_transcript(video_id: str) -> TranscriptPayload:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise YouTubeQuizError(
            "The transcript dependency is not installed on the server.",
            status_code=500,
        ) from exc

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except Exception as exc:
        logger.exception("Failed to list transcripts for video %s", video_id)
        raise YouTubeQuizError(
            "Unable to read captions for that video. Make sure it is public and has subtitles.",
            status_code=400,
        ) from exc

    transcript = None
    lookup_attempts = (
        "find_manually_created_transcript",
        "find_generated_transcript",
        "find_transcript",
    )
    for method_name in lookup_attempts:
        finder = getattr(transcript_list, method_name, None)
        if not finder:
            continue
        try:
            transcript = finder(DEFAULT_LANGUAGE_PREFERENCES)
            if transcript:
                break
        except Exception:
            continue

    if transcript is None:
        try:
            transcript = next(iter(transcript_list))
        except StopIteration as exc:
            raise YouTubeQuizError(
                "No transcript tracks were found for that video.",
                status_code=400,
            ) from exc

    if not str(transcript.language_code).startswith("en") and getattr(
        transcript, "is_translatable", False
    ):
        try:
            transcript = transcript.translate("en")
        except Exception:
            logger.info(
                "Transcript for %s could not be translated to English; using %s",
                video_id,
                transcript.language_code,
            )

    try:
        fetched = transcript.fetch()
    except Exception as exc:
        logger.exception("Failed to fetch transcript for video %s", video_id)
        raise YouTubeQuizError(
            "The transcript was found, but the caption text could not be fetched.",
            status_code=400,
        ) from exc

    snippets = getattr(fetched, "snippets", fetched)
    text_parts = []
    for snippet in snippets:
        if hasattr(snippet, "text"):
            text_parts.append(snippet.text)
        elif isinstance(snippet, dict):
            text_parts.append(snippet.get("text", ""))

    transcript_text = _clean_transcript_text(" ".join(part for part in text_parts if part))
    if not transcript_text:
        raise YouTubeQuizError(
            "That video has captions, but the transcript text came back empty.",
            status_code=400,
        )

    return TranscriptPayload(
        video_id=video_id,
        text=transcript_text,
        language=getattr(transcript, "language", "Unknown"),
        language_code=getattr(transcript, "language_code", "unknown"),
        is_generated=bool(getattr(transcript, "is_generated", False)),
        segment_count=len(text_parts),
    )


def _build_generation_prompt(transcript_text: str) -> str:
    return (
        "Based solely on the transcript below, first create a concise, descriptive title "
        "for the subject matter (5-7 words). Then generate exactly 30 board-exam-level "
        "multiple-choice questions that probe deep factual and conceptual mastery of the "
        "material. Questions must test understanding of facts and concepts as they apply "
        "in general contexts, not recall of the transcript's exact wording.\n\n"
        'For each question, include exactly these four properties:\n'
        '1. "question": the question stem, phrased like a high-level exam item.\n'
        '2. "options": an array of four distinct answer strings.\n'
        '3. "correctAnswer": the one option that is correct and matches exactly one entry in "options".\n'
        '4. "explanation": a brief, board-style rationale grounded in the transcript.\n\n'
        'Format the output as a single JSON object with exactly two top-level properties: "title" and "questions".\n'
        'Do not include markdown, code fences, or commentary.\n\n'
        "Transcript:\n"
        f"{transcript_text}"
    )


def _build_video_generation_prompt() -> str:
    return (
        "Based solely on the attached YouTube video, including its spoken content and any "
        "clearly inferable instructional context, first create a concise, descriptive title "
        "for the subject matter (5-7 words). Then generate exactly 30 board-exam-level "
        "multiple-choice questions that probe deep factual and conceptual mastery of the "
        "material. Questions must test understanding of facts and concepts as they apply "
        "in general contexts, not recall of the video's exact wording.\n\n"
        'For each question, include exactly these four properties:\n'
        '1. "question": the question stem, phrased like a high-level exam item.\n'
        '2. "options": an array of four distinct answer strings.\n'
        '3. "correctAnswer": the one option that is correct and matches exactly one entry in "options".\n'
        '4. "explanation": a brief, board-style rationale grounded in the video content.\n\n'
        'Format the output as a single JSON object with exactly two top-level properties: "title" and "questions".\n'
        'Do not include markdown, code fences, or commentary.'
    )


def _extract_json_text(api_response: dict[str, Any]) -> str:
    for candidate in api_response.get("candidates", []):
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        text_chunks = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("text")
        ]
        if text_chunks:
            return "".join(text_chunks).strip()

    prompt_feedback = api_response.get("promptFeedback")
    if prompt_feedback:
        raise YouTubeQuizError(
            f"Gemini did not return a quiz. Prompt feedback: {prompt_feedback}",
            status_code=502,
        )

    raise YouTubeQuizError("Gemini returned an empty response.", status_code=502)


def _parse_quiz_json(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Gemini JSON: %s", cleaned[:500])
        raise YouTubeQuizError(
            "Gemini returned malformed quiz JSON.",
            status_code=502,
        ) from exc


def _normalize_question(question: Any, index: int) -> dict[str, Any]:
    if not isinstance(question, dict):
        raise YouTubeQuizError(
            f"Question {index + 1} is not an object.",
            status_code=502,
        )

    normalized = {
        "question": str(question.get("question", "")).strip(),
        "options": [str(option).strip() for option in question.get("options", [])],
        "correctAnswer": str(question.get("correctAnswer", "")).strip(),
        "explanation": str(question.get("explanation", "")).strip(),
    }

    if not normalized["question"]:
        raise YouTubeQuizError(f"Question {index + 1} is missing text.", status_code=502)
    if len(normalized["options"]) != 4:
        raise YouTubeQuizError(
            f"Question {index + 1} must have exactly four options.",
            status_code=502,
        )
    if len({option.lower() for option in normalized["options"]}) != 4:
        raise YouTubeQuizError(
            f"Question {index + 1} has duplicate options.",
            status_code=502,
        )

    if normalized["correctAnswer"] not in normalized["options"]:
        matches = [
            option
            for option in normalized["options"]
            if option.lower() == normalized["correctAnswer"].lower()
        ]
        if len(matches) == 1:
            normalized["correctAnswer"] = matches[0]
        else:
            raise YouTubeQuizError(
                f"Question {index + 1} has a correct answer that does not match its options.",
                status_code=502,
            )

    if not normalized["explanation"]:
        normalized["explanation"] = "No explanation provided."

    return normalized


def normalize_quiz_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    title = str(raw_payload.get("title", "")).strip()
    questions = raw_payload.get("questions")

    if not title:
        raise YouTubeQuizError("Gemini did not return a quiz title.", status_code=502)
    if not isinstance(questions, list) or not questions:
        raise YouTubeQuizError("Gemini did not return any quiz questions.", status_code=502)

    normalized_questions = [
        _normalize_question(question, index)
        for index, question in enumerate(questions[:30])
    ]

    if len(normalized_questions) < 30:
        raise YouTubeQuizError(
            f"Gemini returned only {len(normalized_questions)} questions instead of 30.",
            status_code=502,
        )

    return {
        "title": title,
        "questions": normalized_questions,
    }


def _post_gemini_request(
    request_payload: dict[str, Any], model: str
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise YouTubeQuizError(
            "GEMINI_API_KEY is not configured on the server.",
            status_code=500,
        )

    url = GEMINI_API_URL_TEMPLATE.format(model=model, api_key=api_key)
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=request_payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        logger.exception("Gemini request failed")
        raise YouTubeQuizError(
            "Failed to contact Gemini while generating the quiz.",
            status_code=502,
        ) from exc

    response_text = response.text
    if not response.ok:
        logger.error("Gemini error response: %s", response_text[:1000])
        message = "Gemini rejected the quiz generation request."
        try:
            error_payload = response.json()
            message = error_payload.get("error", {}).get("message") or message
        except ValueError:
            pass
        raise YouTubeQuizError(
            message,
            status_code=502,
        )

    try:
        return response.json()
    except ValueError as exc:
        logger.error("Non-JSON Gemini response: %s", response_text[:1000])
        raise YouTubeQuizError(
            "Gemini returned a non-JSON API response.",
            status_code=502,
        ) from exc


def _parse_quiz_response(
    response_json: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_quiz_text = _extract_json_text(response_json)
    parsed_quiz = _parse_quiz_json(raw_quiz_text)
    normalized_quiz = normalize_quiz_payload(parsed_quiz)
    usage_metadata = response_json.get("usageMetadata") or {}
    return normalized_quiz, usage_metadata


def call_gemini_for_quiz_from_transcript(
    transcript_text: str, model: str = DEFAULT_GEMINI_MODEL
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You are an expert exam writer. Use only the provided transcript. "
                        "Return strict JSON that matches the requested schema."
                    )
                }
            ]
        },
        "contents": [
            {
                "parts": [
                    {
                        "text": _build_generation_prompt(transcript_text)
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.35,
            "response_mime_type": "application/json",
            "response_schema": QUIZ_RESPONSE_SCHEMA,
        },
    }
    response_json = _post_gemini_request(request_payload, model=model)
    return _parse_quiz_response(response_json)


def call_gemini_for_quiz_from_youtube_url(
    youtube_url: str, model: str = DEFAULT_GEMINI_VIDEO_MODEL
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You are an expert exam writer. Use only the attached YouTube video. "
                        "Return strict JSON that matches the requested schema."
                    )
                }
            ]
        },
        "contents": [
            {
                "parts": [
                    {
                        "text": _build_video_generation_prompt()
                    },
                    {
                        "file_data": {
                            "file_uri": youtube_url
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.35,
            "response_mime_type": "application/json",
            "response_schema": QUIZ_RESPONSE_SCHEMA,
        },
    }
    response_json = _post_gemini_request(request_payload, model=model)
    return _parse_quiz_response(response_json)


def estimate_cost_from_usage(usage_metadata: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = int(usage_metadata.get("promptTokenCount") or 0)
    candidate_tokens = int(usage_metadata.get("candidatesTokenCount") or 0)
    total_tokens = int(usage_metadata.get("totalTokenCount") or (prompt_tokens + candidate_tokens))
    thoughts_tokens = int(usage_metadata.get("thoughtsTokenCount") or 0)

    if prompt_tokens <= 200000:
        input_rate = INPUT_RATE_UNDER_200K
        output_rate = OUTPUT_RATE_UNDER_200K
    else:
        input_rate = INPUT_RATE_OVER_200K
        output_rate = OUTPUT_RATE_OVER_200K

    estimated_cost = (prompt_tokens / 1_000_000 * input_rate) + (
        candidate_tokens / 1_000_000 * output_rate
    )

    return {
        "usd": round(estimated_cost, 6),
        "display": format_cost_display(estimated_cost),
        "promptTokens": prompt_tokens,
        "candidateTokens": candidate_tokens,
        "thoughtsTokens": thoughts_tokens,
        "totalTokens": total_tokens,
        "inputRatePerMillionUsd": input_rate,
        "outputRatePerMillionUsd": output_rate,
    }


def build_preview_cost_estimate(usage_metadata: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = int(usage_metadata.get("promptTokenCount") or 0)
    candidate_tokens = int(usage_metadata.get("candidatesTokenCount") or 0)
    total_tokens = int(usage_metadata.get("totalTokenCount") or (prompt_tokens + candidate_tokens))
    thoughts_tokens = int(usage_metadata.get("thoughtsTokenCount") or 0)

    return {
        "usd": 0.0,
        "display": YOUTUBE_URL_PREVIEW_COST_DISPLAY,
        "promptTokens": prompt_tokens,
        "candidateTokens": candidate_tokens,
        "thoughtsTokens": thoughts_tokens,
        "totalTokens": total_tokens,
        "pricingNote": "Public YouTube URL input is currently marked no-charge preview in Gemini docs.",
    }


def format_cost_display(estimated_cost: float) -> str:
    if estimated_cost >= 1:
        cost_text = f"${estimated_cost:.2f}"
    elif estimated_cost >= 0.1:
        cost_text = f"${estimated_cost:.3f}"
    else:
        cost_text = f"${estimated_cost:.4f}"
    return f"[{cost_text} est]"


def serialize_transcript_payload(transcript: TranscriptPayload) -> dict[str, Any]:
    return {
        "language": transcript.language,
        "languageCode": transcript.language_code,
        "isGenerated": transcript.is_generated,
        "segmentCount": transcript.segment_count,
    }


def fetch_transcript_from_youtube_url(youtube_url: str) -> dict[str, Any]:
    video_id = extract_youtube_video_id(youtube_url)
    transcript = fetch_transcript(video_id)
    return {
        "videoId": transcript.video_id,
        "transcript": serialize_transcript_payload(transcript),
        "transcriptText": transcript.text,
    }


def generate_quiz_from_youtube_url(
    youtube_url: str,
    source_mode: str = "video",
    transcript_text: str | None = None,
) -> dict[str, Any]:
    video_id = extract_youtube_video_id(youtube_url)

    if source_mode == "transcript":
        transcript = None
        normalized_transcript_text = (transcript_text or "").strip()
        if not normalized_transcript_text:
            transcript = fetch_transcript(video_id)
            normalized_transcript_text = transcript.text

        quiz_payload, usage_metadata = call_gemini_for_quiz_from_transcript(
            normalized_transcript_text
        )
        cost_estimate = estimate_cost_from_usage(usage_metadata)
        transcript_metadata = (
            serialize_transcript_payload(transcript)
            if transcript is not None
            else None
        )
        model_used = DEFAULT_GEMINI_MODEL
    else:
        quiz_payload, usage_metadata = call_gemini_for_quiz_from_youtube_url(youtube_url)
        cost_estimate = build_preview_cost_estimate(usage_metadata)
        transcript_metadata = None
        model_used = DEFAULT_GEMINI_VIDEO_MODEL

    return {
        "quiz": quiz_payload,
        "model": model_used,
        "videoId": video_id,
        "sourceMode": source_mode,
        "transcript": transcript_metadata,
        "usage": cost_estimate,
    }
