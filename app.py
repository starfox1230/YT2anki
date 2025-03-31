import os
import re
import json
import logging
import tempfile
import genanki
from flask import Flask, request, redirect, url_for, flash, render_template_string, send_file

# Updated OpenAI API import and initialization.
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

# Set up logging
logging.basicConfig(level=logging.INFO) # Changed to INFO for production, DEBUG is very verbose
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
if not os.environ.get("OPENAI_API_KEY"):
    logger.warning("OPENAI_API_KEY environment variable not set. API calls will fail.")


# ----------------------------
# Helper Functions
# ----------------------------

def preprocess_transcript(text):
    """
    Remove common timestamp patterns (e.g. "00:00:00.160" or "00:00:00,160")
    and normalize whitespace.
    """
    # Remove timestamps like HH:MM:SS.ms or HH:MM:SS,ms
    text_no_timestamps = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}', '', text) # Handle VTT format
    text_no_timestamps = re.sub(r'\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?', '', text_no_timestamps) # General time patterns
    text_no_timestamps = re.sub(r'\[\d{2}:\d{2}:\d{2}\]', '', text_no_timestamps) # [HH:MM:SS] pattern
    # Remove speaker labels like "Speaker 1:", "John Doe:", etc. (adjust regex as needed)
    text_no_speakers = re.sub(r'^[A-Za-z0-9\s]+:\s*', '', text_no_timestamps, flags=re.MULTILINE)
    # Normalize whitespace (replace multiple spaces/newlines with single space)
    cleaned_text = re.sub(r'\s+', ' ', text_no_speakers).strip()
    return cleaned_text

def chunk_text(text, max_size, min_size=100):
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
            # Try to find the last sentence-ending punctuation before max_size
            last_punct = -1
            for punct in ['.', '!', '?']:
                pos = text.rfind(punct, start, end)
                if pos > last_punct:
                    last_punct = pos

            if last_punct != -1 and last_punct > start: # Found suitable punctuation
                end = last_punct + 1
            else: # No punctuation found, try last space
                last_space = text.rfind(" ", start, end)
                if last_space != -1 and last_space > start:
                    end = last_space + 1
                # If no space found either, just cut at max_size (will happen rarely with large max_size)

        chunk = text[start:end].strip()

        # Merge small chunks with the previous one if possible
        if chunks and len(chunk) < min_size and chunk:
            logger.debug("Merging small chunk (len %d) with previous.", len(chunk))
            chunks[-1] += " " + chunk
            start = end # Adjust start for the next iteration
            continue # Skip appending this small chunk separately

        if chunk: # Only add non-empty chunks
             chunks.append(chunk)

        start = end # Move start for the next chunk

    logger.debug("Total number of chunks after splitting: %d", len(chunks))
    # Log chunk sizes for debugging
    # for i, c in enumerate(chunks):
    #     logger.debug(f"Chunk {i+1} size: {len(c)}")
    return chunks


def fix_cloze_formatting(card):
    """
    Ensures that cloze deletions in the card use exactly two curly braces on each side.
    Handles cases like {c1::...}, { { c1 :: ... } }, etc.
    Also trims whitespace around the cloze number, answer, and hint.
    """
    # Regex to find potential cloze patterns (flexible with spacing and brace count)
    # {{? means optional opening brace
    # \s* means optional whitespace
    # c(\d+) captures the number
    # :: the separator
    # (.*?) captures the answer (non-greedy)
    # (?: a non-capturing group for the optional hint part
    #   :: another separator
    #   (.*?) captures the hint (non-greedy)
    # )? makes the hint part optional
    # \s* optional whitespace
    # }}? optional closing brace
    pattern = re.compile(r"\{{1,2}\s*c(\d+)\s*::\s*(.*?)\s*(?:::\s*(.*?)\s*)?\}\}{1,2}")

    def replace_match(match):
        num = match.group(1).strip()
        answer = match.group(2).strip()
        hint = match.group(3)
        if hint:
            hint = hint.strip()
            return f"{{{{c{num}::{answer}::{hint}}}}}"
        else:
            return f"{{{{c{num}::{answer}}}}}"

    corrected_card = pattern.sub(replace_match, card)

    # Simple replacement for cases missed by regex (e.g., single brace only)
    # This is less precise but acts as a fallback
    if "{{" not in corrected_card and "}}" not in corrected_card:
         corrected_card = corrected_card.replace("{c", "{{c")
         corrected_card = corrected_card.replace("::", "::") # No change needed usually
         corrected_card = re.sub(r'(?<!})}(?!})', '}}', corrected_card) # Fix single closing braces

    # Ensure hints are correctly formatted if present (double check)
    corrected_card = re.sub(r'({{c\d+::[^:]+):([^:}]+)}}', r'\1::\2}}', corrected_card)

    return corrected_card

def get_anki_cards_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o"):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    """
    user_instr = ""
    if user_preferences.strip():
        user_instr = f'\nUser Request: {user_preferences.strip()}\nIf no content relevant to the user request is found in this chunk, output a dummy card in the format: "User request not found in {{{{c1::this chunk}}}}."'
    
    # Added hint instruction
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
   • If multiple deletions belong to the same testable concept, they should use the same number:
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
Ensure you output ONLY a valid JSON array of strings, with no additional commentary or markdown. Each string in the array represents one flashcard.

Transcript:
\"\"\"{transcript_chunk}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model=model,
            # Specify JSON mode if supported and desired, although parsing fallback exists
            # response_format={ "type": "json_object" }, # May require prompt adjustments for object wrapper
            messages=[
                {"role": "system", "content": "You are an expert Anki card creator outputting valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6, # Slightly lower temp might improve format consistency
            max_tokens=2500, # Increased slightly
            timeout=90 # Increased timeout
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for chunk: %s", result_text)

        # Attempt to extract JSON array even if surrounded by other text
        json_match = re.search(r'\[.*\]', result_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            try:
                cards = json.loads(json_str)
                if isinstance(cards, list) and all(isinstance(item, str) for item in cards):
                    fixed_cards = [fix_cloze_formatting(card) for card in cards]
                    logger.debug("Successfully parsed and fixed cards from extracted JSON.")
                    return fixed_cards
                else:
                    logger.error("Extracted JSON is not a list of strings: %s", json_str)
                    flash(f"API returned unexpected JSON structure for a chunk. Please check logs.")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error("JSON parsing error for extracted chunk: %s\nContent: %s", parse_err, json_str)
                flash(f"Failed to parse JSON from API response for a chunk: {parse_err}")
                return []
        else:
             logger.error("Could not find JSON array structure in API response: %s", result_text)
             flash("API response did not contain a valid JSON array for a chunk.")
             return []

    except Exception as e:
        logger.exception("OpenAI API error or other exception for chunk: %s", e)
        flash(f"An error occurred while generating cards for a chunk: {e}")
        return []


def get_all_anki_cards(transcript, user_preferences="", max_chunk_size=4000, model="gpt-4o"):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk.
    Returns a combined list of all flashcards.
    """
    if not transcript or transcript.isspace():
        logger.warning("Received empty or whitespace-only transcript.")
        flash("Transcript content is empty.")
        return []
    cleaned_transcript = preprocess_transcript(transcript)
    if not cleaned_transcript:
        logger.warning("Transcript became empty after preprocessing.")
        flash("Transcript empty after cleaning. Please check format (e.g., remove only timestamps/speaker labels).")
        return []

    logger.debug("Cleaned transcript length: %d", len(cleaned_transcript))
    logger.debug("Cleaned transcript (first 200 chars): %s", cleaned_transcript[:200])
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks.")
         flash("Failed to split transcript into processable chunks.")
         return []

    all_cards = []
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.isspace():
            logger.debug("Skipping empty chunk %d", i+1)
            continue
        logger.info("Processing chunk %d/%d (size: %d)", i+1, len(chunks), len(chunk))
        cards = get_anki_cards_for_chunk(chunk, user_preferences, model=model)
        if cards: # Only extend if cards were successfully generated
             logger.info("Chunk %d produced %d cards.", i+1, len(cards))
             all_cards.extend(cards)
        else:
             logger.warning("Chunk %d produced no cards.", i+1)
             # Decide if we should flash a message per failed chunk or just one at the end
             # flash(f"Warning: Could not generate cards for chunk {i+1}.") # Optional: More verbose feedback

    logger.info("Total flashcards generated: %d", len(all_cards))
    if not all_cards:
        flash("No Anki cards could be generated from the provided transcript and settings. Check the transcript quality or try different settings.")
    return all_cards

# ----------------------------
# New Functions for Interactive Mode
# ----------------------------

def get_interactive_questions_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o"):
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
- Ensure the output is ONLY a valid JSON array of these question objects. No introductory text, comments, or markdown formatting outside the JSON strings.
{user_instr}

Transcript:
\"\"\"{transcript_chunk}\"\"\"

Output JSON array:
"""
    try:
        response = client.chat.completions.create(
            model=model,
            # Specify JSON mode if supported by the model version
            # response_format={ "type": "json_object" }, # May need prompt adjustment
            messages=[
                {"role": "system", "content": "You are a helpful assistant creating multiple-choice questions in JSON format."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2500, # Increased slightly
            timeout=90 # Increased timeout
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for interactive questions: %s", result_text)

        # Attempt to extract JSON array
        json_match = re.search(r'\[.*\]', result_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            try:
                questions = json.loads(json_str)
                # Basic validation of the structure
                if isinstance(questions, list) and all(isinstance(q, dict) and 'question' in q and 'options' in q and 'correctAnswer' in q for q in questions):
                     logger.debug("Successfully parsed interactive questions from extracted JSON.")
                     return questions
                else:
                    logger.error("Extracted JSON is not a valid list of question objects: %s", json_str)
                    flash("API returned an invalid structure for interactive questions for a chunk.")
                    return []
            except json.JSONDecodeError as parse_err:
                logger.error("JSON parsing error for interactive questions chunk: %s\nContent: %s", parse_err, json_str)
                flash(f"Failed to parse JSON for interactive questions for a chunk: {parse_err}")
                return []
        else:
            logger.error("Could not find JSON array structure in API response for interactive questions: %s", result_text)
            flash("API response did not contain a valid JSON array for interactive questions for a chunk.")
            return []

    except Exception as e:
        logger.exception("OpenAI API error or other exception for interactive questions chunk: %s", e)
        flash(f"An error occurred while generating interactive questions for a chunk: {e}")
        return []

def get_all_interactive_questions(transcript, user_preferences="", max_chunk_size=4000, model="gpt-4o"):
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
        flash("Transcript empty after cleaning. Please check format.")
        return []

    logger.debug("Cleaned transcript length for interactive questions: %d", len(cleaned_transcript))
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    if not chunks:
         logger.warning("Transcript could not be split into chunks for interactive questions.")
         flash("Failed to split transcript into processable chunks.")
         return []

    all_questions = []
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.isspace():
            logger.debug("Skipping empty chunk %d for interactive questions", i+1)
            continue
        logger.info("Processing chunk %d/%d for interactive questions (size: %d)", i+1, len(chunks), len(chunk))
        questions = get_interactive_questions_for_chunk(chunk, user_preferences, model=model)
        if questions:
            logger.info("Chunk %d produced %d interactive questions.", i+1, len(questions))
            all_questions.extend(questions)
        else:
            logger.warning("Chunk %d produced no interactive questions.", i+1)
            # flash(f"Warning: Could not generate questions for chunk {i+1}.") # Optional

    logger.info("Total interactive questions generated: %d", len(all_questions))
    if not all_questions:
        flash("No interactive questions could be generated from the provided transcript and settings.")
    return all_questions


# ----------------------------
# Embedded HTML Templates
# ----------------------------

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transcript to Anki Cards or Interactive Game</title>
  <style>
    /* Remove tap highlight on mobile */
    button, input[type="submit"], select, textarea { -webkit-tap-highlight-color: transparent; }
    /* Remove focus outline */
    button:focus, input:focus, select:focus, textarea:focus { outline: none; }
    body { background-color: #1E1E20; color: #D7DEE9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol"; line-height: 1.5; margin: 0; padding: 0; }
    .container { max-width: 900px; margin: 0 auto; padding: 20px; }
    h1 { text-align: center; color: #bb86fc; }
    p { text-align: center; }
    textarea, input[type="text"], select {
      width: 100%; /* Full width */
      box-sizing: border-box; /* Include padding and border in the element's total width and height */
      padding: 12px;
      font-size: 16px;
      margin-bottom: 15px;
      background-color: #2F2F31;
      color: #D7DEE9;
      border: 1px solid #444;
      border-radius: 5px;
      resize: vertical; /* Allow vertical resizing for textarea */
    }
    textarea { min-height: 150px; }
    input[type="submit"] {
      display: block; /* Make buttons block level */
      width: 100%; /* Full width */
      padding: 12px 20px;
      font-size: 16px;
      font-weight: bold;
      margin-top: 10px;
      background-color: #6200ee;
      color: #fff;
      border: none;
      border-radius: 5px;
      cursor: pointer;
      transition: background-color 0.3s;
    }
    input[type="submit"]:hover { background-color: #3700b3; }
    .flash { background-color: #cf6679; color: #121212; padding: 10px; margin-bottom: 15px; border-radius: 5px; text-align: center; }
    a { color: #03dac6; text-decoration: none; }
    a:hover { text-decoration: underline; }
    #advancedToggle {
      cursor: pointer;
      color: #bb86fc;
      font-weight: bold;
      margin-bottom: 10px;
      text-align: left;
      display: inline-block;
      padding: 5px 0;
    }
    #advancedOptions {
      margin: 0 0 20px 0; /* Adjust margin */
      text-align: left;
      background-color: #2a2a2e; /* Slightly different background */
      padding: 15px;
      border: 1px solid #444;
      border-radius: 5px;
    }
    #advancedOptions label { display: block; margin-bottom: 8px; font-weight: bold; }
    #advancedOptions input[type="text"], #advancedOptions select { margin-bottom: 12px; } /* Consistent spacing */

    .button-group {
      display: flex;
      flex-direction: column; /* Stack buttons vertically */
      gap: 10px; /* Space between buttons */
      align-items: center; /* Center buttons if container is wider */
    }
    .button-group input[type="submit"] {
        width: 100%; /* Ensure buttons take full width */
        max-width: 400px; /* Optional: constrain max width on wider screens */
    }
    /* Loading Overlay Styles */
    #loadingOverlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(18, 18, 18, 0.9); /* Slightly transparent background */
      display: none;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      z-index: 9999;
      backdrop-filter: blur(5px); /* Optional blur effect */
    }
     #loadingText { color: #D7DEE9; margin-top: 20px; font-size: 18px; }

    /* Responsive adjustments */
     @media (min-width: 600px) {
        .button-group {
            flex-direction: row; /* Side-by-side buttons on larger screens */
            justify-content: center; /* Center the row of buttons */
        }
        .button-group input[type="submit"] {
            width: auto; /* Allow buttons to size based on content */
            min-width: 180px; /* Ensure minimum clickable area */
        }
        #advancedOptions { max-width: 600px; margin-left: auto; margin-right: auto; }
     }
  </style>
  <!-- Include Lottie for the loading animation -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.12.2/lottie.min.js"></script> <!-- Updated Lottie version -->
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

          <label for="maxSize">Max Chunk Size (characters):</label>
          <input type="number" name="max_size" id="maxSize" value="8000" min="1000" max="16000" step="1000">
          <small style="color: #aaa; display: block; margin-top: -8px; margin-bottom: 12px;">Splits long transcripts for processing. Default: 8000.</small>

          <label for="preferences">Specific Instructions (Optional):</label>
          <input type="text" name="preferences" id="preferences" placeholder="e.g., Focus on definitions, ignore section X">
        </div>

        <label for="transcript" style="display: block; text-align: left; margin-bottom: 5px; font-weight: bold;">Transcript:</label>
        <textarea name="transcript" id="transcript" placeholder="Paste your full transcript here..." required></textarea>
        <br>

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

    let lottieInstance = null; // Store lottie instance

    document.getElementById("transcriptForm").addEventListener("submit", function(event) {
      event.preventDefault(); // Prevent default form submission

      // Basic validation
      const transcript = document.getElementById('transcript').value;
      if (!transcript || transcript.trim().length === 0) {
          alert('Please paste a transcript before submitting.');
          return;
      }


      // Show the loading overlay
      var overlay = document.getElementById("loadingOverlay");
      overlay.style.display = "flex"; // Show overlay

      // Load Lottie animation if not already loaded
      if (!lottieInstance) {
        lottieInstance = lottie.loadAnimation({
            container: document.getElementById('lottieContainer'),
            renderer: 'svg',
            loop: true,
            autoplay: true,
            // Using a different animation - replace with your preferred one if needed
            path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json'
            // Original path: 'https://lottie.host/embed/4500dbaf-9ac9-4b2b-b664-692cd9a3ccab/BGvTKQT8Tx.json'
        });
      } else {
          lottieInstance.play(); // Ensure it's playing if already loaded
      }


      var form = event.target;
      var formData = new FormData(form);

      // Determine which button was clicked and add its value to formData
      const clickedButton = event.submitter;
      if (clickedButton && clickedButton.name === "mode") {
          formData.set("mode", clickedButton.value);
           // Optionally change loading text based on mode
          document.getElementById('loadingText').textContent = `Generating ${clickedButton.value.includes('Anki') ? 'Anki Cards' : 'Game'}... Please wait.`;
      } else {
          // Default or fallback if submitter detection fails (shouldn't happen often)
          formData.set("mode", "Generate Anki Cards");
          document.getElementById('loadingText').textContent = 'Generating... Please wait.';
      }


      // Send the form data via fetch to the /generate endpoint
      fetch("/generate", {
        method: "POST",
        body: formData // FormData handles encoding
      })
      .then(response => {
          if (!response.ok) {
              // Try to get error message from response body if possible
              return response.text().then(text => {
                  throw new Error(text || `HTTP error! status: ${response.status}`);
              });
          }
          return response.text(); // Get HTML response as text
      })
      .then(html => {
        // Replace the current document content with the new HTML
        // This simulates a page navigation without a full reload history entry
        document.open();
        document.write(html);
        document.close();
        // No need to hide overlay here, the new page will load without it initially
      })
      .catch(error => {
        console.error("Form submission error:", error);
        // Display error to user in a more friendly way
        const flashContainer = document.querySelector('.flash') || document.createElement('div');
        if (!flashContainer.classList.contains('flash')) {
            flashContainer.className = 'flash'; // Add class if it's a new element
            // Insert it before the form or somewhere visible
            form.parentNode.insertBefore(flashContainer, form);
        }
        flashContainer.textContent = "An error occurred: " + error.message + ". Please check the logs or try again.";
        flashContainer.style.display = 'block'; // Ensure it's visible

        // Hide the loading overlay on error
        if (lottieInstance) lottieInstance.stop(); // Stop animation
        overlay.style.display = "none";
      });
    });
  </script>
</body>
</html>
"""

ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>Anki Cloze Review</title>
  <style>
    /* Base Styles */
    * { -webkit-tap-highlight-color: transparent; user-select: none; box-sizing: border-box; }
    html { touch-action: manipulation; height: 100%; }
    body { background-color: #1E1E20; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; margin: 0; padding: 0; display: flex; flex-direction: column; min-height: 100%; color: #D7DEE9; }
    button:focus, input:focus, textarea:focus { outline: none; }

    /* Layout Container */
    #reviewContainer { display: flex; flex-direction: column; align-items: center; padding: 15px; flex-grow: 1; justify-content: center; width: 100%; max-width: 800px; margin: 0 auto; }

    /* Progress Bar */
    #progress { width: 100%; text-align: center; color: #A6ABB9; margin-bottom: 15px; font-size: 14px; }

    /* Card Display */
    #kard { background-color: #2F2F31; border-radius: 8px; padding: 25px; width: 100%; min-height: 40vh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; word-wrap: break-word; margin-bottom: 20px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2); }
    .card { font-size: 22px; color: #D7DEE9; line-height: 1.6em; width: 100%; }
    .cloze { font-weight: bold; color: #03dac6 !important; cursor: pointer; border-bottom: 2px dotted #03dac6; padding-bottom: 1px; }
    .cloze-hint { font-size: 0.8em; color: #aaa; margin-left: 5px; font-style: italic; } /* Style for hint text */

    /* Editing Area */
    #editArea { width: 100%; height: 150px; font-size: 18px; padding: 10px; background-color: #252527; color: #D7DEE9; border: 1px solid #444; border-radius: 5px; line-height: 1.5; margin-top: 10px; }

    /* Action Buttons (Save/Discard after reveal) */
    #actionControls { display: none; /* Initially hidden */ justify-content: space-around; width: 100%; margin: 10px auto 0; gap: 10px; }
    .actionButton { padding: 12px 20px; font-size: 16px; font-weight: bold; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; transition: background-color 0.2s ease, transform 0.1s ease; }
    .actionButton:active { transform: scale(0.97); }
    .discard { background-color: #B00020; } /* Material Design Error Color */
    .discard:hover { background-color: #cf6679; }
    .save { background-color: #018786; } /* Material Design Secondary Color */
    .save:hover { background-color: #03dac6; }

    /* Bottom Control Bar */
    .bottom-controls { display: flex; flex-wrap: wrap; /* Allow wrapping on small screens */ justify-content: center; width: 100%; gap: 10px; margin-top: 15px; }
    .control-group { display: flex; flex: 1 1 auto; /* Allow flex grow/shrink */ justify-content: center; min-width: 150px; /* Minimum width before wrapping */ }
    .bottomButton { padding: 12px 20px; font-size: 16px; font-weight: 500; border: none; color: #fff; border-radius: 5px; cursor: pointer; background-color: #3700b3; flex: 1; /* Take available space in group */ margin: 0 5px; /* Spacing within group */ transition: background-color 0.2s ease, transform 0.1s ease; white-space: nowrap; /* Prevent text wrapping */ }
    .bottomButton:hover { background-color: #6200ee; }
    .bottomButton:active { transform: scale(0.97); }
    .bottomButton:disabled { background-color: #444; color: #888; cursor: not-allowed; }
    .edit { background-color: #FF8C00; } /* DarkOrange */
    .edit:hover { background-color: #FFA500; }
    .cart { background-color: #1E88E5; } /* Blue */
    .cart:hover { background-color: #42A5F5; }
    .tts-toggle { background-color: #FF6347; } /* Tomato (Default OFF) */
    .tts-toggle.on { background-color: #32CD32; } /* LimeGreen (ON) */
    .tts-toggle:hover { background-color: #FF7F50; } /* Coral hover for OFF */
    .tts-toggle.on:hover { background-color: #90EE90; } /* LightGreen hover for ON */


    /* Edit Mode Controls (Save/Cancel Edit) */
    #editControls { display: none; /* Initially hidden */ justify-content: space-around; width: 100%; margin: 10px auto 0; gap: 10px; }
    .editButton { padding: 12px 20px; font-size: 16px; font-weight: bold; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; transition: background-color 0.2s ease, transform 0.1s ease; }
    .editButton:active { transform: scale(0.97); }
    .cancelEdit { background-color: #6c757d; } /* Gray */
    .cancelEdit:hover { background-color: #5a6268; }
    .saveEdit { background-color: #28a745; } /* Green */
    .saveEdit:hover { background-color: #218838; }

    /* Saved Cards / Finished Screen */
    #savedCardsContainer { width: 100%; margin: 20px auto; color: #D7DEE9; display: none; /* Initially hidden */ flex-direction: column; align-items: center; }
    #savedCardsText { width: 100%; height: 200px; padding: 15px; font-size: 14px; background-color: #2F2F31; border: 1px solid #444; border-radius: 5px; resize: none; color: #D7DEE9; font-family: monospace; white-space: pre; }
    #finishedHeader { text-align: center; color: #bb86fc; margin-bottom: 15px; }
    .saved-cards-buttons { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-top: 15px; width: 100%; }
    #copyButton, #downloadButton, #returnButton { /* Use bottomButton styling */ padding: 12px 20px; font-size: 16px; font-weight: 500; border: none; color: #fff; border-radius: 5px; cursor: pointer; background-color: #03A9F4; /* Light Blue */ transition: background-color 0.2s ease, transform 0.1s ease; flex: 1 1 auto; min-width: 120px; }
    #copyButton:hover, #downloadButton:hover, #returnButton:hover { background-color: #29B6F6; }
    #copyButton:active, #downloadButton:active, #returnButton:active { transform: scale(0.97); }
    #copyButton { background-color: #4A90E2; } /* Distinct Copy Color */
    #returnButton { background-color: #757575; } /* Gray for return */


    /* Loading Overlay */
    #loadingOverlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(18, 18, 18, 0.9); display: flex; justify-content: center; align-items: center; z-index: 9999; backdrop-filter: blur(5px); }

    /* Responsive Adjustments */
    @media (max-width: 600px) {
        .card { font-size: 18px; }
        #kard { padding: 15px; min-height: 35vh;}
        .actionButton, .bottomButton, .editButton { font-size: 14px; padding: 10px 15px; }
        .bottom-controls { flex-direction: column; align-items: stretch; } /* Stack controls vertically */
        .control-group { width: 100%; justify-content: stretch; }
        .bottomButton { flex-grow: 1; width: 100%; margin: 0 0 10px 0; } /* Full width buttons */
        .bottom-controls .control-group:last-child .bottomButton { margin-bottom: 0; }
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
    <div class="bottom-controls">
       <div class="control-group" id="undoGroup">
          <button id="undoButton" class="bottomButton undo" onmousedown="event.preventDefault()" ontouchend="this.blur()">Previous Card</button>
       </div>
       <div class="control-group" id="editGroup">
          <button id="editButton" class="bottomButton edit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Edit Card</button>
       </div>
       <div class="control-group" id="cartGroup">
           <button id="cartButton" class="bottomButton cart" onmousedown="event.preventDefault()" ontouchend="this.blur()">Saved (<span id="savedCount">0</span>)</button>
       </div>
       <!-- TTS Toggle Button -->
       <div class="control-group" id="ttsGroup">
            <button id="ttsToggleButton" class="bottomButton tts-toggle" onmousedown="event.preventDefault()" ontouchend="this.blur()">TTS: OFF</button>
       </div>
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
    const rawCards = {{ cards_json|safe }};
    let interactiveCards = []; // Will hold objects { target: num|null, displayText: html, exportText: original }
    let currentIndex = 0;
    let savedCards = []; // Holds the exportText of saved cards
    let historyStack = []; // For undo functionality
    let inEditMode = false;
    let finished = false;
    let viewMode = 'card'; // 'card' or 'saved'
    let savedCardIndexBeforeCart = null; // Track index when opening saved view

    // TTS State
    let ttsEnabled = false;
    let currentUtterance = null; // To manage ongoing speech

    // DOM Elements
    const reviewContainer = document.getElementById('reviewContainer');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const currentEl = document.getElementById("current");
    const totalEl = document.getElementById("total");
    const cardContentEl = document.getElementById("cardContent");
    const kardEl = document.getElementById("kard"); // Reference to the card container
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
    const downloadButton = document.getElementById("downloadButton"); // Added download button reference
    const cartButton = document.getElementById("cartButton");
    const returnButton = document.getElementById("returnButton");
    const savedCountEl = document.getElementById("savedCount");
    const progressEl = document.getElementById("progress");
    const ttsToggleButton = document.getElementById("ttsToggleButton"); // TTS Button

    // Groups for hiding/showing sections
    const undoGroup = document.getElementById('undoGroup');
    const editGroup = document.getElementById('editGroup');
    const cartGroup = document.getElementById('cartGroup');
    const ttsGroup = document.getElementById('ttsGroup');


    // === Core Functions ===

    function generateInteractiveCards(cardText) {
      // Regex captures cloze number, answer, and optional hint
      const regex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g;
      const numbers = new Set();
      let m;
      // Find all unique cloze numbers (e.g., c1, c2) in the card
      // Use [...cardText.matchAll(regex)] for cleaner iteration if needed
      while ((m = regex.exec(cardText)) !== null) {
        // Avoid infinite loops with zero-width matches
        if (m.index === regex.lastIndex) {
            regex.lastIndex++;
        }
        numbers.add(m[1]);
      }

      if (numbers.size === 0) {
        // Card has no clozes, treat as a single card front/back
        return [{ target: null, displayText: cardText, exportText: cardText, hint: null }];
      }

      const cardsForNote = [];
      // Create a separate review card for each unique cloze number
      Array.from(numbers).sort((a, b) => a - b).forEach(num => {
        const { processedText, hint } = processCloze(cardText, num);
        cardsForNote.push({ target: num, displayText: processedText, exportText: cardText, hint: hint });
      });
      return cardsForNote;
    }

    function processCloze(text, targetClozeNumber) {
        let hintText = null;
        const processed = text.replace(/{{c(\d+)::(.*?)(?:::(.*?))?}}/g, (match, clozeNum, answer, hint) => {
            answer = answer.trim();
            hint = hint ? hint.trim() : null;

            if (clozeNum === targetClozeNumber) {
                // This is the cloze being tested ([...])
                hintText = hint; // Store the hint for this specific card
                let hintSpan = hint ? `<span class="cloze-hint">(${hint})</span>` : '';
                return `<span class="cloze" data-answer="${escapeHtml(answer)}">[...]${hintSpan}</span>`;
            } else {
                // Other clozes (not being tested) are shown directly
                return answer;
            }
        });
        return { processedText: processed, hint: hintText }; // Return processed text and the hint for the target
    }

    function escapeHtml(unsafe) {
        if (!unsafe) return "";
        return unsafe
             .replace(/&/g, "&")
             .replace(/</g, "<")
             .replace(/>/g, ">")
             .replace(/"/g, """)
             .replace(/'/g, "'");
    }

    function updateDisplay() {
      // Reset common elements
      kardEl.style.display = 'flex';
      cardContentEl.style.display = 'block'; // Ensure content div is visible
      actionControls.style.display = 'none';
      editControls.style.display = 'none';
      savedCardsContainer.style.display = 'none';
      if (document.getElementById('editArea')) { // Remove edit area if present
           document.getElementById('editArea').remove();
      }
      undoGroup.style.display = 'flex';
      editGroup.style.display = 'flex';
      cartGroup.style.display = 'flex';
      ttsGroup.style.display = 'flex'; // Show TTS button
      progressEl.style.visibility = 'visible';
      inEditMode = false; // Ensure edit mode is off

      if (viewMode === 'card' && !finished) {
        // --- Normal Card Review ---
        if (currentIndex >= interactiveCards.length) {
            // Should not happen if 'finished' is managed correctly, but as a fallback:
            console.warn("currentIndex out of bounds, showing finished screen.");
            showFinishedScreen();
            return;
        }

        progressEl.textContent = `Card ${currentIndex + 1} of ${interactiveCards.length}`;
        cardContentEl.innerHTML = interactiveCards[currentIndex].displayText;
        // Set button states
        updateUndoButtonState();
        editButton.disabled = false;
        cartButton.disabled = false;
        ttsToggleButton.disabled = false;

        // Speak the front of the card if TTS is on
        speakCurrentCardState();

      } else if (viewMode === 'saved' || finished) {
        // --- Saved Cards View or Finished ---
        kardEl.style.display = 'none';
        progressEl.style.visibility = 'hidden'; // Hide progress counter

        finishedHeader.textContent = finished ? "Review Complete!" : "Saved Cards";
        savedCardsText.value = savedCards.length > 0 ? savedCards.join("\n\n") : "No cards saved yet.";
        savedCardsContainer.style.display = 'flex'; // Use flex for centering

        // Configure buttons for this view
        copyButton.textContent = "Copy Text"; // Reset button text
        downloadButton.disabled = savedCards.length === 0;
        copyButton.disabled = savedCards.length === 0;
        returnButton.style.display = finished ? 'none' : 'block'; // Hide return if review is complete
        if (!finished && savedCardIndexBeforeCart !== null) {
            returnButton.textContent = `Return to Card ${savedCardIndexBeforeCart + 1}`;
        }

        // Hide card-specific controls
        undoGroup.style.display = 'none';
        editGroup.style.display = 'none';
        cartGroup.style.display = 'none';
        ttsGroup.style.display = 'none'; // Hide TTS button in saved view

        // Cancel any ongoing speech when entering this view
        window.speechSynthesis.cancel();

      } else if (viewMode === 'edit') {
        // --- Edit Mode ---
        inEditMode = true;
        progressEl.textContent = `Editing Card ${currentIndex + 1}`;
        // Keep kard visible, but replace content with textarea
        const currentCardData = interactiveCards[currentIndex];
        cardContentEl.style.display = 'none'; // Hide the normal display area
        // Create and insert textarea if it doesn't exist
        let editArea = document.getElementById('editArea');
        if (!editArea) {
            editArea = document.createElement('textarea');
            editArea.id = 'editArea';
            kardEl.appendChild(editArea); // Append inside the card container
        }
        editArea.value = currentCardData.exportText; // Use original text for editing
        editArea.style.display = 'block'; // Make sure it's visible

        editControls.style.display = 'flex'; // Show Save/Cancel Edit buttons
        // Hide other controls
        actionControls.style.display = 'none';
        undoGroup.style.display = 'none';
        editGroup.style.display = 'none';
        cartGroup.style.display = 'none';
        ttsGroup.style.display = 'none'; // Hide TTS button during edit

         // Cancel any ongoing speech when entering edit mode
        window.speechSynthesis.cancel();
      }

      updateSavedCount();
    }


    function revealAnswer() {
        if (inEditMode || actionControls.style.display === 'flex' || viewMode !== 'card') return; // Already revealed or not in card mode

        const clozes = cardContentEl.querySelectorAll(".cloze");
        if (clozes.length === 0) {
             // If no clozes, maybe it's a Q&A card - reveal is showing action buttons
             // Or it's just text - clicking reveals action buttons anyway
        } else {
            clozes.forEach(span => {
                const answer = span.getAttribute("data-answer");
                // Replace [...] and hint with the answer, keep the cloze class for styling
                span.innerHTML = escapeHtml(answer); // Display the answer
            });
        }

        actionControls.style.display = "flex"; // Show Save/Discard

        // Speak the back of the card if TTS is on
        speakCurrentCardState();
    }

    function handleCardAction(saveCard) {
      if (viewMode !== 'card') return;

      // Push current state BEFORE modifying it
      historyStack.push({
          index: currentIndex,
          saved: savedCards.slice(), // Copy saved cards array
          finishedState: finished,
          view: viewMode
      });
      updateUndoButtonState();

      if (saveCard) {
          // Avoid duplicates if saving the same card variation again after undo/redo
          const cardToSave = interactiveCards[currentIndex].exportText;
          if (!savedCards.includes(cardToSave)) {
             savedCards.push(cardToSave);
             updateSavedCount();
          }
      }

      // Move to next card or finish
      if (currentIndex < interactiveCards.length - 1) {
          currentIndex++;
          viewMode = 'card'; // Ensure we stay in card mode
          updateDisplay();
      } else {
          finished = true;
          viewMode = 'saved'; // Switch to saved view upon completion
          updateDisplay(); // Show the finished screen
      }
    }

    function handleUndo() {
      if (historyStack.length === 0) return;

      const snapshot = historyStack.pop();
      currentIndex = snapshot.index;
      savedCards = snapshot.saved;
      finished = snapshot.finishedState;
      viewMode = snapshot.view; // Restore the view mode (usually 'card')

      updateDisplay(); // This will handle showing the correct screen/card
      // No need to call speak here, updateDisplay calls it if needed.
    }

    function enterEditMode() {
      if (viewMode !== 'card' || finished) return;
      viewMode = 'edit';
      savedCardIndexBeforeCart = currentIndex; // Store index for potential cancel/return
      updateDisplay();
    }

    function cancelEdit() {
      viewMode = 'card'; // Go back to card view
      currentIndex = savedCardIndexBeforeCart; // Restore original index just in case
      updateDisplay();
    }

    function saveEdit() {
      const editedText = document.getElementById('editArea').value;
      if (!editedText || editedText.trim() === "") {
          alert("Card text cannot be empty.");
          return;
      }
      const originalCardData = interactiveCards[currentIndex];
      const fixedEditedText = fix_cloze_formatting(editedText); // Ensure format is good

      // We need to update potentially MULTIPLE interactive cards if the original text
      // generated more than one (e.g., c1, c2). Find all cards derived from the
      // original exportText and regenerate/update them.
      const originalExportText = originalCardData.exportText;
      const newInteractiveCardsForThisNote = generateInteractiveCards(fixedEditedText);

      // Replace the old cards with the new ones in the main array
      let firstIndex = -1;
      // Filter out the old cards, keeping track of where the first one was
      interactiveCards = interactiveCards.filter((card, index) => {
          if (card.exportText === originalExportText) {
              if (firstIndex === -1) firstIndex = index;
              return false; // Remove this card
          }
          return true; // Keep other cards
      });

      // Insert the newly generated cards at the position of the first removed card
      if (firstIndex !== -1) {
          interactiveCards.splice(firstIndex, 0, ...newInteractiveCardsForThisNote);
          // Adjust currentIndex to point to the first of the newly inserted/edited cards
          currentIndex = firstIndex;
      } else {
          // This case should theoretically not happen if we started editing an existing card
          // But as a fallback, just append and set index
          currentIndex = interactiveCards.length;
          interactiveCards.push(...newInteractiveCardsForThisNote);
      }


      // Update total count
      totalEl.textContent = interactiveCards.length;

      // Exit edit mode and show the (potentially new first variation of the) edited card
      viewMode = 'card';
      finished = false; // Editing might change the end condition
      updateDisplay();
    }


    function showSavedCards() {
        if (viewMode === 'saved') return; // Already viewing
        savedCardIndexBeforeCart = currentIndex; // Remember where we were
        viewMode = 'saved';
        updateDisplay();
    }

    function returnToCard() {
        if (viewMode !== 'saved') return;
        viewMode = 'card';
        // Restore index only if we have a valid index stored
        if (savedCardIndexBeforeCart !== null && savedCardIndexBeforeCart < interactiveCards.length) {
             currentIndex = savedCardIndexBeforeCart;
        } else {
            // Fallback: go to the current logical index or 0 if finished
            currentIndex = finished ? 0 : currentIndex;
        }
        finished = false; // No longer in finished state if returning to card
        updateDisplay();
    }


    function updateSavedCount() {
        savedCountEl.textContent = savedCards.length;
    }

    function updateUndoButtonState() {
        undoButton.disabled = historyStack.length === 0;
    }

    function showFinishedScreen() {
        finished = true;
        viewMode = 'saved'; // Use the saved view for the finished screen
        updateDisplay();
    }

     function copySavedCardsToClipboard() {
        savedCardsText.select();
        try {
            document.execCommand("copy");
            copyButton.textContent = "Copied!";
            setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
        } catch (err) {
            console.error('Failed to copy text: ', err);
            copyButton.textContent = "Copy Failed";
             setTimeout(() => { copyButton.textContent = "Copy Text"; }, 2000);
        }
        // Deselect text
        window.getSelection().removeAllRanges();
    }

    function downloadApkgFile() {
        if (savedCards.length === 0) {
            alert("No saved cards to download.");
            return;
        }
        // Add a visual loading indicator to the button?
        downloadButton.textContent = "Preparing...";
        downloadButton.disabled = true;

        fetch("/download_apkg", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ saved_cards: savedCards })
        })
        .then(response => {
            if (!response.ok) {
                 // Try to get error message from response body
                 return response.text().then(text => {
                     throw new Error(text || `Server error: ${response.status}`);
                 });
            }
            // Get filename from content-disposition header if available, otherwise use default
            const disposition = response.headers.get('content-disposition');
            let filename = "saved_cards.apkg";
            if (disposition && disposition.indexOf('attachment') !== -1) {
                const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
                const matches = filenameRegex.exec(disposition);
                if (matches != null && matches[1]) {
                  filename = matches[1].replace(/['"]/g, '');
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
            setTimeout(() => { downloadButton.textContent = "Download .apkg"; downloadButton.disabled = false; }, 2500);
        })
        .catch(error => {
            console.error("Download failed:", error);
            alert(`Download failed: ${error.message}`);
            downloadButton.textContent = "Download Failed";
            setTimeout(() => { downloadButton.textContent = "Download .apkg"; downloadButton.disabled = (savedCards.length === 0); }, 2500);
        });
    }


    // === TTS Functions ===
    function toggleTTS() {
        ttsEnabled = !ttsEnabled;
        if (ttsEnabled) {
            ttsToggleButton.textContent = "TTS: ON";
            ttsToggleButton.classList.add("on");
            // Ensure voices are loaded before speaking (can take a moment)
            loadVoicesAndSpeak();
        } else {
            ttsToggleButton.textContent = "TTS: OFF";
            ttsToggleButton.classList.remove("on");
            window.speechSynthesis.cancel(); // Stop any current speech
        }
    }

    function loadVoicesAndSpeak() {
        // Voices might load asynchronously. Speaking immediately might use default.
        // This tries to wait briefly or proceeds if voices already available.
        let voices = window.speechSynthesis.getVoices();
        if (voices.length) {
            console.debug("Voices already loaded.");
            speakCurrentCardState(); // Voices available, speak now
        } else {
            console.debug("Waiting for voices to load...");
            window.speechSynthesis.onvoiceschanged = () => {
                console.debug("Voices loaded.");
                // Check again if TTS is still enabled before speaking
                if(ttsEnabled) {
                   speakCurrentCardState();
                }
                // Important: Remove the listener after it fires once
                window.speechSynthesis.onvoiceschanged = null;
            };
            // As a fallback if onvoiceschanged doesn't fire reliably in some browsers
             setTimeout(() => {
                 if (ttsEnabled && !window.speechSynthesis.speaking && !window.speechSynthesis.pending) {
                     console.debug("Fallback voice check, speaking now.");
                     speakCurrentCardState();
                 }
             }, 500); // Wait half a second
        }
    }


    function speakText(text) {
        if (!text || text.trim() === "" || !('speechSynthesis' in window)) {
            if (!('speechSynthesis' in window)) console.warn("Speech Synthesis not supported.");
            return;
        }

        // Cancel previous utterance *before* creating a new one
        window.speechSynthesis.cancel();

        currentUtterance = new SpeechSynthesisUtterance(text);
        // --- Optional configuration ---
        // let voices = window.speechSynthesis.getVoices();
        // Find a preferred voice if needed, e.g., based on language or name
        // currentUtterance.voice = voices.find(v => v.lang.startsWith('en') && v.name.includes('Google'));
        // currentUtterance.lang = 'en-US'; // Set language
        currentUtterance.rate = 1.0; // Speed (0.1 to 10)
        currentUtterance.pitch = 1.0; // Pitch (0 to 2)
        currentUtterance.volume = 1.0; // Volume (0 to 1)

        currentUtterance.onend = () => {
            console.debug("Speech finished.");
            currentUtterance = null;
        };
        currentUtterance.onerror = (event) => {
            console.error("Speech Synthesis Error:", event.error);
            currentUtterance = null;
        };

        // Add slight delay before speaking, sometimes helps avoid issues
        setTimeout(() => {
             window.speechSynthesis.speak(currentUtterance);
             console.debug("Speaking:", text.substring(0, 50) + "...");
        }, 50); // 50ms delay
    }

    function getTextToSpeak() {
        if (viewMode !== 'card' || finished || currentIndex >= interactiveCards.length) {
             return null; // Don't speak if not on a card or finished
        }

        const cardData = interactiveCards[currentIndex];
        const originalText = cardData.exportText;
        const targetClozeNumber = cardData.target; // The cloze number being tested (e.g., '1', '2', or null)
        let textForSpeech = originalText;

        // Regex to find all clozes: {{c<number>::<answer>(::<hint>)?}}
        const clozeRegex = /{{c(\d+)::(.*?)(?:::(.*?))?}}/g;

        if (actionControls.style.display === "flex") {
            // --- Back Side Revealed ---
            // Replace ALL clozes with their answers
            textForSpeech = textForSpeech.replace(clozeRegex, (match, num, answer, hint) => {
                return answer.trim(); // Just the answer
            });
        } else {
            // --- Front Side (Cloze Hidden) ---
            textForSpeech = textForSpeech.replace(clozeRegex, (match, num, answer, hint) => {
                if (targetClozeNumber !== null && num === String(targetClozeNumber)) {
                    // This is the cloze currently hidden ([...])
                    // Speak the hint if available, otherwise "blank"
                    return hint ? hint.trim() : "blank";
                } else {
                    // Other clozes are revealed on the front side
                    return answer.trim();
                }
            });
        }

        // Clean up HTML tags (basic removal)
        textForSpeech = textForSpeech.replace(/<br\s*\/?>/gi, '. '); // Replace line breaks with periods for pauses
        textForSpeech = textForSpeech.replace(/<[^>]*>/g, ''); // Remove other tags
        // Normalize whitespace and clean up punctuation issues from replacements
        textForSpeech = textForSpeech.replace(/\s+/g, ' ').replace(/ \./g, '.').trim();

        return textForSpeech;
    }


    function speakCurrentCardState() {
        // Only speak if TTS is enabled and not currently speaking
        if (!ttsEnabled || window.speechSynthesis.speaking || window.speechSynthesis.pending) {
             if(window.speechSynthesis.speaking || window.speechSynthesis.pending) {
                 console.debug("Speech request ignored, already speaking or pending.");
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
    downloadButton.addEventListener("click", (e) => { e.stopPropagation(); downloadApkgFile(); }); // Added listener
    ttsToggleButton.addEventListener("click", (e) => { e.stopPropagation(); toggleTTS(); }); // Added listener


    // === Initialization ===
    function initialize() {
        // Generate interactive cards from raw data
        rawCards.forEach(cardText => {
            interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
        });

        if (interactiveCards.length === 0) {
            // Handle case where no processable cards were generated
            progressEl.textContent = "No cards generated";
            cardContentEl.innerHTML = "Could not generate reviewable cards from the input.";
            // Disable all buttons
            [undoButton, editButton, cartButton, ttsToggleButton].forEach(btn => btn.disabled = true);
            loadingOverlay.style.display = 'none'; // Hide loading overlay
            reviewContainer.style.display = 'flex'; // Show container
            return; // Stop initialization
        }


        totalEl.textContent = interactiveCards.length;
        updateSavedCount();
        updateUndoButtonState();
        updateDisplay(); // Show the first card

        // Hide loading overlay and show review container after setup
        loadingOverlay.style.transition = 'opacity 0.5s ease';
        loadingOverlay.style.opacity = '0';
        setTimeout(() => {
            loadingOverlay.style.display = 'none';
            reviewContainer.style.display = 'flex'; // Make sure it's flex
        }, 500);
    }

    // Initialize Lottie animation for loading
    var animation = lottie.loadAnimation({
      container: document.getElementById('lottieContainer'),
      renderer: 'svg',
      loop: true,
      autoplay: true,
      path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json' // Or your preferred animation
    });

    // Start the application once the window is loaded
    window.addEventListener('load', initialize);

  </script>
</body>
</html>
"""

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
    const questions = {{ questions_json|safe }};
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
        button.ontouchstart = (e) => { /* Allow touch interaction */ }; // Could add specific touch handling if needed
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
          // Maybe add a subtle shake effect for incorrect answers
           questionBox.animate([
                { transform: 'translateX(-5px)' },
                { transform: 'translateX(5px)' },
                { transform: 'translateX(-3px)' },
                { transform: 'translateX(3px)' },
                { transform: 'translateX(0)' }
            ], { duration: 300, easing: 'ease-in-out' });
      }

      updateHeader(); // Update score display

      // Wait a moment before showing the next question
      setTimeout(() => {
        currentQuestionIndex++;
        showQuestion();
      }, 1800); // Increased delay slightly
    }

    function endGame() {
      questionBox.textContent = "Quiz Complete!";
      optionsWrapper.style.display = 'none'; // Hide options area
      timerEl.style.display = 'none'; // Hide timer

      feedbackEl.classList.remove('hidden');
      feedbackEl.innerHTML = `
        <h2>Final Score: ${score} / ${totalQuestions} (${Math.round((score/totalQuestions)*100)}%)</h2>
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
               // Basic cloze format: Question<br><br>{{c1::Answer}}
               // Using index+1 as a unique identifier part if needed, but c1 is standard for single cloze per card.
               content += `${q.question}<br><br>{{c1::${q.correctAnswer}}}<hr>`; // Use <hr> for separation
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
         tempTextarea.value = container.innerHTML.replace(/<br\s*\/?>/gi, '\n').replace(/<hr>/gi, '\n\n');
         document.body.appendChild(tempTextarea);
         tempTextarea.select();
         try {
            document.execCommand('copy');
            this.textContent = "Copied!";
         } catch (err) {
             console.error('Copy failed', err);
             this.textContent = "Copy Failed";
         }
         document.body.removeChild(tempTextarea);
         setTimeout(() => { this.textContent = "Copy Anki Cards"; }, 2000);
      });
    }

    // Helper function for ripple effect
    function createRipple(event) {
        const button = event.currentTarget;
        const rect = button.getBoundingClientRect();
        const ripple = document.createElement('span');
        ripple.className = 'ripple';
        ripple.style.left = (event.clientX - rect.left) + 'px';
        ripple.style.top = (event.clientY - rect.top) + 'px';
        button.appendChild(ripple);
        setTimeout(() => { ripple.remove(); }, 600); // Match animation duration
    }

    // === Initialization ===
    function initializeQuiz() {
        if (!questions || questions.length === 0) {
             questionBox.textContent = "No questions loaded. Please try generating again.";
             // Disable timer/options if needed
             timerEl.style.display = 'none';
             optionsWrapper.style.display = 'none';
        } else {
            startGame();
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
      path: 'https://lottie.host/2f725a78-396a-4063-8b79-6da941a0e9a2/hUnrcyGMzF.json' // Or your preferred animation
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
        # Redirect back to index with the flash message
        # Using redirect helps clear the form and show the message cleanly
        return redirect(url_for('index'))

    user_preferences = request.form.get("preferences", "")
    model = request.form.get("model", "gpt-4o-mini")
    max_size_str = request.form.get("max_size", "8000") # Default increased

    try:
        max_size = int(max_size_str)
        if not (1000 <= max_size <= 16000): # Added reasonable limits
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
                # Flash message is likely set within get_all_interactive_questions
                # Redirect back to index if generation failed completely
                 flash("Failed to generate any interactive questions. Please check input or try again.")
                 return redirect(url_for('index'))
            # Escape JSON for safe embedding in HTML script tag
            questions_json = json.dumps(questions).replace('<', '\\u003c').replace('>', '\\u003e')
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
                # Flash message likely set within get_all_anki_cards
                flash("Failed to generate any Anki cards. Please check input or try again.")
                return redirect(url_for('index'))
            # Escape JSON for safe embedding
            cards_json = json.dumps(cards).replace('<', '\\u003c').replace('>', '\\u003e')
            return render_template_string(ANKI_HTML, cards_json=cards_json)
        except Exception as e:
            logger.exception("Error during Anki card generation: %s", e)
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
        # Create a unique deck ID based on current time (or use a fixed one)
        deck_id = genanki.guid_for(os.urandom(16)) # More robust ID generation
        deck_name = 'Generated Anki Deck'

        # Use the standard Anki Cloze model GUID
        # Found here: https://github.com/ankitects/anki/blob/main/proto/anki/models.proto#L9
        # Or generate a new stable one if needed: model_id = genanki.guid_for("My Cloze Model")
        model_id = 998877661 # Standard Anki Cloze model ID
        # Define the model structure expected by Anki's Cloze type
        cloze_model = genanki.Model(
            model_id,
            'Cloze (Generated)', # Model name
            fields=[
                {'name': 'Text'}, # Field containing the cloze text
                {'name': 'Back Extra'} # Optional extra field
            ],
            templates=[
                {
                    'name': 'Cloze',
                    'qfmt': '{{cloze:Text}}', # Question format
                    # Answer format includes the full text with cloze revealed, plus optional extra info
                    'afmt': '{{cloze:Text}}<hr id="answer">{{Back Extra}}',
                },
            ],
            model_type=genanki.Model.CLOZE # Specify model type as Cloze
        )

        # Create the deck
        deck = genanki.Deck(deck_id, deck_name)

        # Add notes to the deck
        for card_text in saved_cards:
             # Clean the card text one last time just in case
             cleaned_card_text = fix_cloze_formatting(card_text)
             note = genanki.Note(
                 model=cloze_model,
                 # Fields are ordered: 'Text', 'Back Extra'
                 fields=[cleaned_card_text, ''] # Add the card text to the 'Text' field, empty 'Back Extra'
             )
             deck.add_note(note)

        # Package the deck
        package = genanki.Package(deck)

        # Write to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".apkg") as temp_file:
            package.write_to_file(temp_file.name)
            temp_file_path = temp_file.name # Store path before closing

        logger.info("Successfully created .apkg file at %s", temp_file_path)

        @after_this_request
        def remove_file(response):
            try:
                os.remove(temp_file_path)
                logger.debug("Temporary file %s removed.", temp_file_path)
            except Exception as error:
                logger.error("Error removing temporary file %s: %s", temp_file_path, error)
            return response

        # Send the file
        return send_file(
            temp_file_path,
            mimetype='application/vnd.anki.apkg', # More specific mimetype if available
            as_attachment=True,
            download_name='generated_anki_deck.apkg' # Filename for the user
        )

    except Exception as e:
        logger.exception("Failed to generate or send .apkg file: %s", e)
        return f"Error creating Anki package: {e}", 500


if __name__ == "__main__":
    # Recommended: Use a production-ready WSGI server like Gunicorn or Waitress
    # For development:
    # Use host='0.0.0.0' to make it accessible on your network
    print("Starting Flask server on http://localhost:10000")
    app.run(debug=False, host='0.0.0.0', port=10000) # Turn debug off for deployment
