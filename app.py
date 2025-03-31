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
# Use environment variable for secret key, essential for production
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a-strong-dev-secret-key-please-change")

# Set up logging
logging.basicConfig(level=logging.INFO) # INFO level is better for production
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    logger.warning("**************************************************")
    logger.warning("WARNING: OPENAI_API_KEY environment variable not set.")
    logger.warning("         API calls to OpenAI will fail.")
    logger.warning("**************************************************")
    # Consider adding a more user-facing warning or disabling features if key is missing
    # flash("Warning: OpenAI API key not configured. AI features may not work.", "warning") # Example flash message
client = OpenAI(api_key=openai_api_key)


# ----------------------------
# Helper Functions (Improved Versions)
# ----------------------------

def preprocess_transcript(text):
    """
    Remove common timestamp patterns (e.g., VTT, SRT, simple times)
    and speaker labels, then normalize whitespace.
    """
    if not text:
        return ""
    # Remove VTT timestamps like 00:00:10.440 --> 00:00:12.440
    processed_text = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\s*\n?', '', text, flags=re.MULTILINE)
    # Remove SRT timestamps like 00:00:06,000 --> 00:00:12,074
    processed_text = re.sub(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*\n?', '', processed_text, flags=re.MULTILINE)
    # Remove simple timestamps like [00:00:00] or 0:00:00.160
    processed_text = re.sub(r'\[?\b\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?\b\]?\s*', '', processed_text)
    # Remove potential SRT index numbers at the start of lines
    processed_text = re.sub(r'^\d+\s*$', '', processed_text, flags=re.MULTILINE)
    # Remove common speaker labels (e.g., "Speaker 1:", "John Doe:") - more robust regex
    processed_text = re.sub(r'^[A-Z][a-zA-Z0-9\s_]+:\s*', '', processed_text, flags=re.MULTILINE)
    # Remove WebVTT header and metadata blocks more thoroughly
    processed_text = re.sub(r'^WEBVTT.*?\n(\n|$)', '', processed_text, flags=re.IGNORECASE)
    processed_text = re.sub(r'^NOTE.*?\n(\n|$)', '', processed_text, flags=re.DOTALL)
    processed_text = re.sub(r'^STYLE.*?\n(\n|$)', '', processed_text, flags=re.DOTALL)
    processed_text = re.sub(r'^REGION.*?\n(\n|$)', '', processed_text, flags=re.DOTALL)
    # Normalize whitespace (replace multiple spaces/newlines with single space)
    cleaned_text = re.sub(r'\s+', ' ', processed_text).strip()
    logger.info(f"Transcript length before cleaning: {len(text)}, after: {len(cleaned_text)}")
    if len(cleaned_text) < 20:
        logger.warning(f"Cleaned transcript seems very short: '{cleaned_text[:50]}...'")
    return cleaned_text

def chunk_text(text, max_size, min_size=200):
    """
    Splits text into chunks of up to max_size characters, trying to break at sentence endings.
    If a chunk is shorter than min_size and there is a previous chunk,
    it is merged with the previous chunk.
    """
    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + max_size, text_len) # Ensure end doesn't exceed length
        if end < text_len:
            # Try to find the last sentence-ending punctuation (. ! ?) before max_size
            last_punct = -1
            search_start = max(start, end - 250) # Search in the last ~250 chars
            for punct in '.!?': # Removed space from punctuation check
                try:
                    # Use rfind for simpler check
                    pos = text.rfind(punct, search_start, end)
                    if pos > last_punct:
                        last_punct = pos
                except ValueError:
                    continue

            if last_punct != -1:
                end = last_punct + 1
            else: # No punctuation found, try last space
                try:
                   last_space = text.rfind(" ", search_start, end)
                   if last_space > start:
                       end = last_space + 1
                   # else: keep original 'end' if no space found
                except ValueError:
                   # Keep original 'end' if no space found either
                   pass

        chunk = text[start:end].strip()

        # Merge small chunks with the previous one if possible and previous exists
        if chunks and chunk and len(chunk) < min_size:
            logger.debug(f"Merging small chunk (len {len(chunk)}) with previous (len {len(chunks[-1])}).")
            chunks[-1] += " " + chunk
            start = end # Adjust start for the next iteration
            continue

        if chunk: # Only add non-empty chunks
             chunks.append(chunk)

        start = end # Move start for the next chunk

    logger.info(f"Split text into {len(chunks)} chunks.")
    # for i, c in enumerate(chunks): logger.debug(f"Chunk {i+1} size: {len(c)}") # Optional: Log sizes
    return chunks

def fix_cloze_formatting(card):
    """
    Ensures cloze deletions use {{c#:text::hint}} or {{c#:text}}.
    Trims whitespace and handles some common API formatting issues.
    """
    if not isinstance(card, str): return card

    # Regex to find potential cloze patterns (flexible spacing, optional hint)
    pattern = re.compile(r"\{{1,2}\s*c(\d+)\s*::\s*(.*?)\s*(?:::\s*(.*?)\s*)?\}\}{1,2}")

    def replace_match(match):
        num = match.group(1).strip()
        answer = match.group(2).strip()
        hint = match.group(3)
        # Clean answer and hint (remove potential nested braces or unwanted chars)
        answer = re.sub(r'[{}]', '', answer)
        if hint:
            hint = hint.strip()
            hint = re.sub(r'[{}]', '', hint)
            return f"{{{{c{num}::{answer}::{hint}}}}}"
        else:
            return f"{{{{c{num}::{answer}}}}}"

    corrected_card = pattern.sub(replace_match, card)

    # Fallback for simple single braces if missed
    if "{{" not in corrected_card and "}}" not in corrected_card:
         if "{c" in corrected_card and "::" in corrected_card and "}" in corrected_card:
              corrected_card = re.sub(r'\{c(\d+)::(.*?)\}', r'{{c\1::\2}}', corrected_card)
              # Re-run main regex to fix potential spacing/hints after fallback
              corrected_card = pattern.sub(replace_match, corrected_card)

    return corrected_card

def get_anki_cards_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o-mini"):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze cards.
    Handles JSON parsing and basic validation.
    """
    user_instr = ""
    if user_preferences.strip():
        user_instr = (f'\nUser Request: {user_preferences.strip()}\nIf no content relevant to the user request '
                      f'is found in this chunk, output a dummy card in the format: '
                      f'"User request not found in {{{{c1::this chunk}}}}."')

    prompt = f"""
You are an expert Anki card creator using cloze deletion. Given the transcript, generate a list of flashcards.
Format each cloze EXACTLY as {{{{c1::hidden text}}}} or {{{{c1::hidden text::hint}}}}.

Follow these formatting instructions precisely:
1.  **Cloze Format**: Use `{{{{c1::text}}}}` or `{{{{c1::text::hint}}}}`. Use `c1`, `c2`, etc., for different concepts on the same card. Use the same number (e.g., `c1`) for related concepts tested together.
2.  **Content**: Focus on college-level or expert-level facts, definitions, or key concepts from the transcript. Avoid trivial information.
3.  **Clarity**: Ensure each blank `[...]` has only one reasonable answer based on the context.
4.  **Style**: Prefer fill-in-the-blank style. For Q&A, use `<br><br>` before the cloze: `Question?<br><br>{{{{c1::Answer}}}}`.
5.  **Definitions**: Use the structure "A {{{{c1::term}}}} is defined as {{{{c2::definition}}}}."
6.  **Hints**: Use `::hint` sparingly for difficult terms or necessary context.
{user_instr}
Output ONLY a valid JSON array of strings. Each string is one flashcard. Do not include ```json``` markers or any other text outside the `[...]` array.

Transcript Chunk:
\"\"\"{transcript_chunk}\"\"\"

JSON Output:
"""
    if not client.api_key:
        logger.error("OpenAI API key not configured.")
        flash("Server configuration error: OpenAI API key is missing.", "error")
        return []
    try:
        logger.debug(f"Sending prompt to {model} (chunk length: {len(transcript_chunk)})")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert Anki card creator outputting ONLY a valid JSON array of strings."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=3000, # Adjusted max tokens
            timeout=120 # Increased timeout
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for chunk: %s", result_text[:500] + "...") # Log beginning

        # Enhanced JSON extraction
        json_str = None
        # Try strict extraction first
        strict_match = re.search(r'^\s*(\[.*\])\s*$', result_text, re.DOTALL)
        if strict_match:
            json_str = strict_match.group(1)
            logger.debug("Strict JSON extraction successful.")
        else:
            # Fallback: find first '[' and last ']'
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx > start_idx:
                 json_str = result_text[start_idx : end_idx + 1]
                 logger.debug("Using fallback JSON extraction (first '[' to last ']').")
            else:
                 logger.error("Could not find JSON array structure '[...]' in API response.")
                 flash("Failed to extract card data structure from AI response for a chunk.", "error")
                 return []

        if json_str:
            try:
                cards = json.loads(json_str)
                if isinstance(cards, list) and all(isinstance(item, str) for item in cards):
                    # Filter out potentially empty strings from API response
                    non_empty_cards = [fix_cloze_formatting(card) for card in cards if card and card.strip()]
                    logger.debug(f"Successfully parsed and fixed {len(non_empty_cards)} non-empty cards from JSON.")
                    return non_empty_cards
                else:
                    logger.error(f"Extracted JSON is not a list of strings: {type(cards)}")
                    flash(f"AI returned unexpected data structure (not a list of strings) for a chunk.", "error")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error(f"JSON parsing error: {parse_err} for content: {json_str[:500]}...")
                flash(f"Failed to parse AI response for a chunk (JSON Error near char {parse_err.pos}).", "error")
                return []

    except Exception as e:
        logger.exception(f"OpenAI API error or other exception during card generation: {e}")
        flash(f"An error occurred communicating with the AI for a chunk: {type(e).__name__}", "error")
        return []
    return [] # Should not be reached normally

def get_all_anki_cards(transcript, user_preferences="", max_chunk_size=8000, model="gpt-4o-mini"):
    """Preprocesses transcript, splits into chunks, gets cards for each."""
    if not transcript or transcript.isspace():
        logger.warning("Received empty or whitespace-only transcript.")
        flash("Transcript content is empty.", "warning")
        return []
    cleaned_transcript = preprocess_transcript(transcript)
    if not cleaned_transcript:
        logger.warning("Transcript became empty after preprocessing.")
        flash("Transcript empty after cleaning. Please check its format.", "warning")
        return []

    logger.info(f"Cleaned transcript length: {len(cleaned_transcript)}")
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks.")
         flash("Failed to split transcript into processable chunks.", "error")
         return []

    all_cards = []
    total_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.isspace():
            logger.debug(f"Skipping empty chunk {i+1}/{total_chunks}")
            continue

        logger.info(f"Processing chunk {i+1}/{total_chunks} (size: {len(chunk)} chars)")
        cards_from_chunk = get_anki_cards_for_chunk(chunk, user_preferences, model=model)
        # Error/success messages flashed within get_anki_cards_for_chunk
        if cards_from_chunk:
             logger.info(f"Chunk {i+1} produced {len(cards_from_chunk)} cards.")
             all_cards.extend(cards_from_chunk)
        else:
             logger.warning(f"Chunk {i+1} produced no cards.")

    logger.info(f"Total Anki flashcards generated: {len(all_cards)}")
    if not all_cards and total_chunks > 0: # Only flash if chunks were processed but yielded nothing
        flash("No Anki cards could be generated from the transcript. Check content quality or AI model.", "warning")
    return all_cards

# ----------------------------
# Interactive Mode Functions (Improved Versions)
# ----------------------------
def get_interactive_questions_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o-mini"):
    """Gets multiple-choice questions for a chunk using OpenAI."""
    user_instr = ""
    if user_preferences.strip():
        user_instr = (f'\nUser Request: {user_preferences.strip()}\nIf no content relevant, output a dummy '
                      f'question in the required JSON format.')

    prompt = f"""
You create interactive multiple-choice questions from transcripts.
Generate a list of questions based on the text below. Each question MUST be a JSON object with keys:
"question": (String) The question text.
"options": (Array of Strings) 3-4 plausible options.
"correctAnswer": (String) The exact text of the correct option from the array.
Optional: "explanation": (String) Brief explanation.

Focus on key concepts. Distractors should be plausible but wrong according to the text.
Output ONLY a valid JSON array of these question objects. No text outside the `[...]`.

{user_instr}

Transcript Chunk:
\"\"\"{transcript_chunk}\"\"\"

JSON Output:
"""
    if not client.api_key:
        logger.error("OpenAI API key not configured.")
        flash("Server configuration error: OpenAI API key is missing.", "error")
        return []
    try:
        logger.debug(f"Sending prompt to {model} for interactive questions (chunk length: {len(transcript_chunk)})")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You create multiple-choice questions as a JSON array."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=3000,
            timeout=120
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for interactive questions: %s", result_text[:500] + "...")

        # Enhanced JSON extraction
        json_str = None
        strict_match = re.search(r'^\s*(\[.*\])\s*$', result_text, re.DOTALL)
        if strict_match:
            json_str = strict_match.group(1)
            logger.debug("Strict JSON extraction successful (interactive).")
        else:
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx > start_idx:
                 json_str = result_text[start_idx : end_idx + 1]
                 logger.debug("Using fallback JSON extraction (interactive).")
            else:
                 logger.error("Could not find JSON array structure '[...]' in API response (interactive).")
                 flash("Failed to extract question data structure from AI response for a chunk.", "error")
                 return []

        if json_str:
            try:
                questions = json.loads(json_str)
                if isinstance(questions, list):
                    # Validate structure of each question object
                    valid_questions = []
                    invalid_count = 0
                    for q in questions:
                        if (isinstance(q, dict) and
                            'question' in q and isinstance(q['question'], str) and q['question'].strip() and
                            'options' in q and isinstance(q['options'], list) and len(q['options']) >= 2 and
                            'correctAnswer' in q and isinstance(q['correctAnswer'], str) and
                            q['correctAnswer'] in q['options']):
                            valid_questions.append(q)
                        else:
                            logger.warning(f"Invalid question structure skipped: {q}")
                            invalid_count += 1
                    if invalid_count > 0:
                         flash(f"Warning: Skipped {invalid_count} invalid questions from AI response.", "warning")
                    logger.debug(f"Successfully parsed {len(valid_questions)} valid interactive questions.")
                    return valid_questions
                else:
                    logger.error(f"Extracted JSON is not a list for interactive questions: {type(questions)}")
                    flash(f"AI returned unexpected data structure (not a list) for interactive questions.", "error")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error(f"JSON parsing error (interactive): {parse_err} for content: {json_str[:500]}...")
                flash(f"Failed to parse AI response for questions (JSON Error near char {parse_err.pos}).", "error")
                return []

    except Exception as e:
        logger.exception(f"OpenAI API error or other exception during question generation: {e}")
        flash(f"An error occurred communicating with the AI for questions: {type(e).__name__}", "error")
        return []
    return [] # Fallback

def get_all_interactive_questions(transcript, user_preferences="", max_chunk_size=8000, model="gpt-4o-mini"):
    """Preprocesses transcript, splits, gets questions for each chunk."""
    if not transcript or transcript.isspace():
        logger.warning("Received empty or whitespace-only transcript for questions.")
        flash("Transcript content is empty.", "warning")
        return []
    cleaned_transcript = preprocess_transcript(transcript)
    if not cleaned_transcript:
        logger.warning("Transcript became empty after preprocessing for questions.")
        flash("Transcript empty after cleaning.", "warning")
        return []

    logger.info(f"Cleaned transcript length for questions: {len(cleaned_transcript)}")
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks for questions.")
         flash("Failed to split transcript into processable chunks.", "error")
         return []

    all_questions = []
    total_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.isspace():
            logger.debug(f"Skipping empty chunk {i+1}/{total_chunks} for questions")
            continue
        logger.info(f"Processing chunk {i+1}/{total_chunks} for interactive questions (size: {len(chunk)})")
        questions_from_chunk = get_interactive_questions_for_chunk(chunk, user_preferences, model=model)
        # Messages flashed within get_interactive_questions_for_chunk
        if questions_from_chunk:
            logger.info(f"Chunk {i+1} produced {len(questions_from_chunk)} interactive questions.")
            all_questions.extend(questions_from_chunk)
        else:
            logger.warning(f"Chunk {i+1} produced no interactive questions.")

    logger.info(f"Total interactive questions generated: {len(all_questions)}")
    if not all_questions and total_chunks > 0:
        flash("No interactive questions could be generated. Check transcript or try different settings.", "warning")
    return all_questions


# ----------------------------
# Embedded HTML Templates
# ----------------------------

# INDEX_HTML (Using the improved version from previous steps)
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
    .flash {
      padding: 12px 15px; margin: 0 auto 20px auto; border-radius: 5px; text-align: center;
      font-weight: bold; max-width: 860px; border: 1px solid transparent;
    }
    .flash.error { background-color: #cf6679; color: #121212; border-color: #b00020; }
    .flash.warning { background-color: #FB8C00; color: #121212; border-color: #EF6C00; }
    .flash.info { background-color: #03a9f4; color: #121212; border-color: #0277bd;}
    .flash.success { background-color: #00c853; color: #121212; border-color: #009624;}


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

      <div id="flash-container"> <!-- Container for dynamic flash messages -->
          {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
              {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
              {% endfor %}
            {% endif %}
          {% endwith %}
      </div>

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

      // Clear previous flash messages
      clearFlashMessages();

      const transcript = document.getElementById('transcript').value;
      if (!transcript || transcript.trim().length < 10) {
          flashMessage('Please paste a transcript (at least 10 characters).', 'error');
          return;
      }

      var overlay = document.getElementById("loadingOverlay");
      overlay.style.display = "flex";

      if (!lottieInstance) {
        try {
            lottieInstance = lottie.loadAnimation({
                container: document.getElementById('lottieContainer'),
                renderer: 'svg',
                loop: true,
                autoplay: true,
                path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
            });
        } catch (e) { console.error("Lottie loading error:", e); }
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
          if (!response.ok) {
              // Attempt to read error text, prioritizing text/plain or text/html
              const contentType = response.headers.get("content-type");
              if (contentType && (contentType.includes("text/html") || contentType.includes("text/plain"))) {
                   return response.text().then(text => {
                       // Try to extract flash message if it's HTML from redirect
                       const match = text.match(/<div class="flash\s*(\w*)?">(.*?)<\/div>/s);
                       const errorMsg = match ? match[2].trim() : `Server error: ${response.status}`;
                       throw new Error(errorMsg);
                   });
              } else {
                  // If not text, throw generic status error
                   throw new Error(`Server error: ${response.status}`);
              }
          }
          return response.text();
      })
      .then(html => {
        // Success: Replace current document content
        document.open();
        document.write(html);
        document.close();
        // No need to hide overlay; new page loads.
      })
      .catch(error => {
        console.error("Form submission error:", error);
        flashMessage(`Error: ${error.message}`, 'error'); // Display error
        if (lottieInstance) lottieInstance.stop();
        overlay.style.display = "none";
      });
    });

    // Helper to show flash messages dynamically
    function flashMessage(message, category = 'info') {
        const container = document.getElementById('flash-container');
        if (!container) return; // Exit if container not found

        const flashDiv = document.createElement('div');
        flashDiv.className = `flash ${category}`; // Apply category class
        flashDiv.textContent = message;
        flashDiv.setAttribute('role', 'alert'); // Accessibility

        // Prepend new message
        container.insertBefore(flashDiv, container.firstChild);

        // Optional: scroll to message
        flashDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // Helper to clear dynamic flash messages
     function clearFlashMessages() {
        const container = document.getElementById('flash-container');
         if (container) {
            // Remove only dynamically added messages if needed, or all
            container.innerHTML = ''; // Clear all messages inside the container
         }
     }
  </script>
</body>
</html>
"""

# ANKI_HTML (Corrected styling + JS from previous step)
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
    }
    .card { font-size: 20px; color: #D7DEE9; line-height: 1.6em; width: 100%; } /* Original font size */
    .cloze { font-weight: bold; color: MediumSeaGreen !important; cursor: pointer; } /* Original color, added important */
    .cloze-hint { font-size: 0.8em; color: #aaa; margin-left: 5px; font-style: italic; } /* Style for hint text */

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
      flex-wrap: wrap; /* Allow wrapping */
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
      flex: 1 1 150px; /* Allow grow, shrink, base width */
      transition: background-color 0.3s; /* Original transition */
      white-space: nowrap;
      text-align: center;
    }
    .bottomButton:hover { background-color: #3700b3; } /* Original hover */
    .bottomButton:active { transform: scale(0.97); }
    .bottomButton:disabled { background-color: #444; color: #888; cursor: not-allowed; opacity: 0.7;}
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
      flex: 1 1 auto; min-width: 120px; text-align: center;
    }
    #copyButton:hover, #downloadButton:hover, #returnButton:hover { filter: brightness(1.2); }
    #copyButton { background-color: #4A90E2; } /* Original */
    #downloadButton { background-color: #03A9F4; } /* Original */
    #returnButton { background-color: #757575; } /* Gray */

    /* Loading Overlay */
    #loadingOverlay { position: fixed; inset: 0; background: rgba(18, 18, 18, 0.9); display: flex; justify-content: center; align-items: center; z-index: 9999; backdrop-filter: blur(5px); transition: opacity 0.5s ease; opacity: 1;}
    #loadingOverlay.hidden { opacity: 0; pointer-events: none; }


    /* Responsive Adjustments */
    @media (max-width: 600px) {
        .card { font-size: 18px; }
        #kard { padding: 15px; min-height: 40vh; }
        .actionButton, .bottomButton, .editButton { font-size: 14px; padding: 10px 15px; }
        /* Keep bottom buttons wrapping naturally */
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

    <!-- Action Controls (Save/Discard) -->
    <div id="actionControls">
      <button id="discardButton" class="actionButton discard" onmousedown="event.preventDefault()" ontouchend="this.blur()">Discard</button>
      <button id="saveButton" class="actionButton save" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save</button>
    </div>

    <!-- Edit Mode Controls -->
    <div id="editControls">
      <button id="cancelEditButton" class="editButton cancelEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Cancel</button>
      <button id="saveEditButton" class="editButton saveEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save Edit</button>
    </div>

    <!-- Bottom Controls Bar -->
    <div class="bottom-control-group">
        <button id="undoButton" class="bottomButton undo" onmousedown="event.preventDefault()" ontouchend="this.blur()">Previous Card</button>
        <button id="editButton" class="bottomButton edit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Edit Card</button>
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
    // === Global Variables and State ===
    const rawCards = {{ cards_json|safe if cards_json else '[]' }};
    let interactiveCards = []; // { target, displayText, exportText, hint }
    let currentIndex = 0;
    let savedCards = []; // Holds exportText of saved cards
    let historyStack = []; // {index, saved, finishedState, view}
    let viewMode = 'loading'; // 'loading', 'card', 'saved', 'edit'
    let savedCardIndexBeforeCart = null;
    let ttsEnabled = false;
    let currentUtterance = null;

    // === DOM Element References ===
    const getEl = (id) => document.getElementById(id); // Helper
    const reviewContainer = getEl('reviewContainer');
    const loadingOverlay = getEl('loadingOverlay');
    const currentEl = getEl("current");
    const totalEl = getEl("total");
    const cardContentEl = getEl("cardContent");
    const kardEl = getEl("kard");
    const actionControls = getEl("actionControls");
    const undoButton = getEl("undoButton");
    const editButton = getEl("editButton");
    const discardButton = getEl("discardButton");
    const saveButton = getEl("saveButton");
    const editControls = getEl("editControls");
    const saveEditButton = getEl("saveEditButton");
    const cancelEditButton = getEl("cancelEditButton");
    const savedCardsContainer = getEl("savedCardsContainer");
    const finishedHeader = getEl("finishedHeader");
    const savedCardsText = getEl("savedCardsText");
    const copyButton = getEl("copyButton");
    const downloadButton = getEl("downloadButton");
    const cartButton = getEl("cartButton");
    const returnButton = getEl("returnButton");
    const savedCountEl = getEl("savedCount");
    const progressEl = getEl("progress");
    const ttsToggleButton = getEl("ttsToggleButton");
    const bottomControlGroups = document.querySelectorAll('.bottom-control-group');

    // === Core Logic Functions ===

    function generateInteractiveCards(cardText) {
      const regex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g;
      const numbers = new Set();
      let tempText = cardText;
      let m;
      while ((m = regex.exec(tempText)) !== null) {
        if (m.index === regex.lastIndex) regex.lastIndex++;
        numbers.add(m[1]);
      }
      regex.lastIndex = 0;

      if (numbers.size === 0) {
        return [{ target: null, displayText: cardText, exportText: cardText, hint: null }];
      }

      const cardsForNote = [];
      Array.from(numbers).sort((a, b) => parseInt(a) - parseInt(b)).forEach(num => {
        const { processedText, hint } = processCloze(cardText, num);
        cardsForNote.push({ target: num, displayText: processedText, exportText: cardText, hint: hint });
      });
      return cardsForNote;
    }

    function processCloze(text, targetClozeNumber) {
        let hintText = null;
        const targetNumStr = String(targetClozeNumber);
        const processed = text.replace(/{{c(\d+)::(.*?)(?:::(.*?))?}}/g, (match, clozeNum, answer, hint) => {
            answer = answer ? answer.trim() : ''; // Handle potential empty answers
            hint = hint ? hint.trim() : null;

            if (clozeNum === targetNumStr) {
                hintText = hint;
                let hintSpan = hint ? `<span class="cloze-hint">(${escapeHtml(hint)})</span>` : '';
                // Only add data-answer if answer is not empty
                const dataAnswerAttr = answer ? `data-answer="${escapeHtml(answer)}"` : '';
                return `<span class="cloze" ${dataAnswerAttr}>[...]${hintSpan}</span>`;
            } else {
                // Return escaped answer, or indicate if empty (optional)
                return answer ? escapeHtml(answer) : '[empty]';
            }
        });
        return { processedText: processed, hint: hintText };
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
        console.debug(`State: view=${viewMode}, index=${currentIndex}, finished=${window.finished}`); // Use window.finished
        // Reset common elements visibility/state
        kardEl.style.display = 'flex';
        cardContentEl.style.display = 'block';
        actionControls.style.display = 'none';
        editControls.style.display = 'none';
        savedCardsContainer.style.display = 'none';
        bottomControlGroups.forEach(group => group.style.display = 'flex');
        progressEl.style.visibility = 'visible';

        const editArea = getEl('editArea');
        if (editArea) editArea.remove();

        switch (viewMode) {
            case 'card':
                if (currentIndex >= interactiveCards.length) {
                    showFinishedScreen(); // Handle index out of bounds
                    return;
                }
                const currentCardData = interactiveCards[currentIndex];
                progressEl.textContent = `Card ${currentIndex + 1} of ${interactiveCards.length}`;
                cardContentEl.innerHTML = currentCardData.displayText;
                updateButtonStates();
                speakCurrentCardState();
                break;

            case 'saved':
                 // This also covers the 'finished' state appearance
                kardEl.style.display = 'none';
                progressEl.style.visibility = 'hidden';
                bottomControlGroups.forEach(group => group.style.display = 'none');

                finishedHeader.textContent = window.finished ? "Review Complete!" : "Saved Cards"; // Use window.finished
                savedCardsText.value = savedCards.length > 0 ? savedCards.join("\n\n") : "No cards saved yet.";
                savedCardsContainer.style.display = 'flex';

                copyButton.textContent = "Copy Text";
                copyButton.disabled = savedCards.length === 0;
                downloadButton.disabled = savedCards.length === 0;
                // Show return button only if not finished
                returnButton.style.display = window.finished ? 'none' : 'block';
                if (!window.finished && savedCardIndexBeforeCart !== null) {
                    returnButton.textContent = `Return to Card ${savedCardIndexBeforeCart + 1}`;
                }
                window.speechSynthesis.cancel();
                break;

            case 'edit':
                progressEl.textContent = `Editing Card ${currentIndex + 1}`;
                cardContentEl.style.display = 'none';
                bottomControlGroups.forEach(group => group.style.display = 'none');

                const newEditArea = document.createElement('textarea');
                newEditArea.id = 'editArea';
                newEditArea.value = interactiveCards[currentIndex]?.exportText || ''; // Handle potential undefined
                kardEl.appendChild(newEditArea);

                editControls.style.display = 'flex';
                window.speechSynthesis.cancel();
                break;

            case 'loading':
                 // Handled by initial setup, maybe show spinner here if needed
                 kardEl.style.display = 'none';
                 progressEl.textContent = "Loading...";
                 bottomControlGroups.forEach(group => group.style.display = 'none');
                 break;
        }
        updateSavedCount(); // Update count regardless of view
    }

    function updateButtonStates() {
         undoButton.disabled = historyStack.length === 0;
         const editingPossible = viewMode === 'card' && !window.finished && interactiveCards.length > 0;
         editButton.disabled = !editingPossible;
         cartButton.disabled = window.finished; // Disable cart if finished
         ttsToggleButton.disabled = window.finished;
    }

    function revealAnswer() {
        if (viewMode !== 'card' || actionControls.style.display === 'flex') return;

        const clozes = cardContentEl.querySelectorAll(".cloze");
        if (clozes.length > 0) {
            clozes.forEach(span => {
                const answer = span.dataset.answer; // Use dataset
                if (answer !== undefined) { // Check if data-answer exists
                    // Render the answer - already escaped by escapeHtml if needed
                    span.innerHTML = answer;
                } else {
                    span.innerHTML = '[?]'; // Indicate missing answer if data-answer wasn't set
                }
                // Remove hint span if it exists within the cloze span
                 const hintSpan = span.querySelector(".cloze-hint");
                 if(hintSpan) hintSpan.remove();
            });
        }
        actionControls.style.display = "flex";
        speakCurrentCardState();
    }

    function handleCardAction(saveCard) {
      if (viewMode !== 'card' || window.finished) return; // Use window.finished

      historyStack.push({
          index: currentIndex,
          saved: savedCards.slice(),
          finishedState: window.finished, // Use window.finished
          view: viewMode
      });
      updateButtonStates(); // Update undo state

      if (saveCard) {
          const cardToSave = interactiveCards[currentIndex]?.exportText;
          if (cardToSave && !savedCards.includes(cardToSave)) {
             savedCards.push(cardToSave);
             updateSavedCount();
          }
      }

      if (currentIndex < interactiveCards.length - 1) {
          currentIndex++;
          updateDisplay(); // Show next card
      } else {
          showFinishedScreen();
      }
    }

    function handleUndo() {
      if (historyStack.length === 0) return;
      window.speechSynthesis.cancel();

      const snapshot = historyStack.pop();
      currentIndex = snapshot.index;
      savedCards = snapshot.saved;
      window.finished = snapshot.finishedState; // Use window.finished
      viewMode = snapshot.view; // Restore view

      updateDisplay(); // Update display based on restored state
      updateButtonStates(); // Update button states again
    }

    function enterEditMode() {
      if (viewMode !== 'card' || window.finished) return; // Use window.finished
      window.speechSynthesis.cancel();
      viewMode = 'edit';
      savedCardIndexBeforeCart = currentIndex;
      updateDisplay();
    }

    function cancelEdit() {
      if (viewMode !== 'edit') return;
      viewMode = 'card';
      // Index doesn't change on cancel
      updateDisplay();
    }

    function saveEdit() {
       if (viewMode !== 'edit') return;
      const editArea = getEl('editArea');
      if (!editArea) return;

      const editedText = editArea.value;
      if (!editedText || editedText.trim() === "") {
          alert("Card text cannot be empty.");
          return;
      }

      const originalIndex = savedCardIndexBeforeCart; // Index before edit started
      const originalExportText = interactiveCards[originalIndex]?.exportText;
      if (originalExportText === undefined) {
           console.error("Cannot save edit, original card data lost.");
           alert("Error saving edit: Original card data not found.");
           viewMode = 'card'; // Revert view
           updateDisplay();
           return;
      }

      const fixedEditedText = fix_cloze_formatting(editedText);
      const newInteractiveCardsForThisNote = generateInteractiveCards(fixedEditedText);

      if (newInteractiveCardsForThisNote.length === 0) {
          alert("Edited text resulted in no valid cloze cards. Check formatting {{c#::...}}.");
          return;
      }

      // Find range of old cards to replace
      let firstIndex = -1;
      let count = 0;
      for(let i = 0; i < interactiveCards.length; i++) {
          if (interactiveCards[i].exportText === originalExportText) {
              if (firstIndex === -1) firstIndex = i;
              count++;
          } else if (firstIndex !== -1) {
              break; // Stop when we hit cards from a different source
          }
      }

      if (firstIndex !== -1) {
          interactiveCards.splice(firstIndex, count, ...newInteractiveCardsForThisNote);
          currentIndex = firstIndex; // Go to the first of the new cards
          console.log(`Replaced ${count} card(s) at index ${firstIndex} with ${newInteractiveCardsForThisNote.length} new card(s).`);
      } else {
          console.error("Could not find original card range during save edit.");
           alert("Error saving edit: Could not find original card position.");
           viewMode = 'card'; // Revert view
           updateDisplay();
           return;
      }

      // Update total and exit edit mode
      totalEl.textContent = interactiveCards.length;
      viewMode = 'card';
      window.finished = false; // No longer finished if cards were changed/added
      updateDisplay();
    }


    function showSavedCards() {
        if (viewMode === 'saved') return; // No change needed
        window.speechSynthesis.cancel();
        savedCardIndexBeforeCart = currentIndex; // Store current card index
        viewMode = 'saved';
        updateDisplay();
    }

    function returnToCard() {
        // Allow returning even if finished, just goes to last known card index
        if (viewMode !== 'saved') return;
        viewMode = 'card';
        // Restore index if valid, otherwise default to 0 or last card
        if (savedCardIndexBeforeCart !== null && savedCardIndexBeforeCart < interactiveCards.length) {
             currentIndex = savedCardIndexBeforeCart;
        } else {
            currentIndex = Math.max(0, interactiveCards.length - 1); // Go to last card if index invalid
        }
        // Ensure finished status reflects reality if returning to review
        window.finished = (currentIndex >= interactiveCards.length - 1 && interactiveCards.length > 0);
        updateDisplay();
    }


    function updateSavedCount() {
        savedCountEl.textContent = savedCards.length;
    }

    function showFinishedScreen() {
        window.speechSynthesis.cancel();
        window.finished = true; // Use window.finished
        viewMode = 'saved';
        updateDisplay();
    }

    function copySavedCardsToClipboard() {
        if (savedCards.length === 0) return;
        savedCardsText.select(); // Select the text
        const textToCopy = savedCardsText.value;

        navigator.clipboard.writeText(textToCopy).then(() => {
            copyButton.textContent = "Copied!";
            setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
        }).catch(err => {
            console.error('Async copy failed, trying execCommand: ', err);
            // Fallback for older browsers
            try {
                if (!document.execCommand("copy")) {
                    throw new Error("execCommand returned false");
                }
                copyButton.textContent = "Copied! (Fallback)";
                setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
            } catch (execErr) {
                console.error('execCommand copy failed: ', execErr);
                copyButton.textContent = "Copy Failed";
                setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
                alert("Could not copy text automatically. Please copy manually.");
            }
        }).finally(() => {
             // Deselect text
             window.getSelection()?.removeAllRanges(); // Use optional chaining
             savedCardsText.blur(); // Remove focus
        });
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
            let filename = "saved_cards.apkg";
            if (disposition && disposition.includes('attachment')) {
                const filenameMatch = disposition.match(/filename\*?=['"]?([^'";]+)['"]?/);
                if (filenameMatch && filenameMatch[1]) {
                  filename = decodeURIComponent(filenameMatch[1]); // Handle potential encoding
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
        })
        .catch(error => {
            console.error("Download failed:", error);
            alert(`Download failed: ${error.message}`);
            downloadButton.textContent = "Download Failed";
        })
        .finally(() => {
             // Re-enable button after a delay
             setTimeout(() => {
                 downloadButton.textContent = "Download .apkg";
                 downloadButton.disabled = (savedCards.length === 0);
             }, 2500);
        });
    }

    // === TTS Functions ===
    function toggleTTS() {
        ttsEnabled = !ttsEnabled;
        ttsToggleButton.textContent = ttsEnabled ? "TTS: ON" : "TTS: OFF";
        ttsToggleButton.classList.toggle("on", ttsEnabled);
        if (ttsEnabled) {
            // Attempt to speak immediately, relies on voices being ready
            speakCurrentCardState();
        } else {
            window.speechSynthesis.cancel();
        }
    }

    function speakText(text) {
        if (!text || !text.trim() || !('speechSynthesis' in window)) {
            if (!('speechSynthesis' in window)) console.warn("Speech Synthesis not supported.");
            return;
        }
        window.speechSynthesis.cancel(); // Cancel previous utterance

        currentUtterance = new SpeechSynthesisUtterance(text);
        // Config (optional)
        // const voices = window.speechSynthesis.getVoices();
        // const preferredVoice = voices.find(v => v.lang.startsWith('en') && v.name.includes('Google')); // Example
        // if (preferredVoice) currentUtterance.voice = preferredVoice;
        currentUtterance.lang = 'en-US';
        currentUtterance.rate = 1.0;
        currentUtterance.pitch = 1.0;
        currentUtterance.volume = 0.9;

        currentUtterance.onend = () => { currentUtterance = null; };
        currentUtterance.onerror = (e) => { console.error("TTS Error:", e.error); currentUtterance = null; };

        // Use a minimal delay
        setTimeout(() => window.speechSynthesis.speak(currentUtterance), 30);
    }

    function getTextToSpeak() {
        if (viewMode !== 'card' || window.finished || currentIndex >= interactiveCards.length) return null;

        const cardData = interactiveCards[currentIndex];
        if (!cardData) return null; // Safety check

        let originalText = cardData.exportText || ''; // Use source text
        const targetClozeNum = cardData.target ? String(cardData.target) : null;
        let textForSpeech = "";
        const isRevealed = actionControls.style.display === "flex";
        const clozeRegex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g;
        let lastIndex = 0;
        let match;

        while ((match = clozeRegex.exec(originalText)) !== null) {
             textForSpeech += originalText.substring(lastIndex, match.index); // Text before cloze
             const [_, num, answer, hint] = match.map(s => s ? s.trim() : s);

             if (isRevealed) {
                 textForSpeech += " " + (answer || "blank"); // Read answer (or "blank" if empty)
             } else {
                 if (targetClozeNum === num) { // Target cloze
                     textForSpeech += " " + (hint || "blank"); // Read hint or "blank"
                 } else { // Non-target cloze
                     textForSpeech += " " + (answer || "blank"); // Read answer
                 }
             }
             lastIndex = clozeRegex.lastIndex;
        }
        textForSpeech += originalText.substring(lastIndex); // Text after last cloze

        // Clean up HTML, normalize whitespace, handle potential double periods/spaces
        textForSpeech = textForSpeech
            .replace(/<br\s*\/?>/gi, '. ')
            .replace(/<[^>]*>/g, ' ') // Remove HTML tags
            .replace(/[\s\.]+\./g, '.') // Fix double periods etc.
            .replace(/\s+/g, ' ') // Normalize spaces
            .trim();

        return textForSpeech;
    }

    function speakCurrentCardState() {
        if (!ttsEnabled || !('speechSynthesis' in window) || window.speechSynthesis.speaking || window.speechSynthesis.pending) {
            return;
        }
        const text = getTextToSpeak();
        if (text) {
            speakText(text);
        }
    }

    // === Event Listeners ===
    function setupEventListeners() {
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
         // Cancel speech on page unload/navigation
         window.addEventListener('beforeunload', () => {
            if ('speechSynthesis' in window) window.speechSynthesis.cancel();
         });
    }

    // === Initialization ===
    function initialize() {
        console.log("Initializing Anki Review...");
        viewMode = 'loading'; // Set initial state
        try {
            if (!rawCards || !Array.isArray(rawCards)) {
                 throw new Error("Initial card data is invalid or missing.");
            }
            rawCards.forEach(cardText => {
                if (typeof cardText === 'string') {
                    interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
                } else {
                     console.warn("Skipping invalid card data item:", cardText);
                }
            });
            console.log(`Processed ${rawCards.length} raw cards into ${interactiveCards.length} interactive cards.`);

        } catch (error) {
            console.error("Error processing initial card data:", error);
            progressEl.textContent = "Error loading cards";
            cardContentEl.innerHTML = `Could not process card data: ${escapeHtml(error.message)}`;
            viewMode = 'error'; // Indicate error state
        }

        if (viewMode !== 'error' && interactiveCards.length === 0) {
            progressEl.textContent = "No cards to review";
            cardContentEl.innerHTML = "No valid Anki cards were generated or found.";
            viewMode = 'finished'; // Treat as finished if no cards
            window.finished = true;
        }

        if (viewMode !== 'error') {
            totalEl.textContent = interactiveCards.length;
            updateSavedCount(); // Initialize saved count display
             // Set initial view mode based on whether cards exist
             viewMode = interactiveCards.length > 0 ? 'card' : 'finished';
             window.finished = (interactiveCards.length === 0); // Set global finished state
             updateDisplay(); // Show first card or finished screen
             updateButtonStates(); // Set initial button states
        }

        setupEventListeners(); // Attach event listeners

        // Hide loading overlay after setup
        setTimeout(() => { // Add slight delay for smoother transition
            loadingOverlay.classList.add('hidden');
            reviewContainer.style.display = 'flex';
        }, 300);
    }

    // Initialize Lottie animation
    try {
        const lottieAnimation = lottie.loadAnimation({
          container: getEl('lottieContainer'),
          renderer: 'svg', loop: true, autoplay: true,
          path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
        });
    } catch(e) { console.error("Lottie loading error:", e); }


    // Defer initialization until DOM is fully ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize(); // DOM is already ready
    }

  </script>
</body>
</html>
"""

# INTERACTIVE_HTML (With the fixed parenthesis)
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
    #loadingOverlay { position: fixed; inset: 0; background: rgba(18, 18, 18, 0.9); display: flex; justify-content: center; align-items: center; z-index: 9999; backdrop-filter: blur(5px); transition: opacity 0.5s ease; opacity: 1;}
     #loadingOverlay.hidden { opacity: 0; pointer-events: none; }
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
    let timeLeft = 15;
    const totalQuestions = Array.isArray(questions) ? questions.length : 0; // Ensure questions is array

    // DOM Elements
    const getEl = (id) => document.getElementById(id);
    const gameContainer = getEl('gameContainer');
    const loadingOverlay = getEl('loadingOverlay');
    const questionProgressEl = getEl('questionProgress');
    const rawScoreEl = getEl('rawScore');
    const timerEl = getEl('timer');
    const questionBox = getEl('questionBox');
    const optionsWrapper = getEl('optionsWrapper');
    const feedbackEl = getEl('feedback');

    // === Core Game Functions ===

    function startGame() {
      score = 0;
      currentQuestionIndex = 0;
      feedbackEl.classList.add('hidden');
      feedbackEl.innerHTML = '';
      optionsWrapper.style.display = 'block';
      timerEl.style.display = 'block';
      questionBox.style.display = 'flex'; // Ensure question box is visible
      updateHeader();
      showQuestion();
    }

    function updateHeader() {
      questionProgressEl.textContent = `Question ${currentQuestionIndex + 1} / ${totalQuestions}`;
      rawScoreEl.textContent = `Score: ${score}`;
    }

    function startTimer() {
      timeLeft = 15;
      timerEl.textContent = `Time: ${timeLeft}`;
      clearInterval(timerInterval);

      timerInterval = setInterval(() => {
        timeLeft--;
        timerEl.textContent = `Time: ${timeLeft}`;
        if (timeLeft <= 0) {
          clearInterval(timerInterval);
          handleAnswerSelection(null); // Time's up
        }
      }, 1000);
    }

    function showQuestion() {
      if (currentQuestionIndex >= totalQuestions) {
        endGame();
        return;
      }

      const currentQuestion = questions[currentQuestionIndex];
      // Basic check for valid question structure
      if (!currentQuestion || typeof currentQuestion.question !== 'string' || !Array.isArray(currentQuestion.options)) {
          console.error("Invalid question data at index:", currentQuestionIndex, currentQuestion);
          // Skip to next or end game? For now, end game.
          endGame("Error: Invalid question data encountered.");
          return;
      }


      questionBox.textContent = currentQuestion.question;
      optionsWrapper.innerHTML = "";
      const ul = document.createElement('ul');
      ul.className = 'options';

      const optionsShuffled = [...currentQuestion.options];
      for (let i = optionsShuffled.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [optionsShuffled[i], optionsShuffled[j]] = [optionsShuffled[j], optionsShuffled[i]];
      }

      optionsShuffled.forEach(option => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.textContent = option;
        button.className = 'option-button';
        button.onmousedown = (e) => { e.preventDefault(); };
        button.onclick = (event) => {
             createRipple(event);
             handleAnswerSelection(option);
        };
        li.appendChild(button);
        ul.appendChild(li);
      });

      optionsWrapper.appendChild(ul);
      startTimer();
      updateHeader();
    }

    function handleAnswerSelection(selectedOption) {
      clearInterval(timerInterval);
      const currentQuestion = questions[currentQuestionIndex];
      // Add check in case question data is bad
      if (!currentQuestion || typeof currentQuestion.correctAnswer !== 'string') {
           console.error("Cannot process answer, invalid question data:", currentQuestion);
           endGame("Error: Could not process answer.");
           return;
      }

      const buttons = optionsWrapper.querySelectorAll('.option-button');
      const isCorrect = (selectedOption === currentQuestion.correctAnswer);

      buttons.forEach(button => {
        button.disabled = true;
        if (button.textContent === currentQuestion.correctAnswer) {
          button.classList.add('correct');
        } else if (button.textContent === selectedOption) {
          button.classList.add('incorrect');
        }
      });

      if (isCorrect) {
        score++;
        try {
            confetti({ particleCount: 120, spread: 80, origin: { y: 0.6 }, colors: ['#bb86fc', '#03dac6', '#f0f0f0'] });
        } catch (e) { console.warn("Confetti error:", e); }
      } else if (selectedOption !== null) {
           try {
               questionBox.animate([ { transform: 'translateX(-5px)' }, { transform: 'translateX(5px)' }, { transform: 'translateX(-3px)' }, { transform: 'translateX(3px)' }, { transform: 'translateX(0)' } ], { duration: 300, easing: 'ease-in-out' });
           } catch (e) { console.warn("Animation error:", e); }
      }

      updateHeader();

      setTimeout(() => {
        currentQuestionIndex++;
        showQuestion();
      }, 1800);
    }

    function endGame(errorMessage = null) {
      clearInterval(timerInterval); // Ensure timer is stopped
      questionBox.textContent = errorMessage || "Quiz Complete!";
      questionBox.style.display = 'flex'; // Ensure it's visible for message
      optionsWrapper.style.display = 'none';
      timerEl.style.display = 'none';

      const finalPercentage = totalQuestions > 0 ? Math.round((score / totalQuestions) * 100) : 0; // *** CORRECTED PARENTHESES ***

      feedbackEl.classList.remove('hidden');
      if (!errorMessage) {
          feedbackEl.innerHTML = `
            <h2>Final Score: ${score} / ${totalQuestions} (${finalPercentage}%)</h2>
            <button id='playAgainBtn' class='option-button'>Play Again</button>
            <button id='toggleAnkiBtn' class='option-button' style='margin-top:10px;'>Show Anki Cards</button>
            <div id='ankiCardsContainer'></div>
            <button id='copyAnkiBtn' class='option-button' style='display:none; margin-top:10px;'>Copy Anki Cards</button>
          `;
          // Add event listeners only if game ended normally
          getEl('playAgainBtn').addEventListener('click', startGame);
          getEl('toggleAnkiBtn').addEventListener('click', toggleAnkiDisplay);
          getEl('copyAnkiBtn').addEventListener('click', copyAnkiCards);
      } else {
           feedbackEl.innerHTML = `<h2>An Error Occurred</h2><p>${escapeHtml(errorMessage)}</p><button id='playAgainBtn' class='option-button'>Try Again?</button>`;
            getEl('playAgainBtn').addEventListener('click', () => window.location.reload()); // Simple reload on error
      }
    }

    function toggleAnkiDisplay() {
        const container = getEl('ankiCardsContainer');
        const copyBtn = getEl('copyAnkiBtn');
        const toggleBtn = getEl('toggleAnkiBtn'); // Need reference to toggle button itself
        if (!container || !copyBtn || !toggleBtn) return; // Safety check

        if (container.style.display === 'none') {
           let content = "";
           questions.forEach((q) => {
               if (q && typeof q.question === 'string' && typeof q.correctAnswer === 'string') {
                    const escapedQuestion = q.question.replace(/</g, "<").replace(/>/g, ">");
                    const escapedAnswer = q.correctAnswer.replace(/</g, "<").replace(/>/g, ">");
                    content += `${escapedQuestion}<br><br>{{c1::${escapedAnswer}}}<hr>`;
               }
           });
           container.innerHTML = content.replace(/<hr>$/, '');
           container.style.display = 'block';
           copyBtn.style.display = 'block';
           toggleBtn.textContent = "Hide Anki Cards";
        } else {
           container.style.display = 'none';
           copyBtn.style.display = 'none';
           toggleBtn.textContent = "Show Anki Cards";
        }
    }

     function copyAnkiCards() {
         const container = getEl('ankiCardsContainer');
         const copyBtn = getEl('copyAnkiBtn');
         if (!container || !copyBtn) return;

         const tempTextarea = document.createElement('textarea');
         tempTextarea.value = container.innerHTML
             .replace(/<hr>/gi, '\n\n')
             .replace(/<br\s*\/?>/gi, '\n')
             .replace(/</g, '<')
             .replace(/>/g, '>')
             .replace(/&/g, '&'); // Basic decoding
         document.body.appendChild(tempTextarea);
         tempTextarea.select();
         try {
            if (!document.execCommand('copy')) {
                 throw new Error('execCommand failed');
            }
            copyBtn.textContent = "Copied!";
         } catch (err) {
             console.error('Copy failed', err);
             copyBtn.textContent = "Copy Failed";
             alert("Could not copy text automatically.");
         } finally {
             document.body.removeChild(tempTextarea);
             setTimeout(() => { if(copyBtn) copyBtn.textContent = "Copy Anki Cards"; }, 2000);
         }
     }


    // Helper function for ripple effect
    function createRipple(event) {
        const button = event.currentTarget;
        const rect = button.getBoundingClientRect();
        const ripple = document.createElement('span');
        const diameter = Math.max(button.clientWidth, button.clientHeight);
        const radius = diameter / 2;

        ripple.style.width = ripple.style.height = `${diameter}px`;
        ripple.style.left = `${event.clientX - (rect.left + radius)}px`;
        ripple.style.top = `${event.clientY - (rect.top + radius)}px`;
        ripple.className = 'ripple';

        const existingRipple = button.querySelector(".ripple");
        if (existingRipple) existingRipple.remove();

        button.appendChild(ripple);
        // Animation handles removal implicitly now
    }


    // === Initialization ===
    function initializeQuiz() {
        console.log("Initializing Interactive Quiz...");
        if (!Array.isArray(questions) || totalQuestions === 0) {
             questionBox.textContent = "No questions loaded. Please generate again.";
             timerEl.style.display = 'none';
             optionsWrapper.style.display = 'none';
             questionProgressEl.textContent = "Question 0 / 0";
             rawScoreEl.textContent = "Score: 0";
        } else {
            startGame(); // Start the game if questions are valid
        }

        // Hide loading overlay
        setTimeout(() => { // Delay slightly for smoother fade
             loadingOverlay.classList.add('hidden');
             gameContainer.style.display = 'flex';
         }, 300);
    }

    // Initialize Lottie animation
    try {
        const animation = lottie.loadAnimation({
          container: getEl('lottieContainer'),
          renderer: 'svg', loop: true, autoplay: true,
          path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
        });
    } catch(e) { console.error("Lottie loading error:", e); }


    // Start the quiz once the DOM is ready
     if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeQuiz);
    } else {
        initializeQuiz(); // DOM is already ready
    }

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
        flash("Error: Transcript cannot be empty.", "error")
        return redirect(url_for('index'))

    user_preferences = request.form.get("preferences", "")
    model = request.form.get("model", "gpt-4o-mini")
    max_size_str = request.form.get("max_size", "8000")

    try:
        max_size = int(max_size_str)
        if not (1000 <= max_size <= 16000):
             raise ValueError("Max chunk size must be between 1000 and 16000.")
    except ValueError as e:
        flash(f"Invalid Max Chunk Size: {e}. Using default 8000.", "warning")
        max_size = 8000

    mode = request.form.get("mode", "Generate Anki Cards")
    logger.info(f"Request received - Mode: {mode}, Model: {model}, Max Chunk Size: {max_size}")

    # Check API key status before proceeding
    if not openai_api_key:
         flash("Server Error: AI Service is not configured.", "error")
         return redirect(url_for('index'))


    if mode == "Generate Game":
        try:
            questions = get_all_interactive_questions(transcript, user_preferences, max_chunk_size=max_size, model=model)
            logger.info(f"Generated {len(questions)} interactive questions.")
            if not questions:
                 # Flash message set within get_all_interactive_questions if generation failed
                 # Redirect back if no questions were generated at all
                 if not get_flashed_messages(): # Check if a message was already flashed
                    flash("Failed to generate interactive questions. Please check input or logs.", "warning")
                 return redirect(url_for('index'))
            # Pass questions directly, no extra escaping needed here
            questions_json = json.dumps(questions)
            return render_template_string(INTERACTIVE_HTML, questions_json=questions_json)
        except Exception as e:
            logger.exception(f"Error during interactive game generation pipeline: {e}")
            flash(f"An unexpected error occurred generating the game: {type(e).__name__}", "error")
            return redirect(url_for('index'))
    else: # Default to Anki Cards
        try:
            cards = get_all_anki_cards(transcript, user_preferences, max_chunk_size=max_size, model=model)
            logger.info(f"Generated {len(cards)} Anki cards.")
            if not cards:
                 if not get_flashed_messages():
                    flash("Failed to generate Anki cards. Please check input or logs.", "warning")
                 return redirect(url_for('index'))
            # Pass cards directly
            cards_json = json.dumps(cards)
            # Make sure the template variable name matches {{ cards_json|safe ... }}
            return render_template_string(ANKI_HTML, cards_json=cards_json)
        except Exception as e:
            logger.exception(f"Error during Anki card generation pipeline or rendering: {e}")
            flash(f"An unexpected error occurred generating Anki cards: {type(e).__name__}", "error")
            return redirect(url_for('index'))


@app.route("/download_apkg", methods=["POST"])
def download_apkg():
    """Generates and serves an Anki deck (.apkg) file."""
    # Basic input validation
    if not request.is_json:
        logger.warning("Download request received without JSON data.")
        return "Invalid request: Content-Type must be application/json.", 415 # Unsupported Media Type

    data = request.get_json()
    if not data or "saved_cards" not in data:
        logger.warning("Download request received missing 'saved_cards' key.")
        return "Invalid request: Missing 'saved_cards' data.", 400

    saved_cards = data["saved_cards"]
    if not isinstance(saved_cards, list) or not saved_cards:
        logger.warning("Download request received with empty or invalid 'saved_cards' list.")
        return "Invalid request: 'saved_cards' must be a non-empty list.", 400
    # Further validation: ensure list contains strings
    if not all(isinstance(card, str) for card in saved_cards):
         logger.warning("Download request 'saved_cards' list contains non-string elements.")
         return "Invalid request: 'saved_cards' list must only contain strings.", 400


    logger.info(f"Generating .apkg file for {len(saved_cards)} cards.")

    try:
        # --- Deck and Model Definition ---
        # Generate stable IDs using a namespace (e.g., your app name)
        deck_id = genanki.guid_for("MyAnkiAppDeck_v1")
        model_id = genanki.guid_for("MyAnkiAppClozeModel_v1")
        deck_name = 'Generated Anki Deck'

        cloze_model = genanki.Model(
            model_id,
            'My Cloze (Generated)', # Custom model name
            fields=[{'name': 'Text'}, {'name': 'Back Extra'}],
            templates=[{
                'name': 'Cloze',
                'qfmt': '{{cloze:Text}}',
                'afmt': '{{cloze:Text}}<hr id="answer">{{Back Extra}}',
            }],
            css="""
                .card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }
                .cloze { font-weight: bold; color: blue; }
                .nightMode .cloze { color: lightblue; }
                #answer { margin-top: 1em; border-top: 1px dashed #ccc; }
                .mobile .card { font-size: 18px; }
            """,
            model_type=genanki.Model.CLOZE
        )
        deck = genanki.Deck(deck_id, deck_name)

        # --- Add Notes ---
        added_notes_count = 0
        for card_text in saved_cards:
             cleaned_card_text = fix_cloze_formatting(card_text)
             if not cleaned_card_text or not cleaned_card_text.strip():
                 logger.warning("Skipping empty card string during apkg generation.")
                 continue # Skip empty strings
             # Basic check if it looks like a cloze card
             if "{{c" not in cleaned_card_text:
                 logger.warning(f"Skipping card missing cloze format: {cleaned_card_text[:50]}...")
                 continue

             note = genanki.Note(
                 model=cloze_model,
                 fields=[cleaned_card_text, 'Generated via Web App']
             )
             deck.add_note(note)
             added_notes_count += 1

        if added_notes_count == 0:
            logger.warning("No valid cards were added to the deck.")
            return "Error: No valid cards found to create Anki package.", 400

        package = genanki.Package(deck)

        # --- File Handling ---
        temp_file_path = None # Ensure path is defined outside try block
        try:
            # Use NamedTemporaryFile ensuring it's deleted even on error
            with tempfile.NamedTemporaryFile(delete=False, suffix=".apkg") as temp_file:
                temp_file_path = temp_file.name
                package.write_to_file(temp_file_path)
            logger.info(f"Successfully created .apkg file at {temp_file_path}")

            # Prepare response before setting up cleanup
            response = send_file(
                temp_file_path,
                mimetype='application/vnd.anki.apkg', # Standard mimetype
                as_attachment=True,
                download_name='generated_anki_deck.apkg'
            )

            # Use after_this_request for cleanup
            @after_this_request
            def remove_temp_file(response_obj):
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        logger.debug(f"Temporary file {temp_file_path} removed.")
                    except OSError as error:
                        logger.error(f"Error removing temporary file {temp_file_path}: {error}")
                return response_obj # Must return the response

            return response

        except Exception as e_file:
             # Catch errors during file writing or sending
             logger.exception(f"Error during file writing or sending: {e_file}")
             # Clean up temp file if it exists and writing failed
             if temp_file_path and os.path.exists(temp_file_path):
                 try: os.remove(temp_file_path)
                 except Exception as e_clean: logger.error(f"Error cleaning up failed temp file: {e_clean}")
             return f"Error creating or sending file: {e_file}", 500

    except Exception as e_gen:
        # Catch errors during Deck/Model/Note generation
        logger.exception(f"Failed to generate Anki package components: {e_gen}")
        return f"Error creating Anki package: {e_gen}", 500


if __name__ == "__main__":
    # Get port from environment variable or default to 10000
    port = int(os.environ.get("PORT", 10000))
    # Use '0.0.0.0' to be accessible externally (like on Render)
    host = '0.0.0.0'
    # Turn off debug mode for production
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    print(f" --- Starting Flask server ---")
    print(f" * Environment: {'development' if debug_mode else 'production'}")
    print(f" * Debug Mode: {debug_mode}")
    print(f" * Running on http://{host}:{port}")
    print(f" * OpenAI Key Configured: {'Yes' if openai_api_key else 'NO - API calls will fail!'}")
    print(f"-----------------------------")

    if debug_mode:
        app.run(debug=True, host=host, port=port)
    else:
        # Use a production-ready WSGI server like waitress or gunicorn
        # Option 1: Waitress (cross-platform)
        try:
            from waitress import serve
            serve(app, host=host, port=port, threads=8) # Adjust threads as needed
        except ImportError:
            print("Waitress not found. Falling back to Flask's development server (NOT recommended for production).")
            print("Install waitress: pip install waitress")
            app.run(host=host, port=port) # Fallback, but not ideal

        # Option 2: Gunicorn (Unix-like systems - often used by Render)
        # Gunicorn is typically run via command line: `gunicorn --bind 0.0.0.0:10000 app:app`
        # The `if __name__ == '__main__':` block isn't usually run when using Gunicorn externally.
        # If you needed to run Gunicorn programmatically (less common):
        # Note: Requires Gunicorn installed
        # class StandaloneApplication(gunicorn.app.base.BaseApplication):
        #     def __init__(self, app, options=None):
        #         self.options = options or {}
        #         self.application = app
        #         super().__init__()
        #     def load_config(self):
        #         config = {key: value for key, value in self.options.items()
        #                   if key in self.cfg.settings and value is not None}
        #         for key, value in config.items():
        #             self.cfg.set(key.lower(), value)
        #     def load(self):
        #         return self.application
        # if not debug_mode:
        #      options = {
        #          'bind': f'{host}:{port}',
        #          'workers': 4, # Adjust based on CPU cores
        #          'timeout': 120,
        #      }
        #      StandaloneApplication(app, options).run()
