import os
import re
import json
import logging
import tempfile
import genanki
from flask import Flask, request, redirect, url_for, flash, render_template_string, send_file, after_this_request

# Updated OpenAI API import and initialization.
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me") # Use env var or default

# Set up logging
logging.basicConfig(level=logging.INFO) # INFO level for deployment
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    logger.warning("OPENAI_API_KEY environment variable not set. API calls will fail.")
    # Optionally, you could prevent the app from starting or disable API features
client = OpenAI(api_key=openai_api_key)


# ----------------------------
# Helper Functions
# ----------------------------

def preprocess_transcript(text):
    """
    Remove common timestamp patterns (e.g., VTT, SRT, simple times)
    and speaker labels, then normalize whitespace.
    """
    if not text:
        return ""
    # Remove VTT timestamps like 00:00:10.440 --> 00:00:12.440
    text_no_timestamps = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}', '', text)
    # Remove SRT timestamps like 00:00:06,000 --> 00:00:12,074
    text_no_timestamps = re.sub(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}', '', text_no_timestamps)
    # Remove simple timestamps like [00:00:00] or 0:00:00.160
    text_no_timestamps = re.sub(r'\[?\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?\]?', '', text_no_timestamps)
    # Remove potential SRT index numbers at the start of lines
    text_no_indices = re.sub(r'^\d+\s*$', '', text_no_timestamps, flags=re.MULTILINE)
    # Remove common speaker labels (e.g., "Speaker 1:", "John Doe:") - adjust regex if needed
    # This one is cautious, remove speaker names followed by a colon at the start of a line or after spaces
    text_no_speakers = re.sub(r'(^|\s)[A-Za-z0-9\s_]+:\s*', r'\1', text_no_indices, flags=re.MULTILINE)
    # Remove WebVTT header and style blocks
    text_no_webvtt_meta = re.sub(r'^WEBVTT.*?\n\n', '', text_no_speakers, flags=re.DOTALL | re.IGNORECASE)
    text_no_webvtt_meta = re.sub(r'^STYLE.*?\n\n', '', text_no_webvtt_meta, flags=re.DOTALL | re.IGNORECASE)
    text_no_webvtt_meta = re.sub(r'^NOTE.*?\n\n', '', text_no_webvtt_meta, flags=re.DOTALL | re.IGNORECASE)
    # Normalize whitespace (replace multiple spaces/newlines with single space)
    cleaned_text = re.sub(r'\s+', ' ', text_no_webvtt_meta).strip()
    return cleaned_text


def chunk_text(text, max_size, min_size=200): # Increased min_size slightly
    """
    Splits text into chunks of up to max_size characters, trying to break at sentence endings.
    If a chunk is shorter than min_size and there is a previous chunk,
    it is merged with the previous chunk.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_size
        if end < len(text):
            # Try to find the last sentence-ending punctuation (. ! ?) before max_size
            last_punct = -1
            # Look backwards from end position
            search_start = max(start, end - 200) # Search in the last 200 chars for efficiency
            for punct in ['.', '!', '?']:
                try:
                    pos = text.rindex(punct, search_start, end)
                    if pos > last_punct:
                        last_punct = pos
                except ValueError:
                    continue # Punctuation not found in the search range

            if last_punct != -1: # Found suitable punctuation
                end = last_punct + 1
            else: # No punctuation found, try last space
                try:
                   last_space = text.rindex(" ", search_start, end)
                   if last_space > start: # Ensure we don't create an empty chunk
                       end = last_space + 1
                   # else: keep original 'end' if no space found either
                except ValueError:
                    # No space found, keep original 'end' - will cut mid-word if necessary
                    pass

        chunk = text[start:end].strip()

        # Merge small chunks with the previous one if possible
        if chunks and chunk and len(chunk) < min_size:
            logger.debug("Merging small chunk (len %d) with previous.", len(chunk))
            chunks[-1] += " " + chunk
            start = end # Adjust start for the next iteration
            continue # Skip appending this small chunk separately

        if chunk: # Only add non-empty chunks
             chunks.append(chunk)

        start = end # Move start for the next chunk

    logger.info("Split text into %d chunks.", len(chunks))
    return chunks


def fix_cloze_formatting(card):
    """
    Ensures that cloze deletions in the card use exactly two curly braces on each side.
    Handles cases like {c1::...}, { { c1 :: ... } }, etc.
    Also trims whitespace around the cloze number, answer, and hint.
    """
    if not isinstance(card, str): # Basic type check
        return card

    # Regex to find potential cloze patterns (flexible with spacing and brace count)
    pattern = re.compile(r"\{{1,2}\s*c(\d+)\s*::\s*(.*?)\s*(?:::\s*(.*?)\s*)?\}\}{1,2}")

    def replace_match(match):
        num = match.group(1).strip()
        answer = match.group(2).strip()
        hint = match.group(3)
        if hint:
            hint = hint.strip()
            # Ensure hint doesn't contain problematic characters for the format, basic clean
            hint = hint.replace('}}', '').replace('{{', '')
            return f"{{{{c{num}::{answer}::{hint}}}}}"
        else:
            return f"{{{{c{num}::{answer}}}}}"

    corrected_card = pattern.sub(replace_match, card)

    # Fallback for simple single braces if missed (less common with better regex)
    if "{{" not in corrected_card and "}}" not in corrected_card:
         if "{c" in corrected_card and "}" in corrected_card:
              corrected_card = corrected_card.replace("{c", "{{c").replace("}", "}}")
              # Re-run main regex to fix spacing/hints in this fallback case
              corrected_card = pattern.sub(replace_match, corrected_card)

    return corrected_card


def get_anki_cards_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o-mini"):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    """
    user_instr = ""
    if user_preferences.strip():
        user_instr = f'\nUser Request: {user_preferences.strip()}\nIf no content relevant to the user request is found in this chunk, output a dummy card in the format: "User request not found in {{{{c1::this chunk}}}}."'

    # Updated prompt with hint example and clear JSON instruction
    prompt = f"""
You are an expert at creating study flashcards in Anki using cloze deletion.
Given the transcript below, generate a list of flashcards.
Each flashcard should be a complete, self-contained sentence (or sentence fragment) containing one or more cloze deletions.
Each cloze deletion must be formatted exactly as:
  {{{{c1::hidden text}}}}
Optionally, you can add a hint like this:
  {{{{c1::hidden text::hint}}}}

Follow these formatting instructions exactly:
1. Formatting Cloze Deletions Properly
   • Cloze deletions should be written in the format:
     {{{{c1::hidden text}}}} or {{{{c1::hidden text::hint}}}}
   • Example:
     Original sentence: "Canberra is the capital of Australia."
     Cloze version: "{{{{c1::Canberra}}}} is the capital of {{{{c2::Australia}}}}."
     With hint: "{{{{c1::Canberra::Australian Capital}}}} is the capital of {{{{c2::Australia}}}}."
2. Using Multiple Cloze Deletions in One Card
   • If multiple deletions belong to the same testable concept, use the same number:
     Example: "The three branches of the U.S. government are {{{{c1::executive}}}}, {{{{c1::legislative}}}}, and {{{{c1::judicial}}}}."
   • If deletions belong to separate testable concepts, use different numbers:
     Example: "The heart has {{{{c1::four}}}} chambers and pumps blood through the {{{{c2::circulatory}}}} system."
3. Ensuring One Clear Answer
   • Avoid ambiguity—each blank should have only one reasonable answer.
   • Bad Example: "{{{{c1::He}}}} went to the store."
   • Good Example: "The mitochondria is known as the {{{{c1::powerhouse}}}} of the cell."
4. Choosing Between Fill-in-the-Blank vs. Q&A Style
   • Fill-in-the-blank format works well for quick fact recall:
         {{{{c1::Canberra}}}} is the capital of {{{{c2::Australia}}}}.
   • Q&A-style cloze deletions work better for some questions:
         What is the capital of Australia?<br><br>{{{{c1::Canberra}}}}
   • Use line breaks (<br><br>) so the answer appears on a separate line.
5. Avoiding Overly General or Basic Facts
   • Bad Example (too vague): "{{{{c1::A planet}}}} orbits a star."
   • Better Example: "{{{{c1::Jupiter}}}} is the largest planet in the solar system."
   • Focus on college-level or expert-level knowledge.
6. Using Cloze Deletion for Definitions
   • Definitions should follow the “is defined as” structure for clarity.
         Example: "A {{{{c1::pneumothorax}}}} is defined as {{{{c2::air in the pleural space}}}}."
7. Formatting Output in HTML for Readability
   • Use line breaks (<br><br>) to properly space question and answer.
         Example:
         What is the capital of Australia?<br><br>{{{{c1::Canberra}}}}
8. Summary of Key Rules
   • Keep answers concise (single words or short phrases).
   • Use different C-numbers for unrelated deletions.
   • Ensure only one correct answer per deletion.
   • Focus on college-level or expert-level knowledge.
   • Use HTML formatting for better display.
   • Use hints ({{{{c1::answer::hint}}}}) sparingly for difficult terms or when context is needed.

In addition, you must make sure to follow the following instructions:
{user_instr}
Ensure you output ONLY a valid JSON array of strings, where each string is a flashcard. Do not include any surrounding text, explanations, or markdown formatting like ```json ... ```. Just the array itself starting with [ and ending with ].

Transcript:
\"\"\"{transcript_chunk}\"\"\"
"""
    if not client.api_key:
        logger.error("OpenAI API key not configured.")
        flash("Server configuration error: OpenAI API key is missing.")
        return []
    try:
        response = client.chat.completions.create(
            model=model,
            # Consider using JSON mode if available and reliable for your model
            # response_format={ "type": "json_object" }, # Needs prompt adjustment
            messages=[
                {"role": "system", "content": "You are an expert Anki card creator outputting ONLY a valid JSON array of strings."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=2500,
            timeout=90
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for chunk: %s", result_text)

        # Attempt to extract JSON array, more robustly
        json_match = re.search(r'^\s*(\[.*\])\s*$', result_text, re.DOTALL)
        if not json_match:
            # Fallback: Find the first '[' and last ']' if not strict match
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                 json_str = result_text[start_idx : end_idx + 1]
                 logger.debug("Using fallback JSON extraction.")
            else:
                 json_str = None
        else:
            json_str = json_match.group(1)
            logger.debug("Using regex JSON extraction.")


        if json_str:
            try:
                cards = json.loads(json_str)
                if isinstance(cards, list) and all(isinstance(item, str) for item in cards):
                    fixed_cards = [fix_cloze_formatting(card) for card in cards]
                    logger.debug("Successfully parsed and fixed %d cards from extracted JSON.", len(fixed_cards))
                    return fixed_cards
                else:
                    logger.error("Extracted JSON is not a list of strings: %s", json_str)
                    flash(f"API returned unexpected JSON structure for a chunk (not a list of strings).")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error("JSON parsing error for extracted chunk: %s\nContent: %s", parse_err, json_str)
                # Provide more specific error feedback if possible
                flash(f"Failed to parse API response for a chunk (JSON error near char {parse_err.pos}). Please check logs.")
                return []
        else:
             logger.error("Could not find JSON array structure in API response: %s", result_text)
             flash("API response did not contain a recognizable JSON array for a chunk.")
             return []

    except Exception as e:
        # Catch potential OpenAI API errors (rate limits, auth, etc.) or timeouts
        logger.exception("OpenAI API error or other exception during card generation for chunk: %s", e)
        flash(f"An error occurred communicating with the AI for a chunk: {e}")
        return []

def get_all_anki_cards(transcript, user_preferences="", max_chunk_size=8000, model="gpt-4o-mini"):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk.
    Returns a combined list of all flashcards.
    """
    if not transcript or transcript.isspace():
        logger.warning("Received empty or whitespace-only transcript.")
        flash("Transcript content is empty or contains only whitespace.")
        return []
    cleaned_transcript = preprocess_transcript(transcript)
    if not cleaned_transcript:
        logger.warning("Transcript became empty after preprocessing.")
        flash("Transcript empty after cleaning. Please check format (e.g., remove only timestamps/speaker labels).")
        return []

    logger.info("Cleaned transcript length: %d", len(cleaned_transcript))
    # logger.debug("Cleaned transcript (first 200 chars): %s", cleaned_transcript[:200]) # Optional debug
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks.")
         flash("Failed to split transcript into processable chunks.")
         return []

    all_cards = []
    total_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        # Skip empty chunks that might result from splitting/merging logic
        if not chunk or chunk.isspace():
            logger.debug("Skipping empty chunk %d/%d", i+1, total_chunks)
            continue

        logger.info("Processing chunk %d/%d (size: %d chars)", i+1, total_chunks, len(chunk))
        cards_from_chunk = get_anki_cards_for_chunk(chunk, user_preferences, model=model)
        if cards_from_chunk: # Only extend if cards were successfully generated
             logger.info("Chunk %d produced %d cards.", i+1, len(cards_from_chunk))
             all_cards.extend(cards_from_chunk)
        else:
             logger.warning("Chunk %d produced no cards.", i+1)
             # Flash message is handled within get_anki_cards_for_chunk

    logger.info("Total Anki flashcards generated: %d", len(all_cards))
    if not all_cards:
        # Flash message if *no* cards were generated across *all* chunks
        flash("No Anki cards could be generated from the provided transcript and settings. Check the transcript quality, try different preferences, or check the logs.")
    return all_cards


# ----------------------------
# Interactive Mode Functions (Placeholder - Assuming similar structure)
# ----------------------------

def get_interactive_questions_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o-mini"):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of interactive multiple-choice questions.
    Each question is a JSON object with keys: "question", "options", "correctAnswer" (and optionally "explanation").
    """
    user_instr = ""
    if user_preferences.strip():
        user_instr = f'\nUser Request: {user_preferences.strip()}\nIf no content relevant to the user request is found in this chunk, output a dummy question in the required JSON format.'

    prompt = f"""
You are an expert at creating interactive multiple-choice questions for educational purposes based on a transcript.
Given the transcript below, generate a list of interactive multiple-choice questions.
Each question must be a JSON object with the following keys:
  "question": (String) The question text. Should be clear and directly related to the transcript content.
  "options": (Array of Strings) At least 3, ideally 4, plausible options. Only one must be correct.
  "correctAnswer": (String) The exact string of the correct option from the "options" array.
Optionally, you may include an "explanation" key with a brief explanation for the correct answer, derived from the transcript.

Formatting Requirements:
- Focus on key concepts, definitions, facts, or processes mentioned in the transcript.
- Ensure distractors (incorrect options) are plausible but clearly wrong based on the transcript.
- Avoid overly simple or trivial questions.
- Ensure the output is ONLY a valid JSON array of these question objects. No introductory text, comments, or markdown formatting outside the JSON strings. Just the array itself starting with [ and ending with ].
{user_instr}

Transcript:
\"\"\"{transcript_chunk}\"\"\"

Output JSON array:
"""
    if not client.api_key:
        logger.error("OpenAI API key not configured.")
        flash("Server configuration error: OpenAI API key is missing.")
        return []
    try:
        response = client.chat.completions.create(
            model=model,
            # response_format={ "type": "json_object" }, # Optional: if model supports JSON mode well
            messages=[
                {"role": "system", "content": "You are a helpful assistant creating multiple-choice questions as a JSON array."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2500,
            timeout=90
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for interactive questions: %s", result_text)

        # Attempt to extract JSON array
        json_match = re.search(r'^\s*(\[.*\])\s*$', result_text, re.DOTALL)
        if not json_match:
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                 json_str = result_text[start_idx : end_idx + 1]
                 logger.debug("Using fallback JSON extraction for interactive questions.")
            else:
                 json_str = None
        else:
            json_str = json_match.group(1)
            logger.debug("Using regex JSON extraction for interactive questions.")

        if json_str:
            try:
                questions = json.loads(json_str)
                # Basic validation of the structure
                if isinstance(questions, list) and all(isinstance(q, dict) and 'question' in q and 'options' in q and 'correctAnswer' in q for q in questions):
                     logger.debug("Successfully parsed %d interactive questions from extracted JSON.", len(questions))
                     # Further validation: check if options is a list and correctAnswer is in options
                     valid_questions = []
                     for q in questions:
                         if isinstance(q['options'], list) and q['correctAnswer'] in q['options']:
                             valid_questions.append(q)
                         else:
                             logger.warning("Invalid question structure found: %s", q)
                     if len(valid_questions) < len(questions):
                          flash(f"Warning: Some ({len(questions) - len(valid_questions)}) generated questions had invalid structure and were skipped.")
                     return valid_questions
                else:
                    logger.error("Extracted JSON is not a valid list of question objects: %s", json_str)
                    flash("API returned an invalid structure for interactive questions for a chunk.")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error("JSON parsing error for interactive questions chunk: %s\nContent: %s", parse_err, json_str)
                flash(f"Failed to parse API response for interactive questions (JSON error near char {parse_err.pos}).")
                return []
        else:
            logger.error("Could not find JSON array structure in API response for interactive questions: %s", result_text)
            flash("API response did not contain a recognizable JSON array for interactive questions.")
            return []

    except Exception as e:
        logger.exception("OpenAI API error or other exception for interactive questions chunk: %s", e)
        flash(f"An error occurred communicating with the AI for interactive questions: {e}")
        return []


def get_all_interactive_questions(transcript, user_preferences="", max_chunk_size=8000, model="gpt-4o-mini"):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk to generate interactive questions.
    Returns a combined list of all questions.
    """
    if not transcript or transcript.isspace():
        logger.warning("Received empty or whitespace-only transcript for interactive questions.")
        flash("Transcript content is empty.")
        return []
    cleaned_transcript = preprocess_transcript(transcript)
    if not cleaned_transcript:
        logger.warning("Transcript became empty after preprocessing for interactive questions.")
        flash("Transcript empty after cleaning.")
        return []

    logger.info("Cleaned transcript length for interactive questions: %d", len(cleaned_transcript))
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks for interactive questions.")
         flash("Failed to split transcript into processable chunks.")
         return []

    all_questions = []
    total_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.isspace():
            logger.debug("Skipping empty chunk %d/%d for interactive questions", i+1, total_chunks)
            continue
        logger.info("Processing chunk %d/%d for interactive questions (size: %d)", i+1, total_chunks, len(chunk))
        questions_from_chunk = get_interactive_questions_for_chunk(chunk, user_preferences, model=model)
        if questions_from_chunk:
            logger.info("Chunk %d produced %d interactive questions.", i+1, len(questions_from_chunk))
            all_questions.extend(questions_from_chunk)
        else:
            logger.warning("Chunk %d produced no interactive questions.", i+1)
            # Flash message handled within get_interactive_questions_for_chunk

    logger.info("Total interactive questions generated: %d", len(all_questions))
    if not all_questions:
        flash("No interactive questions could be generated from the provided transcript and settings.")
    return all_questions


# ----------------------------
# Embedded HTML Templates
# ----------------------------

# INDEX_HTML remains largely the same as your last working version
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transcript to Anki Cards or Interactive Game</title>
  <style>
    /* Basic Reset & Mobile Defaults */
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    html { font-size: 16px; scroll-behavior: smooth; }
    body { background-color: #1E1E20; color: #D7DEE9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; }
    button, input, select, textarea { font-family: inherit; font-size: inherit; outline: none; border: none; }
    button, input[type="submit"] { cursor: pointer; }

    /* Container and Layout */
    .container { max-width: 900px; margin: 40px auto 20px auto; padding: 0 20px; }
    h1 { text-align: center; color: #bb86fc; margin-bottom: 15px; font-size: 1.8rem; }
    p { text-align: center; margin-bottom: 25px; color: #aaa; }

    /* Form Elements */
    textarea, input[type="text"], input[type="number"], select {
      width: 100%;
      padding: 12px;
      margin-bottom: 15px;
      background-color: #2F2F31;
      color: #D7DEE9;
      border: 1px solid #444;
      border-radius: 5px;
      transition: border-color 0.2s ease;
    }
    textarea:focus, input[type="text"]:focus, input[type="number"]:focus, select:focus { border-color: #bb86fc; }
    textarea { min-height: 180px; resize: vertical; }
    label { display: block; text-align: left; margin-bottom: 5px; font-weight: bold; color: #ccc; }
    small { color: #888; display: block; margin-top: -10px; margin-bottom: 15px; font-size: 0.85em; }

    /* Buttons */
    input[type="submit"], .button {
      display: inline-block; /* Changed to inline-block for potential side-by-side */
      padding: 12px 25px;
      font-size: 1rem;
      font-weight: bold;
      background-color: #6200ee;
      color: #fff;
      border-radius: 5px;
      cursor: pointer;
      text-align: center;
      transition: background-color 0.3s, transform 0.1s ease;
      margin-top: 10px;
      width: 100%; /* Default to full width */
    }
    input[type="submit"]:hover, .button:hover { background-color: #3700b3; }
    input[type="submit"]:active, .button:active { transform: scale(0.98); }

    .button-group {
      display: flex;
      flex-direction: column; /* Stack by default */
      gap: 10px;
      align-items: center; /* Center buttons */
      margin-top: 15px;
    }
    @media (min-width: 600px) {
      .button-group {
          flex-direction: row; /* Side-by-side on larger screens */
          justify-content: center;
      }
       input[type="submit"], .button {
            width: auto; /* Allow natural width */
            min-width: 200px;
      }
    }


    /* Flash Messages */
    .flash { background-color: #cf6679; color: #121212; padding: 12px; margin: 0 auto 20px auto; border-radius: 5px; text-align: center; font-weight: bold; max-width: 860px; }

    /* Links */
    a { color: #03dac6; text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* Advanced Options */
    #advancedToggle {
      cursor: pointer;
      color: #bb86fc;
      font-weight: bold;
      margin-bottom: 10px;
      text-align: left;
      display: inline-block;
      padding: 5px 0;
      border-bottom: 1px dashed #bb86fc;
    }
    #advancedOptions {
      margin: 0 0 25px 0;
      text-align: left;
      background-color: #2a2a2e;
      padding: 20px;
      border: 1px solid #444;
      border-radius: 5px;
    }

    /* Loading Overlay */
    #loadingOverlay {
      position: fixed;
      inset: 0; /* Replaces top, left, width, height */
      background: rgba(18, 18, 18, 0.9);
      display: none; /* Initially hidden */
      flex-direction: column;
      justify-content: center;
      align-items: center;
      z-index: 9999;
      backdrop-filter: blur(4px);
    }
    #loadingText { color: #D7DEE9; margin-top: 20px; font-size: 1.1rem; }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.12.2/lottie.min.js"></script>
</head>
<body>
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 250px; height: 250px;"></div>
    <div id="loadingText">Generating. Please wait...</div>
  </div>

  <div class="container">
      <h1>Transcript Processor</h1>
      <p>
        Paste a transcript below. Generate Anki cloze cards or an interactive quiz. Need a transcript? Try <a href="https://tactiq.io/tools/youtube-transcript" target="_blank" rel="noopener noreferrer">Tactiq.io</a>.
      </p>

      {% with messages = get_flashed_messages() %}
        {% if messages %}
          {% for message in messages %}
            <div class="flash">{{ message }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}

      <form id="transcriptForm">
        <!-- Advanced Options Toggle -->
        <div id="advancedToggle" onclick="toggleAdvanced()">Advanced Options ▼</div>
        <div id="advancedOptions" style="display: none;">
          <label for="modelSelect">AI Model:</label>
          <select name="model" id="modelSelect">
            <option value="gpt-4o-mini" selected>GPT-4o Mini (Fastest)</option>
            <option value="gpt-4o">GPT-4o (Most Powerful)</option>
            <!-- Add other models if available/needed -->
          </select>

          <label for="maxSize">Max Chunk Size (chars):</label>
          <input type="number" name="max_size" id="maxSize" value="8000" min="1000" max="16000" step="1000">
          <small>Splits long transcripts. Affects processing time and context. Default: 8000.</small>

          <label for="preferences">Specific Instructions (Optional):</label>
          <input type="text" name="preferences" id="preferences" placeholder="e.g., Focus on definitions, ignore section X">
        </div>

        <label for="transcript">Transcript:</label>
        <textarea name="transcript" id="transcript" placeholder="Paste your full transcript here..." required></textarea>

        <div class="button-group">
          <input type="submit" name="mode" value="Generate Anki Cards">
          <input type="submit" name="mode" value="Generate Game">
        </div>
      </form>
  </div>

  <script>
    function toggleAdvanced(){
      var adv = document.getElementById("advancedOptions");
      var toggle = document.getElementById("advancedToggle");
      if(adv.style.display === "none" || adv.style.display === ""){
          adv.style.display = "block";
          toggle.innerHTML = "Advanced Options ▲";
      } else {
          adv.style.display = "none";
          toggle.innerHTML = "Advanced Options ▼";
      }
    }

    let lottieInstance = null;

    document.getElementById("transcriptForm").addEventListener("submit", function(event) {
      event.preventDefault();

      const transcript = document.getElementById('transcript').value;
      if (!transcript || transcript.trim().length < 10) { // Basic length check
          flashMessage('Please paste a transcript (at least 10 characters).', 'error');
          return;
      }

      var overlay = document.getElementById("loadingOverlay");
      overlay.style.display = "flex";

      if (!lottieInstance) {
        lottieInstance = lottie.loadAnimation({
            container: document.getElementById('lottieContainer'),
            renderer: 'svg',
            loop: true,
            autoplay: true,
            path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
        });
      } else {
          lottieInstance.play();
      }

      var form = event.target;
      var formData = new FormData(form);
      const clickedButton = event.submitter;
      let modeText = 'content'; // Default
      if (clickedButton && clickedButton.name === "mode") {
          formData.set("mode", clickedButton.value);
          modeText = clickedButton.value.includes('Anki') ? 'Anki Cards' : 'Game';
      } else {
          formData.set("mode", "Generate Anki Cards"); // Fallback
          modeText = 'Anki Cards';
      }
      document.getElementById('loadingText').textContent = `Generating ${modeText}... This may take a moment.`;


      fetch("/generate", {
        method: "POST",
        body: formData
      })
      .then(response => {
          // Check if response is OK (status 200-299)
          if (!response.ok) {
              // Attempt to read error text, then throw
              return response.text().then(text => {
                  // Try to parse flash message from redirect HTML if needed
                  const match = text.match(/<div class="flash">(.*?)<\/div>/);
                  const errorMsg = match ? match[1] : `Server error: ${response.status}`;
                  throw new Error(errorMsg);
              }).catch(() => {
                  // If reading text fails, throw generic error
                  throw new Error(`Server error: ${response.status}`);
              });
          }
          return response.text(); // Get HTML response as text
      })
      .then(html => {
        // Success: Replace current document content
        document.open();
        document.write(html);
        document.close();
        // New page will handle its own loading/display state
      })
      .catch(error => {
        console.error("Form submission error:", error);
        flashMessage(`Error: ${error.message}`, 'error'); // Display error to user
        if (lottieInstance) lottieInstance.stop();
        overlay.style.display = "none";
      });
    });

    // Helper to show flash messages dynamically if needed
    function flashMessage(message, type = 'info') {
        const existingFlash = document.querySelector('.flash');
        if (existingFlash) existingFlash.remove(); // Remove old message first

        const flashDiv = document.createElement('div');
        flashDiv.className = `flash ${type === 'error' ? 'error' : ''}`; // Add error class if needed
        flashDiv.textContent = message;

        const container = document.querySelector('.container');
        // Insert before the form
        container.insertBefore(flashDiv, document.getElementById('transcriptForm'));
         // Optional: scroll to message
        flashDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // Add error class style if needed
    const styleSheet = document.styleSheets[0];
    try { styleSheet.insertRule('.flash.error { background-color: #cf6679; color: #121212; }', styleSheet.cssRules.length); } catch(e) { console.warn("Could not insert error style rule"); }

  </script>
</body>
</html>
"""

# ANKI_HTML with restored styling + TTS elements + JS fixes
ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>Anki Cloze Review</title>
  <style>
    /* Base Styles from User Example + TTS additions */
    * { -webkit-tap-highlight-color: transparent; user-select: none; box-sizing: border-box; }
    html { touch-action: manipulation; height: 100%; }
    body { background-color: #1E1E20; font-family: helvetica, Arial, sans-serif; line-height: 1.6; margin: 0; padding: 0; display: flex; flex-direction: column; min-height: 100%; color: #D7DEE9; }
    button:focus, input:focus, textarea:focus { outline: none; }

    /* Layout Container */
    #reviewContainer { display: flex; flex-direction: column; align-items: center; padding: 15px; flex-grow: 1; justify-content: center; width: 100%; max-width: 700px; margin: 0 auto; }

    /* Progress Bar */
    #progress { width: 100%; text-align: center; color: #A6ABB9; margin-bottom: 10px; font-size: 14px; }

    /* Card Display */
    #kard {
      background-color: #2F2F31;
      border-radius: 5px; /* Original was 5px */
      padding: 20px; /* Original */
      width: 100%;
      min-height: 50vh; /* Original */
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      word-wrap: break-word;
      margin-bottom: 20px;
      /* box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2); */ /* Removed shadow from newer style */
    }
    .card { font-size: 20px; color: #D7DEE9; line-height: 1.6em; width: 100%; } /* Original font size */
    .cloze { font-weight: bold; color: MediumSeaGreen !important; cursor: pointer; } /* Original color, added important */
    .cloze-hint { font-size: 0.8em; color: #aaa; margin-left: 5px; font-style: italic; } /* Style for hint text (kept) */

    /* Editing Area */
    #editArea { width: 100%; height: 150px; font-size: 18px; padding: 10px; background-color: #252527; color: #D7DEE9; border: 1px solid #444; border-radius: 5px; line-height: 1.5; margin-top: 10px; }

    /* Action Buttons (Save/Discard after reveal) */
    #actionControls { display: none; justify-content: space-between; width: 100%; max-width: 700px; margin: 10px auto; gap: 10px; }
    .actionButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; /* Original spacing */ transition: background-color 0.2s ease, transform 0.1s ease; }
    .actionButton:active { transform: scale(0.97); }
    .discard { background-color: red; } /* Original color */
    .discard:hover { background-color: #ff4d4d; } /* Simple hover */
    .save { background-color: green; } /* Original color */
    .save:hover { background-color: #00a000; } /* Simple hover */

    /* Bottom Control Bar - Using original structure with added TTS group */
    .bottom-control-group { /* Renamed to avoid conflict */
      display: flex;
      justify-content: center;
      width: 100%;
      max-width: 700px; /* Keep max-width consistent */
      margin: 5px auto; /* Original margin */
      padding: 0 10px; /* Original padding */
      gap: 10px; /* Gap between buttons in the same group */
    }
    .bottomButton {
      padding: 10px 20px; /* Original padding */
      font-size: 16px; /* Original font size */
      border: none;
      color: #fff;
      border-radius: 5px; /* Original radius */
      cursor: pointer;
      background-color: #6200ee; /* Original color */
      flex: 1; /* Take available space */
      transition: background-color 0.3s; /* Original transition */
      white-space: nowrap;
    }
    .bottomButton:hover { background-color: #3700b3; } /* Original hover */
    .bottomButton:active { transform: scale(0.97); }
    .bottomButton:disabled { background-color: #444; color: #888; cursor: not-allowed; }
    .edit { background-color: #FFA500; } /* Original color */
    .edit:hover { background-color: #cc8400; }
    .cart { background-color: #03A9F4; } /* Original color */
    .cart:hover { background-color: #0288D1; } /* Original color */

    /* TTS Toggle Button Styling */
    .tts-toggle { background-color: #FF6347; } /* Tomato (Default OFF) */
    .tts-toggle.on { background-color: #32CD32; } /* LimeGreen (ON) */
    .tts-toggle:hover { background-color: #FF7F50; } /* Coral hover for OFF */
    .tts-toggle.on:hover { background-color: #90EE90; } /* LightGreen hover for ON */

    /* Edit Mode Controls (Save/Cancel Edit) */
    #editControls { display: none; justify-content: space-between; width: 100%; max-width: 700px; margin: 10px auto; gap: 10px; }
    .editButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; transition: background-color 0.2s ease, transform 0.1s ease; }
    .editButton:active { transform: scale(0.97); }
    .cancelEdit { background-color: gray; } /* Original */
    .cancelEdit:hover { background-color: #6c757d; }
    .saveEdit { background-color: green; } /* Original */
    .saveEdit:hover { background-color: #006400; }

    /* Saved Cards / Finished Screen */
    #savedCardsContainer { width: 100%; max-width: 700px; margin: 20px auto; color: #D7DEE9; display: none; flex-direction: column; align-items: center; }
    #savedCardsText {
      width: 100%; height: 200px; padding: 10px; font-size: 16px; /* Original */
      background-color: #2F2F31; border: none; border-radius: 5px; resize: none; color: #D7DEE9;
      font-family: monospace; white-space: pre;
    }
    #finishedHeader { text-align: center; color: #bb86fc; margin-bottom: 15px; }
    .saved-cards-buttons { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-top: 15px; width: 100%; }
    /* Style saved card buttons like bottom buttons for consistency */
    #copyButton, #downloadButton, #returnButton {
      margin-top: 10px; /* Keep original margin */
      padding: 10px 20px; font-size: 16px; background-color: #4A90E2; color: #fff;
      border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.2s ease;
      flex: 1 1 auto; min-width: 120px;
    }
    #copyButton:hover, #downloadButton:hover, #returnButton:hover { background-color: #6BB0F5; }
    #copyButton { background-color: #4A90E2; } /* Original */
    #downloadButton { background-color: #03A9F4; } /* Original */
    #returnButton { background-color: #757575; } /* Gray */

    /* Loading Overlay */
    #loadingOverlay { position: fixed; inset: 0; background: rgba(18, 18, 18, 0.9); display: flex; justify-content: center; align-items: center; z-index: 9999; backdrop-filter: blur(5px); }

    /* Responsive Adjustments (example, adjust as needed) */
    @media (max-width: 600px) {
        .card { font-size: 18px; }
        #kard { padding: 15px; min-height: 40vh; }
        .actionButton, .bottomButton, .editButton { font-size: 14px; padding: 10px 15px; }
        .bottom-control-group { flex-direction: column; align-items: stretch; gap: 8px; /* Adjust gap for vertical stack */ }
        .bottomButton { width: 100%; margin: 0; }
    }

  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.12.2/lottie.min.js"></script>
</head>
<body>
  <!-- Loading Overlay -->
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 250px; height: 250px;"></div>
  </div>

  <div id="reviewContainer" style="display: none;"> <!-- Hide initially until loaded -->
    <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
    <div id="kard">
      <div class="card" id="cardContent"></div>
      <!-- Edit area will be inserted here by JS when needed -->
    </div>

    <!-- Action Controls (Save/Discard) - Shown after answer reveal -->
    <div id="actionControls">
      <button id="discardButton" class="actionButton discard" onmousedown="event.preventDefault()" ontouchend="this.blur()">Discard</button>
      <button id="saveButton" class="actionButton save" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save</button>
    </div>

    <!-- Edit Mode Controls - Shown during edit -->
    <div id="editControls">
      <button id="cancelEditButton" class="editButton cancelEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Cancel</button>
      <button id="saveEditButton" class="editButton saveEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save Edit</button>
    </div>

    <!-- Bottom Controls Bar -->
    <!-- Grouping controls logically -->
    <div class="bottom-control-group">
        <button id="undoButton" class="bottomButton undo" onmousedown="event.preventDefault()" ontouchend="this.blur()">Previous Card</button>
        <button id="editButton" class="bottomButton edit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Edit Card</button>
    </div>
     <div class="bottom-control-group">
        <button id="cartButton" class="bottomButton cart" onmousedown="event.preventDefault()" ontouchend="this.blur()">Saved (<span id="savedCount">0</span>)</button>
        <button id="ttsToggleButton" class="bottomButton tts-toggle" onmousedown="event.preventDefault()" ontouchend="this.blur()">TTS: OFF</button>
     </div>


    <!-- Saved Cards / Finished Screen Area -->
    <div id="savedCardsContainer">
      <h3 id="finishedHeader" style="text-align:center;">Saved Cards</h3>
      <textarea id="savedCardsText" readonly></textarea>
      <div class="saved-cards-buttons">
        <button id="copyButton" onmousedown="event.preventDefault()" ontouchend="this.blur()">Copy Text</button>
        <button id="downloadButton" onmousedown="event.preventDefault()" ontouchend="this.blur()">Download .apkg</button>
        <button id="returnButton" class="return" onmousedown="event.preventDefault()" ontouchend="this.blur()">Return to Card</button>
      </div>
    </div>
  </div>

  <script>
    // === Global Variables and Initial Setup ===
    // Ensure cards_json passed from Flask is properly handled, even if empty
    const rawCards = {{ cards_json|safe if cards_json else '[]' }};
    let interactiveCards = []; // Will hold objects { target: num|null, displayText: html, exportText: original, hint: string|null }
    let currentIndex = 0;
    let savedCards = []; // Holds the exportText of saved cards
    let historyStack = []; // For undo functionality {index, saved, finishedState, view}
    let inEditMode = false; // Redundant if using viewMode, but kept for clarity maybe
    let finished = false;
    let viewMode = 'loading'; // 'loading', 'card', 'saved', 'edit'
    let savedCardIndexBeforeCart = null; // Track index when opening saved view

    // TTS State
    let ttsEnabled = false;
    let currentUtterance = null; // To manage ongoing speech

    // DOM Elements (cache references)
    const reviewContainer = document.getElementById('reviewContainer');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const currentEl = document.getElementById("current");
    const totalEl = document.getElementById("total");
    const cardContentEl = document.getElementById("cardContent");
    const kardEl = document.getElementById("kard");
    const actionControls = document.getElementById("actionControls");
    const undoButton = document.getElementById("undoButton");
    const editButton = document.getElementById("editButton");
    const discardButton = document.getElementById("discardButton");
    const saveButton = document.getElementById("saveButton");
    const editControls = document.getElementById("editControls");
    const saveEditButton = document.getElementById("saveEditButton");
    const cancelEditButton = document.getElementById("cancelEditButton");
    const savedCardsContainer = document.getElementById("savedCardsContainer");
    const finishedHeader = document.getElementById("finishedHeader");
    const savedCardsText = document.getElementById("savedCardsText");
    const copyButton = document.getElementById("copyButton");
    const downloadButton = document.getElementById("downloadButton");
    const cartButton = document.getElementById("cartButton");
    const returnButton = document.getElementById("returnButton");
    const savedCountEl = document.getElementById("savedCount");
    const progressEl = document.getElementById("progress");
    const ttsToggleButton = document.getElementById("ttsToggleButton");
    const bottomControlGroups = document.querySelectorAll('.bottom-control-group'); // Get all groups


    // === Core Functions ===

    function generateInteractiveCards(cardText) {
      const regex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g; // Capture hint group 3
      const numbers = new Set();
      let tempText = cardText; // Work on a copy
      let m;
      while ((m = regex.exec(tempText)) !== null) {
        if (m.index === regex.lastIndex) regex.lastIndex++; // Prevent infinite loop
        numbers.add(m[1]);
      }
      regex.lastIndex = 0; // Reset regex

      if (numbers.size === 0) {
        return [{ target: null, displayText: cardText, exportText: cardText, hint: null }];
      }

      const cardsForNote = [];
      Array.from(numbers).sort((a, b) => parseInt(a) - parseInt(b)).forEach(num => { // Sort numerically
        const { processedText, hint } = processCloze(cardText, num);
        cardsForNote.push({ target: num, displayText: processedText, exportText: cardText, hint: hint });
      });
      return cardsForNote;
    }

    function processCloze(text, targetClozeNumber) {
        let hintText = null;
        // Use String() for comparison as clozeNum is from regex group
        const targetNumStr = String(targetClozeNumber);
        const processed = text.replace(/{{c(\d+)::(.*?)(?:::(.*?))?}}/g, (match, clozeNum, answer, hint) => {
            answer = answer.trim();
            hint = hint ? hint.trim() : null;

            if (clozeNum === targetNumStr) {
                // This is the cloze being tested ([...])
                hintText = hint; // Store the hint for this specific card
                let hintSpan = hint ? `<span class="cloze-hint">(${escapeHtml(hint)})</span>` : ''; // Escape hint too
                return `<span class="cloze" data-answer="${escapeHtml(answer)}">[...]${hintSpan}</span>`;
            } else {
                // Other clozes (not being tested) are shown directly
                return escapeHtml(answer); // Escape other answers shown on front
            }
        });
        return { processedText: processed, hint: hintText }; // Return processed text and the hint for the target
    }


    function escapeHtml(unsafe) {
        if (typeof unsafe !== 'string') return "";
        return unsafe
             .replace(/&/g, "&")
             .replace(/</g, "<")
             .replace(/>/g, ">")
             .replace(/"/g, """)
             .replace(/'/g, "'");
    }


    function updateDisplay() {
        console.debug("Updating display for viewMode:", viewMode, "Index:", currentIndex, "Finished:", finished);
        // Common resets
        kardEl.style.display = 'flex';
        cardContentEl.style.display = 'block';
        actionControls.style.display = 'none';
        editControls.style.display = 'none';
        savedCardsContainer.style.display = 'none';
        bottomControlGroups.forEach(group => group.style.display = 'flex'); // Show all bottom groups initially
        progressEl.style.visibility = 'visible';
        inEditMode = false; // Reset flag

        // Remove edit area if it exists from a previous state
        const existingEditArea = document.getElementById('editArea');
        if (existingEditArea) {
            existingEditArea.remove();
        }

        if (viewMode === 'card' && !finished) {
            if (currentIndex >= interactiveCards.length) {
                 console.error("Attempted to display card index out of bounds:", currentIndex);
                 showFinishedScreen(); // Fallback to finished state
                 return;
            }
            const currentCardData = interactiveCards[currentIndex];
            progressEl.textContent = `Card ${currentIndex + 1} of ${interactiveCards.length}`;
            cardContentEl.innerHTML = currentCardData.displayText; // Assumes processCloze handled escaping needed for HTML render
            updateUndoButtonState();
            editButton.disabled = false;
            cartButton.disabled = false;
            ttsToggleButton.disabled = false;
            speakCurrentCardState(); // Speak front if enabled

        } else if (viewMode === 'saved' || finished) {
            kardEl.style.display = 'none';
            progressEl.style.visibility = 'hidden';
            bottomControlGroups.forEach(group => group.style.display = 'none'); // Hide bottom button groups

            finishedHeader.textContent = finished ? "Review Complete!" : "Saved Cards";
            // Use two newlines for better separation in textarea
            savedCardsText.value = savedCards.length > 0 ? savedCards.join("\n\n") : "No cards saved yet.";
            savedCardsContainer.style.display = 'flex';

            copyButton.textContent = "Copy Text";
            copyButton.disabled = savedCards.length === 0;
            downloadButton.disabled = savedCards.length === 0;
            returnButton.style.display = finished ? 'none' : 'block';
            if (!finished && savedCardIndexBeforeCart !== null) {
                 returnButton.textContent = `Return to Card ${savedCardIndexBeforeCart + 1}`;
            }
            window.speechSynthesis.cancel(); // Stop speech

        } else if (viewMode === 'edit') {
            inEditMode = true;
            progressEl.textContent = `Editing Card ${currentIndex + 1}`;
            cardContentEl.style.display = 'none'; // Hide normal content
            bottomControlGroups.forEach(group => group.style.display = 'none'); // Hide bottom buttons

            let editArea = document.createElement('textarea');
            editArea.id = 'editArea';
            editArea.value = interactiveCards[currentIndex].exportText; // Edit the original source
            kardEl.appendChild(editArea); // Add inside card area

            editControls.style.display = 'flex'; // Show Save/Cancel Edit buttons
            window.speechSynthesis.cancel(); // Stop speech
        }

        updateSavedCount();
    }


    function revealAnswer() {
        if (inEditMode || actionControls.style.display === 'flex' || viewMode !== 'card') return;

        const clozes = cardContentEl.querySelectorAll(".cloze");
        if (clozes.length > 0) {
            clozes.forEach(span => {
                const answer = span.getAttribute("data-answer");
                // Render the answer - it was already escaped if needed by processCloze/escapeHtml
                span.innerHTML = answer;
                span.classList.remove("cloze-hint"); // Remove hint visually if present
                // Optionally, add a class to indicate revealed state if needed for styling
                 // span.classList.add('revealed');
            });
        }
        // Always show action controls on click, even if no clozes (Q&A style)
        actionControls.style.display = "flex";
        speakCurrentCardState(); // Speak back if enabled
    }


    function handleCardAction(saveCard) {
      if (viewMode !== 'card' || finished) return;

      historyStack.push({
          index: currentIndex,
          saved: savedCards.slice(),
          finishedState: finished,
          view: viewMode // Should be 'card' here
      });
      updateUndoButtonState();

      if (saveCard) {
          const cardToSave = interactiveCards[currentIndex].exportText;
          if (!savedCards.includes(cardToSave)) {
             savedCards.push(cardToSave);
             updateSavedCount();
          } else {
              console.debug("Card already saved, not adding duplicate:", cardToSave.substring(0, 50));
          }
      }

      if (currentIndex < interactiveCards.length - 1) {
          currentIndex++;
          viewMode = 'card'; // Stay in card mode
          updateDisplay();
      } else {
          showFinishedScreen(); // Go to finished state
      }
    }


    function handleUndo() {
      if (historyStack.length === 0) return;
      window.speechSynthesis.cancel(); // Stop any speech before state change

      const snapshot = historyStack.pop();
      currentIndex = snapshot.index;
      savedCards = snapshot.saved; // Restore saved cards array
      finished = snapshot.finishedState;
      viewMode = snapshot.view; // Restore view mode ('card' most likely)

      updateDisplay();
      updateUndoButtonState(); // Update button state after stack change
    }


    function enterEditMode() {
      if (viewMode !== 'card' || finished) return;
      window.speechSynthesis.cancel();
      viewMode = 'edit';
      savedCardIndexBeforeCart = currentIndex; // Store for cancel
      updateDisplay();
    }


    function cancelEdit() {
      viewMode = 'card';
      // No need to restore index, it wasn't changed
      updateDisplay();
    }


    function saveEdit() {
      const editArea = document.getElementById('editArea');
      if (!editArea) return; // Should not happen

      const editedText = editArea.value;
      if (!editedText || editedText.trim() === "") {
          alert("Card text cannot be empty.");
          return;
      }
      // Get the original card text *before* editing started
      const originalExportText = interactiveCards[savedCardIndexBeforeCart].exportText;
      const fixedEditedText = fix_cloze_formatting(editedText);

      // Regenerate interactive cards based *only* on the edited text
      const newInteractiveCardsForThisNote = generateInteractiveCards(fixedEditedText);
      if (newInteractiveCardsForThisNote.length === 0) {
          alert("Edited text resulted in no valid cloze cards. Please check formatting.");
          return;
      }

      // Find the range of indices in the main array that correspond to the *original* card text
      let firstIndex = -1;
      let count = 0;
      for(let i = 0; i < interactiveCards.length; i++) {
          if (interactiveCards[i].exportText === originalExportText) {
              if (firstIndex === -1) firstIndex = i;
              count++;
          } else if (firstIndex !== -1) {
              // Stop counting once we hit cards from a different original source
              break;
          }
      }

      if (firstIndex !== -1) {
          // Replace the old cards derived from the original text with the new ones
          interactiveCards.splice(firstIndex, count, ...newInteractiveCardsForThisNote);
          // Set current index to the start of the newly inserted cards
          currentIndex = firstIndex;
      } else {
          // Should not happen if editing an existing card, but log it
          console.error("Could not find original card index during save edit.");
          // Fallback: append and set index (might mess up order)
          currentIndex = interactiveCards.length;
          interactiveCards.push(...newInteractiveCardsForThisNote);
      }

      // Update total count and exit edit mode
      totalEl.textContent = interactiveCards.length;
      viewMode = 'card';
      finished = false; // Review might not be finished anymore
      updateDisplay();
    }


    function showSavedCards() {
        if (viewMode === 'saved') return;
        window.speechSynthesis.cancel();
        savedCardIndexBeforeCart = currentIndex;
        viewMode = 'saved';
        updateDisplay();
    }

    function returnToCard() {
        if (viewMode !== 'saved' || finished) return; // Can't return if finished
        viewMode = 'card';
        // Restore index if valid
        if (savedCardIndexBeforeCart !== null && savedCardIndexBeforeCart < interactiveCards.length) {
             currentIndex = savedCardIndexBeforeCart;
        } else {
            currentIndex = 0; // Fallback to first card
        }
        updateDisplay();
    }

    function updateSavedCount() {
        savedCountEl.textContent = savedCards.length;
    }

    function updateUndoButtonState() {
        undoButton.disabled = historyStack.length === 0;
    }

    function showFinishedScreen() {
        window.speechSynthesis.cancel();
        finished = true;
        viewMode = 'saved'; // Use 'saved' view for finished state
        updateDisplay();
    }

    function copySavedCardsToClipboard() {
        savedCardsText.select();
        try {
            // Use Clipboard API for modern browsers if available
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(savedCardsText.value).then(() => {
                    copyButton.textContent = "Copied!";
                    setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
                }).catch(err => {
                    console.error('Async copy failed: ', err);
                    throw err; // Fallback to execCommand
                });
            } else {
                 if (!document.execCommand("copy")) {
                     throw new Error("execCommand failed");
                 }
                 copyButton.textContent = "Copied!";
                 setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
            }
        } catch (err) {
            console.error('Failed to copy text: ', err);
            copyButton.textContent = "Copy Failed";
            setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
            alert("Could not copy text. Your browser might not support this feature or permissions may be denied.");
        } finally {
            // Deselect text regardless of success/failure
             window.getSelection().removeAllRanges();
        }
    }


    function downloadApkgFile() {
        if (savedCards.length === 0) {
            alert("No saved cards to download.");
            return;
        }
        downloadButton.textContent = "Preparing...";
        downloadButton.disabled = true;

        fetch("/download_apkg", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ saved_cards: savedCards })
        })
        .then(response => {
            if (!response.ok) {
                 return response.text().then(text => {
                     throw new Error(text || `Server error: ${response.status}`);
                 });
            }
            const disposition = response.headers.get('content-disposition');
            let filename = "saved_cards.apkg"; // Default filename
            if (disposition && disposition.includes('attachment')) {
                const filenameMatch = disposition.match(/filename="?(.+?)"?(;|$)/);
                if (filenameMatch && filenameMatch[1]) {
                  filename = filenameMatch[1];
                }
            }
            return response.blob().then(blob => ({ blob, filename }));
        })
        .then(({ blob, filename }) => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.style.display = "none";
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            a.remove();
            downloadButton.textContent = "Downloaded!";
            setTimeout(() => {
                downloadButton.textContent = "Download .apkg";
                downloadButton.disabled = false;
            }, 2500);
        })
        .catch(error => {
            console.error("Download failed:", error);
            alert(`Download failed: ${error.message}`);
            downloadButton.textContent = "Download Failed";
            setTimeout(() => {
                 downloadButton.textContent = "Download .apkg";
                 downloadButton.disabled = (savedCards.length === 0); // Re-enable based on current state
             }, 2500);
        });
    }


    // === TTS Functions ===
    function toggleTTS() {
        ttsEnabled = !ttsEnabled;
        if (ttsEnabled) {
            ttsToggleButton.textContent = "TTS: ON";
            ttsToggleButton.classList.add("on");
            // Attempt to speak current state immediately if voices likely loaded
            speakCurrentCardState();
        } else {
            ttsToggleButton.textContent = "TTS: OFF";
            ttsToggleButton.classList.remove("on");
            window.speechSynthesis.cancel(); // Stop any current speech
        }
    }

    function speakText(text) {
        if (!text || text.trim() === "" || !('speechSynthesis' in window)) {
            if (!('speechSynthesis' in window)) console.warn("Speech Synthesis not supported.");
            return;
        }

        window.speechSynthesis.cancel(); // Cancel previous before speaking new

        currentUtterance = new SpeechSynthesisUtterance(text);
        // Optional: Configure voice, rate, pitch, etc.
        // let voices = window.speechSynthesis.getVoices(); // Get voices (might be async)
        // currentUtterance.voice = voices.find(v => v.lang.startsWith('en') && v.default); // Example: Find default English voice
        currentUtterance.lang = 'en-US'; // Explicitly set language if needed
        currentUtterance.rate = 1.0;
        currentUtterance.pitch = 1.0;
        currentUtterance.volume = 0.9; // Slightly lower volume can be clearer

        currentUtterance.onend = () => { currentUtterance = null; };
        currentUtterance.onerror = (event) => {
            console.error("Speech Synthesis Error:", event.error);
            currentUtterance = null;
        };

        // Slight delay can sometimes help prevent interruptions or glitches
        setTimeout(() => window.speechSynthesis.speak(currentUtterance), 50);
    }

    function getTextToSpeak() {
        if (viewMode !== 'card' || finished || currentIndex >= interactiveCards.length) {
             return null;
        }

        const cardData = interactiveCards[currentIndex];
        let originalText = cardData.exportText; // Start with the source text
        const targetClozeNumber = cardData.target ? String(cardData.target) : null; // Cloze # being tested (as string)
        let textForSpeech = "";

        const isRevealed = actionControls.style.display === "flex";

        // Regex to find clozes: {{c<number>::<answer>(::<hint>)?}}
        const clozeRegex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g;
        let lastIndex = 0;
        let match;

        // Process text segment by segment to handle non-cloze text too
        while ((match = clozeRegex.exec(originalText)) !== null) {
             // Add text before the match
             textForSpeech += originalText.substring(lastIndex, match.index);

             const [fullMatch, num, answer, hint] = match.map(s => s ? s.trim() : s);

             if (isRevealed) {
                 // Back Side: Read the answer for all clozes
                 textForSpeech += " " + answer;
             } else {
                 // Front Side: Read 'blank'/'hint' or the actual answer
                 if (targetClozeNumber !== null && num === targetClozeNumber) {
                     // This is the cloze currently hidden ([...])
                     textForSpeech += " " + (hint || "blank"); // Read hint if available, else "blank"
                 } else {
                     // Other clozes are revealed on the front side
                     textForSpeech += " " + answer;
                 }
             }
             lastIndex = clozeRegex.lastIndex;
        }
        // Add any remaining text after the last cloze
        textForSpeech += originalText.substring(lastIndex);

        // Clean up HTML and normalize
        textForSpeech = textForSpeech.replace(/<br\s*\/?>/gi, '. '); // Replace breaks with pauses
        textForSpeech = textForSpeech.replace(/<[^>]*>/g, ' '); // Remove other HTML tags
        textForSpeech = textForSpeech.replace(/\s+/g, ' ').trim(); // Normalize whitespace

        return textForSpeech;
    }

    function speakCurrentCardState() {
        if (!ttsEnabled || window.speechSynthesis.speaking || window.speechSynthesis.pending) {
             if(window.speechSynthesis.speaking || window.speechSynthesis.pending) {
                 console.debug("Speech ignored: Already speaking or pending.");
             }
             return;
        }
        const textToSpeak = getTextToSpeak();
        if (textToSpeak) {
            speakText(textToSpeak);
        }
    }

    // === Event Listeners ===
    kardEl.addEventListener("click", revealAnswer);
    discardButton.addEventListener("click", (e) => { e.stopPropagation(); handleCardAction(false); });
    saveButton.addEventListener("click", (e) => { e.stopPropagation(); handleCardAction(true); });
    undoButton.addEventListener("click", (e) => { e.stopPropagation(); handleUndo(); });
    editButton.addEventListener("click", (e) => { e.stopPropagation(); enterEditMode(); });
    cancelEditButton.addEventListener("click", (e) => { e.stopPropagation(); cancelEdit(); });
    saveEditButton.addEventListener("click", (e) => { e.stopPropagation(); saveEdit(); });
    cartButton.addEventListener("click", (e) => { e.stopPropagation(); showSavedCards(); });
    returnButton.addEventListener("click", (e) => { e.stopPropagation(); returnToCard(); });
    copyButton.addEventListener("click", (e) => { e.stopPropagation(); copySavedCardsToClipboard(); });
    downloadButton.addEventListener("click", (e) => { e.stopPropagation(); downloadApkgFile(); });
    ttsToggleButton.addEventListener("click", (e) => { e.stopPropagation(); toggleTTS(); });


    // === Initialization ===
    function initialize() {
        try {
            rawCards.forEach(cardText => {
                interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
            });
        } catch (error) {
            console.error("Error processing initial card data:", error);
            // Display error to user?
             progressEl.textContent = "Error loading cards";
             cardContentEl.innerHTML = "Could not process card data. Please check the source.";
              // Disable buttons maybe?
        }


        if (interactiveCards.length === 0) {
            progressEl.textContent = "No cards to review";
            cardContentEl.innerHTML = "No valid Anki cards were generated or found.";
            [undoButton, editButton, cartButton, ttsToggleButton, saveButton, discardButton].forEach(btn => btn.disabled = true);
            viewMode = 'finished'; // Treat as finished
        } else {
            totalEl.textContent = interactiveCards.length;
            updateSavedCount();
            updateUndoButtonState();
            viewMode = 'card'; // Start normal review
            updateDisplay();
        }

        // Hide loading overlay
        loadingOverlay.style.transition = 'opacity 0.5s ease';
        loadingOverlay.style.opacity = '0';
        setTimeout(() => {
            loadingOverlay.style.display = 'none';
            reviewContainer.style.display = 'flex';
        }, 500); // Wait for fade out
    }

    // Initialize Lottie animation for loading screen
    var lottieAnimation = lottie.loadAnimation({
      container: document.getElementById('lottieContainer'),
      renderer: 'svg',
      loop: true,
      autoplay: true,
      // Using the animation from Index page for consistency
      path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
    });

    // Start the application once the window is loaded
    window.addEventListener('load', initialize);

    // Ensure speech is cancelled if the user navigates away
    window.addEventListener('beforeunload', () => {
        window.speechSynthesis.cancel();
    });

  </script>
</body>
</html>
"""

# INTERACTIVE_HTML remains the same as the previous working version
INTERACTIVE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no">
  <title>Interactive Quiz</title>
  <style>
    /* Global resets and mobile-friendly properties */
    * { -webkit-tap-highlight-color: transparent; user-select: none; box-sizing: border-box; }
    html { touch-action: manipulation; height: 100%; }
    body { background-color: #121212; color: #f0f0f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; text-align: center; margin: 0; padding: 0; display: flex; flex-direction: column; min-height: 100vh; }
    button { -webkit-appearance: none; outline: none; border: none; cursor: pointer; }
    button:focus { outline: none; }

    .container { max-width: 800px; margin: 0 auto; padding: 20px; flex-grow: 1; display: flex; flex-direction: column; justify-content: center; }

    /* Header with progress and score */
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding: 0 10px; width: 100%; color: #aaa; }
    #questionProgress, #rawScore { font-size: 16px; font-weight: 500; }

    /* Timer */
    .timer { font-size: 20px; font-weight: bold; margin-bottom: 25px; color: #bb86fc; }

    /* Question Box */
    .question-box { background-color: #1e1e1e; padding: 25px; border: 1px solid #333; border-radius: 10px; margin-bottom: 25px; font-size: 20px; line-height: 1.5; min-height: 100px; display: flex; align-items: center; justify-content: center; }

    /* Options List */
    .options { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 12px; align-items: center; width: 100%; }
    .options li { width: 100%; max-width: 450px; /* Limit width of options */ }

    /* Option Buttons */
    .option-button {
      position: relative;
      overflow: hidden; /* For ripple effect */
      background: linear-gradient(135deg, #3700b3, #6200ee);
      color: #f0f0f0;
      font-size: 18px;
      width: 100%;
      padding: 15px 20px; /* Increased padding */
      border-radius: 8px;
      transition: transform 0.15s ease, background 0.3s ease, box-shadow 0.3s ease;
      text-align: center;
      font-weight: 500;
      border: 1px solid transparent; /* Placeholder for feedback border */
    }
    .option-button:disabled { opacity: 0.7; cursor: not-allowed; }
    @media (hover: hover) {
      .option-button:not(:disabled):hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3); }
    }
    .option-button:active { transform: scale(0.98); } /* Slightly different active effect */

    /* Feedback Styling */
    .option-button.correct { background: #03dac6 !important; color: #121212 !important; font-weight: bold; border-color: #018786; }
    .option-button.incorrect { background: #cf6679 !important; color: #121212 !important; font-weight: bold; border-color: #B00020; }

    /* Feedback/End Game Area */
    #feedback { margin-top: 30px; font-size: 18px; }
    #feedback h2 { color: #bb86fc; margin-bottom: 20px; }
    #feedback .option-button { margin-top: 15px; max-width: 300px; } /* Style end-game buttons */
    #ankiCardsContainer { display: none; margin-top: 20px; text-align: left; background-color: #1e1e1e; padding: 15px; border: 1px solid #bb86fc; border-radius: 8px; max-height: 200px; overflow-y: auto; font-size: 14px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }

    .hidden { display: none; }

    /* Ripple effect */
    .ripple { position: absolute; border-radius: 50%; background: rgba(255, 255, 255, 0.4); transform: scale(0); animation: ripple-animation 0.6s linear; pointer-events: none; }
    @keyframes ripple-animation { to { transform: scale(4); opacity: 0; } }

    /* Loading Overlay Styles */
    #loadingOverlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(18, 18, 18, 0.9); display: flex; justify-content: center; align-items: center; z-index: 9999; backdrop-filter: blur(5px); }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.12.2/lottie.min.js"></script>
</head>
<body>
  <!-- Loading Overlay -->
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 250px; height: 250px;"></div>
  </div>

  <div class="container" id="gameContainer" style="display: none;"> <!-- Hidden until loaded -->
    <div class="header">
      <div id="questionProgress">Question 1 of 0</div>
      <div id="rawScore">Score: 0</div>
    </div>
    <div class="timer" id="timer">Time: 15</div>
    <div class="question-box" id="questionBox">Loading question...</div>
    <div id="optionsWrapper"><ul class="options"></ul></div> <!-- Wrapper for options -->
    <div id="feedback" class="hidden"></div> <!-- Area for end game results -->
  </div>

  <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js"></script> <!-- Updated Confetti -->
  <script>
    // === Global Variables ===
    const questions = {{ questions_json|safe if questions_json else '[]' }}; // Handle empty case
    let currentQuestionIndex = 0;
    let score = 0;
    let timerInterval;
    let timeLeft = 15; // Default time per question
    const totalQuestions = questions.length;

    // DOM Elements
    const gameContainer = document.getElementById('gameContainer');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const questionProgressEl = document.getElementById('questionProgress');
    const rawScoreEl = document.getElementById('rawScore');
    const timerEl = document.getElementById('timer');
    const questionBox = document.getElementById('questionBox');
    const optionsWrapper = document.getElementById('optionsWrapper');
    const feedbackEl = document.getElementById('feedback');

    // === Core Game Functions ===

    function startGame() {
      score = 0;
      currentQuestionIndex = 0;
      feedbackEl.classList.add('hidden'); // Hide feedback area
      feedbackEl.innerHTML = ''; // Clear previous feedback content
      optionsWrapper.style.display = 'block'; // Ensure options are visible
      timerEl.style.display = 'block'; // Ensure timer is visible
      updateHeader();
      showQuestion();
    }

    function updateHeader() {
      questionProgressEl.textContent = `Question ${currentQuestionIndex + 1} / ${totalQuestions}`;
      rawScoreEl.textContent = `Score: ${score}`;
    }

    function startTimer() {
      timeLeft = 15; // Reset timer
      timerEl.textContent = `Time: ${timeLeft}`;
      clearInterval(timerInterval); // Clear any existing timer

      timerInterval = setInterval(() => {
        timeLeft--;
        timerEl.textContent = `Time: ${timeLeft}`;
        if (timeLeft <= 0) {
          clearInterval(timerInterval);
          handleAnswerSelection(null); // Time's up, treat as incorrect/no answer
        }
      }, 1000);
    }

    function showQuestion() {
      if (currentQuestionIndex >= totalQuestions) {
        endGame();
        return;
      }

      const currentQuestion = questions[currentQuestionIndex];
      questionBox.textContent = currentQuestion.question;
      optionsWrapper.innerHTML = ""; // Clear previous options
      const ul = document.createElement('ul');
      ul.className = 'options';

      // Shuffle options using Fisher-Yates algorithm
      const optionsShuffled = [...currentQuestion.options]; // Create a copy
      for (let i = optionsShuffled.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [optionsShuffled[i], optionsShuffled[j]] = [optionsShuffled[j], optionsShuffled[i]];
      }

      optionsShuffled.forEach(option => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.textContent = option;
        button.className = 'option-button';
        // Prevent default behaviors that might interfere
        button.onmousedown = (e) => { e.preventDefault(); };
        button.ontouchstart = (e) => { /* Allow touch interaction */ };
        button.onclick = (event) => {
             createRipple(event); // Add ripple on click
             handleAnswerSelection(option); // Process answer
        };
        li.appendChild(button);
        ul.appendChild(li);
      });

      optionsWrapper.appendChild(ul);
      startTimer();
      updateHeader();
    }

    function handleAnswerSelection(selectedOption) {
      clearInterval(timerInterval); // Stop the timer
      const currentQuestion = questions[currentQuestionIndex];
      const buttons = optionsWrapper.querySelectorAll('.option-button');
      const isCorrect = (selectedOption === currentQuestion.correctAnswer);

      buttons.forEach(button => {
        button.disabled = true; // Disable all buttons
        if (button.textContent === currentQuestion.correctAnswer) {
          button.classList.add('correct');
        } else if (button.textContent === selectedOption) {
          button.classList.add('incorrect');
        }
      });

      if (isCorrect) {
        score++;
        // Trigger confetti!
        confetti({
          particleCount: 120,
          spread: 80,
          origin: { y: 0.6 },
           colors: ['#bb86fc', '#03dac6', '#f0f0f0']
        });
      } else if (selectedOption !== null) {
          // Optional: Subtle shake effect for incorrect answers
           questionBox.animate([
                { transform: 'translateX(-5px)' }, { transform: 'translateX(5px)' },
                { transform: 'translateX(-3px)' }, { transform: 'translateX(3px)' },
                { transform: 'translateX(0)' }
            ], { duration: 300, easing: 'ease-in-out' });
      }

      updateHeader(); // Update score display

      // Wait a moment before showing the next question
      setTimeout(() => {
        currentQuestionIndex++;
        showQuestion();
      }, 1800); // Delay to see feedback
    }

    function endGame() {
      questionBox.textContent = "Quiz Complete!";
      optionsWrapper.style.display = 'none'; // Hide options area
      timerEl.style.display = 'none'; // Hide timer

      const finalPercentage = totalQuestions > 0 ? Math.round((score/totalQuestions)*100) : 0;

      feedbackEl.classList.remove('hidden');
      feedbackEl.innerHTML = `
        <h2>Final Score: ${score} / ${totalQuestions} (${finalPercentage}%)</h2>
        <button id='playAgainBtn' class='option-button'>Play Again</button>
        <button id='toggleAnkiBtn' class='option-button' style='margin-top:10px;'>Show Anki Cards</button>
        <div id='ankiCardsContainer'></div>
        <button id='copyAnkiBtn' class='option-button' style='display:none; margin-top:10px;'>Copy Anki Cards</button>
      `;

      // Add event listeners for the end-game buttons
      document.getElementById('playAgainBtn').addEventListener('click', startGame);

      document.getElementById('toggleAnkiBtn').addEventListener('click', function() {
        const container = document.getElementById('ankiCardsContainer');
        const copyBtn = document.getElementById('copyAnkiBtn');
        if (container.style.display === 'none') {
           let content = "";
           questions.forEach((q, index) => {
               // Escape potential HTML in question/answer before embedding
               const escapedQuestion = q.question.replace(/</g, "<").replace(/>/g, ">");
               const escapedAnswer = q.correctAnswer.replace(/</g, "<").replace(/>/g, ">");
               content += `${escapedQuestion}<br><br>{{c1::${escapedAnswer}}}<hr>`; // Use <hr> for separation
           });
           container.innerHTML = content.replace(/<hr>$/, ''); // Remove last separator
           container.style.display = 'block';
           copyBtn.style.display = 'block'; // Show copy button
           this.textContent = "Hide Anki Cards";
        } else {
           container.style.display = 'none';
           copyBtn.style.display = 'none'; // Hide copy button
           this.textContent = "Show Anki Cards";
        }
      });

      document.getElementById('copyAnkiBtn').addEventListener('click', function() {
         const container = document.getElementById('ankiCardsContainer');
         // Create a temporary textarea to copy the *plain text* representation
         const tempTextarea = document.createElement('textarea');
         // Convert HTML breaks and hr to newlines for plain text copy
         tempTextarea.value = container.innerHTML
             .replace(/<hr>/gi, '\n\n')
             .replace(/<br\s*\/?>/gi, '\n')
             .replace(/</g, '<') // Decode HTML entities for plain text copy
             .replace(/>/g, '>')
             .replace(/&/g, '&');
         document.body.appendChild(tempTextarea);
         tempTextarea.select();
         try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(tempTextarea.value).then(() => {
                    this.textContent = "Copied!";
                }).catch(err => { throw err; });
            } else {
                 if (!document.execCommand('copy')) throw new Error('execCommand failed');
                 this.textContent = "Copied!";
            }
         } catch (err) {
             console.error('Copy failed', err);
             this.textContent = "Copy Failed";
             alert("Could not copy text.");
         } finally {
             document.body.removeChild(tempTextarea);
             setTimeout(() => { this.textContent = "Copy Anki Cards"; }, 2000);
         }
      });
    }

    // Helper function for ripple effect
    function createRipple(event) {
        const button = event.currentTarget;
        // Use pageX/pageY for click coordinates relative to the page
        // and getBoundingClientRect for button position relative to viewport
        const rect = button.getBoundingClientRect();
        const ripple = document.createElement('span');
        const diameter = Math.max(button.clientWidth, button.clientHeight);
        const radius = diameter / 2;

        ripple.style.width = ripple.style.height = `${diameter}px`;
        // Calculate position relative to button, accounting for scroll
        ripple.style.left = `${event.clientX - (rect.left + radius)}px`;
        ripple.style.top = `${event.clientY - (rect.top + radius)}px`;
        ripple.className = 'ripple';

        // Ensure ripple exists before removing, handle rapid clicks
        const existingRipple = button.querySelector(".ripple");
        if (existingRipple) {
            existingRipple.remove();
        }

        button.appendChild(ripple);
        // No need for setTimeout removal, animation handles visibility
    }


    // === Initialization ===
    function initializeQuiz() {
        if (!questions || questions.length === 0) {
             questionBox.textContent = "No questions loaded. Please generate again.";
             timerEl.style.display = 'none';
             optionsWrapper.style.display = 'none';
             questionProgressEl.textContent = "Question 0 / 0";
             rawScoreEl.textContent = "Score: 0";
        } else {
            startGame(); // Start the game if questions are present
        }

        // Hide loading overlay and show game container
        loadingOverlay.style.transition = 'opacity 0.5s ease';
        loadingOverlay.style.opacity = '0';
        setTimeout(() => {
            loadingOverlay.style.display = 'none';
            gameContainer.style.display = 'flex'; // Use flex for container layout
        }, 500);
    }


    // Initialize Lottie animation
    var animation = lottie.loadAnimation({
      container: document.getElementById('lottieContainer'),
      renderer: 'svg',
      loop: true,
      autoplay: true,
      path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json' // Use consistent animation
    });

    // Start the quiz once the window is loaded
    window.addEventListener('load', initializeQuiz);

  </script>
</body>
</html>
"""


# ----------------------------
# Flask Routes
# ----------------------------

@app.route("/", methods=["GET"])
def index():
    """Serves the main page."""
    return render_template_string(INDEX_HTML)

@app.route("/generate", methods=["POST"])
def generate():
    """Handles transcript submission and generates cards or game."""
    transcript = request.form.get("transcript", "").strip()
    if not transcript:
        flash("Error: Transcript cannot be empty.")
        return redirect(url_for('index'))

    user_preferences = request.form.get("preferences", "")
    model = request.form.get("model", "gpt-4o-mini")
    max_size_str = request.form.get("max_size", "8000")

    try:
        max_size = int(max_size_str)
        if not (1000 <= max_size <= 16000):
             raise ValueError("Max chunk size must be between 1000 and 16000.")
    except ValueError as e:
        flash(f"Invalid Max Chunk Size: {e}. Using default 8000.")
        max_size = 8000

    mode = request.form.get("mode", "Generate Anki Cards")
    logger.info("Request received - Mode: %s, Model: %s, Max Chunk Size: %d", mode, model, max_size)

    if mode == "Generate Game":
        try:
            questions = get_all_interactive_questions(transcript, user_preferences, max_chunk_size=max_size, model=model)
            logger.info("Generated %d interactive questions.", len(questions))
            if not questions:
                 # Flash message should be set within get_all_interactive_questions
                 # Redirect back to index if generation failed completely
                 # Ensure a message is flashed if none was set before redirecting
                 if not request.args.get('_flashed_messages'):
                     flash("Failed to generate any interactive questions. Please check input or try again.")
                 return redirect(url_for('index'))
            # No need for extra escaping here for Jinja, JS handles the JSON string
            questions_json = json.dumps(questions)
            return render_template_string(INTERACTIVE_HTML, questions_json=questions_json)
        except Exception as e:
            logger.exception("Error during interactive game generation: %s", e)
            flash(f"An unexpected error occurred while generating the game: {e}")
            return redirect(url_for('index'))
    else: # Default to Anki Cards
        try:
            cards = get_all_anki_cards(transcript, user_preferences, max_chunk_size=max_size, model=model)
            logger.info("Generated %d Anki cards.", len(cards))
            if not cards:
                 # Flash message should be set within get_all_anki_cards
                 if not request.args.get('_flashed_messages'):
                     flash("Failed to generate any Anki cards. Please check input or try again.")
                 return redirect(url_for('index'))
            # No need for extra escaping here for Jinja
            cards_json = json.dumps(cards)
            return render_template_string(ANKI_HTML, cards_json=cards_json)
        except Exception as e:
            # This catches errors in get_all_anki_cards *or* render_template_string
            logger.exception("Error during Anki card generation or rendering: %s", e)
            flash(f"An unexpected error occurred while generating Anki cards: {e}")
            return redirect(url_for('index'))


@app.route("/download_apkg", methods=["POST"])
def download_apkg():
    """Generates and serves an Anki deck (.apkg) file from saved card texts."""
    data = request.get_json()
    if not data or "saved_cards" not in data:
        logger.warning("Download request received without card data.")
        return "Invalid request: No saved cards data provided.", 400

    saved_cards = data["saved_cards"]
    if not isinstance(saved_cards, list) or not saved_cards:
        logger.warning("Download request received with empty or invalid card list.")
        return "Invalid request: No valid saved cards provided.", 400

    logger.info("Generating .apkg file for %d cards.", len(saved_cards))

    try:
        # Use a fixed, randomly generated Deck ID for consistency or generate per request
        deck_id = 1987654321 # Example fixed ID
        deck_name = 'Generated Anki Deck'

        # Use the standard Anki Cloze model GUID
        model_id = 998877661 # Standard Anki Cloze model ID
        cloze_model = genanki.Model(
            model_id,
            'Cloze (Generated)',
            fields=[
                {'name': 'Text'},
                {'name': 'Back Extra'} # Anki default cloze has this
            ],
            templates=[
                {
                    'name': 'Cloze',
                    'qfmt': '{{cloze:Text}}',
                    'afmt': '{{cloze:Text}}<br><hr id=answer>{{Back Extra}}', # Standard Anki format
                },
            ],
            css="""
                .card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }
                .cloze { font-weight: bold; color: blue; }
                .nightMode .cloze { color: lightblue; }
            """, # Add basic default CSS
            model_type=genanki.Model.CLOZE
        )

        deck = genanki.Deck(deck_id, deck_name)

        for card_text in saved_cards:
             # Final check on formatting before adding
             cleaned_card_text = fix_cloze_formatting(str(card_text)) # Ensure string type
             note = genanki.Note(
                 model=cloze_model,
                 fields=[cleaned_card_text, 'Generated via Web App'] # Add text to 'Text', optional info to 'Back Extra'
             )
             deck.add_note(note)

        package = genanki.Package(deck)

        # Use try-with-resource for cleaner temp file handling
        with tempfile.NamedTemporaryFile(delete=False, suffix=".apkg") as temp_file:
            temp_file_path = temp_file.name
            package.write_to_file(temp_file_path)

        logger.info("Successfully created .apkg file at %s", temp_file_path)

        # Use Flask's after_this_request to ensure file deletion
        @after_this_request
        def remove_temp_file(response):
            try:
                os.remove(temp_file_path)
                logger.debug("Temporary file %s removed.", temp_file_path)
            except OSError as error: # Catch specific OS error
                logger.error("Error removing temporary file %s: %s", temp_file_path, error)
            return response

        return send_file(
            temp_file_path,
            mimetype='application/vnd.anki.apkg',
            as_attachment=True,
            download_name='generated_anki_deck.apkg'
        )

    except Exception as e:
        logger.exception("Failed to generate or send .apkg file: %s", e)
        return f"Error creating Anki package: {e}", 500


if __name__ == "__main__":
    # Set host='0.0.0.0' to be accessible on the network (e.g., for Render)
    # Debug=False for production/deployment
    port = int(os.environ.get("PORT", 10000)) # Render typically sets PORT env var
    print(f"Starting Flask server on http://0.0.0.0:{port}")
    # Use waitress or gunicorn in production instead of app.run()
    # For development:
    app.run(debug=False, host='0.0.0.0', port=port)

    # Example using waitress (install waitress first: pip install waitress)
    # from waitress import serve
    # serve(app, host='0.0.0.0', port=port)
