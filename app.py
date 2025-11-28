import os
import re
import json
import logging
import tempfile
import genanki
from flask import Flask, request, redirect, url_for, flash, render_template_string, send_file

#adding for new anki helper app
from flask import send_from_directory
#end


# Updated OpenAI API import and initialization.
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

#adding for anki helper app
@app.route("/reviewer")
def reviewer():
    # serves /static/cloze-reviewer.html
    return send_from_directory("static", "cloze-reviewer.html")
#end adding for anki helper app

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

SACLOZE_MODEL_ID_DEFAULT = 1607392319
SACLOZE_MODEL_NAME = "saCloze+"
SACLOZE_FIELDS = [
    {"name": "Text"},
    {"name": "Extra"},
]
SACLOZE_FRONT_TEMPLATE = """<div id=\"kard\">

<!-- Bar timer (Front) -->
<div class=\"tbar\" data-seconds=\"12\" role=\"timer\" aria-label=\"Front timer\">
  <div class=\"ttrack\"><div class=\"tfill\"></div></div>
  <span class=\"tleft\">00:12</span>
</div>
<!-- END Bar timer (Front) -->


<div class=\"tags\">{{Tags}}</div>
{{edit:cloze:Text}}
</div>



<!-- Tiny bar timer for Anki (desktop, AnkiMobile, AnkiDroid)-->

<script>
/* Re-entrant bar timer for Anki (desktop, AnkiMobile, AnkiDroid)
   - Attaches on every card load (no global guard)
   - Starts once per element via data flag
   - Works even when Anki swaps DOM without reloading the page
*/
(function(){
  function colorFor(p){
    var hStart = 170, hEnd = 0, h = hEnd + (hStart - hEnd) * p;
    return "hsl(" + h + ",95%,55%)";
  }
  function pad2(n){ return (n<10? "0":"") + n }

  function startTimerOn(el){
    if (!el || el.hasAttribute("data-bar-started")) return;
    el.setAttribute("data-bar-started","1");

    var secs = parseFloat(el.getAttribute("data-seconds")||"12");
    if (!(secs>0)) secs = 1;
    var fill  = el.querySelector(".tfill");
    var txt   = el.querySelector(".tleft");
    if (!fill || !txt) return;

    var durMs = secs*1000, start = performance.now();

    function tick(t){
      var left = Math.max(0, durMs - (t - start));
      var frac = left / durMs;
      fill.style.width = (frac*100).toFixed(3) + "%";
      fill.style.background = colorFor(frac);

      var s = Math.ceil(left/1000), mm = Math.floor(s/60), ss = s%60;
      txt.textContent = mm ? (mm + ":" + pad2(ss)) : (s + "s");

      if (left>0) requestAnimationFrame(tick);
      else el.classList.add("done");
    }
    requestAnimationFrame(tick);
  }

  function scanAndStart(){
    var bars = document.querySelectorAll(".tbar");
    for (var i=0;i<bars.length;i++) startTimerOn(bars[i]);
  }

  // Run now (script is after .tbar HTML)
  scanAndStart();

  // Also run whenever Anki swaps/rebuilds the DOM
  var mo = new MutationObserver(function(){ scanAndStart(); });
  if (document.body){
    mo.observe(document.body, { childList: true, subtree: true });
  } else {
    document.addEventListener("DOMContentLoaded", function(){
      mo.observe(document.body, { childList: true, subtree: true });
      scanAndStart();
    });
  }
})();
</script>

<!-- END Tiny bar timer for Anki (desktop, AnkiMobile, AnkiDroid)-->



<br>


{{edit:tts en_US voices=Apple_Evan_(Enhanced) speed=1.1:cloze:Text}}


<!--Take out cloze type brackets to stop tts, put them in to restart tts, two after first dashes, two before last dashes-->
"""
SACLOZE_BACK_TEMPLATE = """<div id=\"kard\">

<!-- Bar timer (Back) -->
<div class=\"tbar\" data-seconds=\"8\" role=\"timer\" aria-label=\"Back timer\">
  <div class=\"ttrack\"><div class=\"tfill\"></div></div>
  <span class=\"tleft\">00:08</span>
</div>
<!-- END Bar timer (Back) -->

<div class=\"tags\" id='tags'>{{Tags}}</div>
{{edit:cloze:Text}}
<div>&nbsp;</div>
<div id='extra'>{{edit:Extra}}</div>

</div>


<br>



{{edit:tts en_US voices=Apple_Evan_(Enhanced) speed=1.1:cloze-only:Text}}
"""
SACLOZE_CSS = """html { overflow: scroll; overflow-x: hidden; }

#kard {
padding: 0px 0px;
background-color:;
max-width: 700px;
margin: 0 auto;
word-wrap: break-word;
background-color: ;
}

.card {
font-family: helvetica;
font-size: 20px;
text-align: center;
color: #D7DEE9; /* FONT COLOR */
line-height: 1.6em;
background-color: #2F2F31; /* BACKGROUND COLOR -- "#333B45" is original */
}

.cloze, .cloze b, .cloze u, .cloze i { font-weight: bold; color: MediumSeaGreen !important;}



/* --- Bar timer --- */
.tbar{
  position: sticky; top: 0; z-index: 1;
  display: flex; align-items: center; gap: 10px;
  padding: 6px 8px; margin: 0 0 12px 0;
  background: rgba(16,24,40,.25); border: 1px solid rgba(34,50,77,.65);
  border-radius: 12px; backdrop-filter: blur(6px);
}
.ttrack{
  flex: 1 1 auto; height: 8px; border-radius: 999px;
  background: #102035; border: 1px solid #22324d; overflow: hidden;
}
.tfill{
  height: 100%; width: 100%; background: #27d3ff; /* JS updates color */
  transform-origin: left center;
}
.tleft{
  font-size: 12px; color: #A6ABB9; line-height: 1;
  white-space: nowrap; font-variant-numeric: tabular-nums;
  min-width: 52px; text-align: right; /* keeps it on one line */
}
.tbar.done .tleft{ color:#CC5B5B }

/* Optional: stop your fixed .tags from intercepting touches/scroll */
.tags{ pointer-events: none; }
/* --- END Bar timer --- */



#extra, #extra i { font-size: 15px; color:#D7DEE9; font-style: italic; }

#list { color: #A6ABB9; font-size: 10px; width: 100%; text-align: center; }

.tags { color: #A6ABB9; opacity: 0; font-size: 10px; background-color: ; width: 100%; height: ; text-align: center; text-transform: uppercase; position: fixed; padding: 0px; top:0;  right: 0;}
/* add what's within quotes if you want to see tags upon hovering: ".tags:hover { opacity: 1; position: fixed;}" */

img { display: block; max-width: 100%; max-height: none; margin-left: auto; margin: 10px auto 10px auto;}
img:active { width: 100%; }
tr {font-size: 12px; }

/* CHANGE COLOR ACCENTS HERE */
b { color: #C695C6 !important; }
u { text-decoration: none; color: #5EB3B3;}
i  { color: IndianRed; }
a { color: LightBlue !important; text-decoration: none; font-size: 14px; font-style: normal;  }

::-webkit-scrollbar {
    /*display: none;   remove scrollbar space */
    background: #fff;   /* optional: just make scrollbar invisible */
    width: 0px; }
::-webkit-scrollbar-thumb { background: #bbb; }


/* .mobile for all mobile devices */
.mobile .card { color: #D7DEE9; font-size: ; font-weight: ; background-color: #2F2F31; }
.iphone .card img {max-width: 100%; max-height: none;}
.mobile .card img:active { width: inherit; max-height: none;}
/* add what's within quotes if you want to see tags upon hovering: ".mobile .tags:hover { opacity: 1; position: relative;}" */
"""
SACLOZE_TEMPLATES = [
    {
        "name": "Card 1",
        "qfmt": SACLOZE_FRONT_TEMPLATE,
        "afmt": SACLOZE_BACK_TEMPLATE,
    }
]


def resolve_sacloze_model_id(payload):
    candidate = None
    if isinstance(payload, dict):
        candidate = payload.get("model_id")
    if not candidate:
        candidate = os.environ.get("SACLOZE_MODEL_ID")
    if not candidate:
        return SACLOZE_MODEL_ID_DEFAULT
    try:
        model_id = int(candidate)
        if model_id <= 0:
            raise ValueError
        return model_id
    except (TypeError, ValueError):
        logger.warning(
            "Invalid saCloze+ model_id %r; falling back to default %d",
            candidate,
            SACLOZE_MODEL_ID_DEFAULT,
        )
        return SACLOZE_MODEL_ID_DEFAULT

# ----------------------------
# Helper Functions
# ----------------------------

def preprocess_transcript(text):
    """
    Remove common timestamp patterns (e.g. "00:00:00.160" or "00:00:00,160")
    and normalize whitespace.
    """
    text_no_timestamps = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}', '', text)
    cleaned_text = re.sub(r'\s+', ' ', text_no_timestamps)
    return cleaned_text.strip()

def chunk_text(text, max_size, min_size=100):
    """
    Splits text into chunks of up to max_size characters.
    If a chunk is shorter than min_size and there is a previous chunk,
    it is merged with the previous chunk.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_size
        if end < len(text):
            last_space = text.rfind(" ", start, end)
            if last_space != -1:
                end = last_space
        chunk = text[start:end]
        if chunks and len(chunk) < min_size:
            chunks[-1] += chunk
        else:
            chunks.append(chunk)
        start = end
    logger.debug("Total number of chunks after splitting: %d", len(chunks))
    return chunks

def fix_cloze_formatting(card):
    """
    Ensures that cloze deletions in the card use exactly two curly braces on each side.
    If the API returns a card like "{c1::...}" then this function converts it to "{{c1::...}}".
    """
    if "{{" not in card:
        card = card.replace("{c", "{{c")
    card = re.sub(r'(?<!})}(?!})', '}}', card)
    return card

def get_anki_cards_for_chunk(transcript_chunk, user_preferences="", model="gpt-4o"):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    """
    user_instr = ""
    if user_preferences.strip():
        user_instr = f'\nUser Request: {user_preferences.strip()}\nIf no content relevant to the user request is found in this chunk, output a dummy card in the format: "User request not found in {{c1::this chunk}}."'
    
    prompt = f"""
You are an expert at creating study flashcards in Anki using cloze deletion.
Given the transcript below, generate a list of flashcards.
Each flashcard should be a complete, self-contained sentence (or sentence fragment) containing one or more cloze deletions.
Each cloze deletion must be formatted exactly as:
  {{c1::hidden text}}
Follow these formatting instructions exactly:
2. Formatting Cloze Deletions Properly
   • Cloze deletions should be written in the format:
     {{c1::hidden text}}
   • Example:
     Original sentence: "Canberra is the capital of Australia."
     Cloze version: "{{c1::Canberra}} is the capital of {{c2::Australia}}."
3. Using Multiple Cloze Deletions in One Card
   • If multiple deletions belong to the same testable concept, they should use the same number:
     Example: "The three branches of the U.S. government are {{c1::executive}}, {{c1::legislative}}, and {{c1::judicial}}."
   • If deletions belong to separate testable concepts, use different numbers:
     Example: "The heart has {{c1::four}} chambers and pumps blood through the {{c2::circulatory}} system."
4. Ensuring One Clear Answer
   • Avoid ambiguity—each blank should have only one reasonable answer.
   • Bad Example: "{{c1::He}} went to the store."
   • Good Example: "The mitochondria is known as the {{c1::powerhouse}} of the cell."
5. Choosing Between Fill-in-the-Blank vs. Q&A Style
   • Fill-in-the-blank format works well for quick fact recall:
         {{c1::Canberra}} is the capital of {{c2::Australia}}.
   • Q&A-style cloze deletions work better for some questions:
         What is the capital of Australia?<br><br>{{c1::Canberra}}
   • Use line breaks (<br><br>) so the answer appears on a separate line.
6. Avoiding Overly General or Basic Facts
   • Bad Example (too vague): "{{c1::A planet}} orbits a star."
   • Better Example: "{{c1::Jupiter}} is the largest planet in the solar system."
   • Focus on college-level or expert-level knowledge.
7. Using Cloze Deletion for Definitions
   • Definitions should follow the “is defined as” structure for clarity.
         Example: "A {{c1::pneumothorax}} is defined as {{c2::air in the pleural space}}."
8. Formatting Output in HTML for Readability
   • Use line breaks (<br><br>) to properly space question and answer.
         Example:
         What is the capital of Australia?<br><br>{{c1::Canberra}}
9.  If Anki cards are provided by the user in Cloze deletion format, go ahead and use them verbatim in the format given rather than making changes.
10. Summary of Key Rules
   • Keep answers concise (single words or short phrases).
   • Use different C-numbers for unrelated deletions.
   • Ensure only one correct answer per deletion.
   • Focus on college-level or expert-level knowledge.
   • Use HTML formatting for better display.
   • If Anki cards are provided by the user in Cloze deletion format, go ahead and use them verbatim in the format given rather than making changes.
In addition, you must make sure to follow the following instructions:
{user_instr}
Ensure you output ONLY a valid JSON array of strings, with no additional commentary or markdown.
    
Transcript:
\"\"\"{transcript_chunk}\"\"\" 
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000,
            timeout=60
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for chunk: %s", result_text)
        try:
            cards = json.loads(result_text)
            if isinstance(cards, list):
                cards = [fix_cloze_formatting(card) for card in cards]
                return cards
        except Exception as parse_err:
            logger.error("JSON parsing error for chunk: %s", parse_err)
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx != -1:
                json_str = result_text[start_idx:end_idx+1]
                try:
                    cards = json.loads(json_str)
                    if isinstance(cards, list):
                        cards = [fix_cloze_formatting(card) for card in cards]
                        return cards
                except Exception as e:
                    logger.error("Fallback JSON parsing failed for chunk: %s", e)
        flash("Failed to generate Anki cards for a chunk. API response: " + result_text)
        return []
    except Exception as e:
        logger.error("OpenAI API error for chunk: %s", e)
        flash("OpenAI API error for a chunk: " + str(e))
        return []

def get_all_anki_cards(transcript, user_preferences="", max_chunk_size=4000, model="gpt-4o"):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk.
    Returns a combined list of all flashcards.
    """
    cleaned_transcript = preprocess_transcript(transcript)
    logger.debug("Cleaned transcript (first 200 chars): %s", cleaned_transcript[:200])
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    all_cards = []
    for i, chunk in enumerate(chunks):
        logger.debug("Processing chunk %d/%d", i+1, len(chunks))
        cards = get_anki_cards_for_chunk(chunk, user_preferences, model=model)
        logger.debug("Chunk %d produced %d cards.", i+1, len(cards))
        all_cards.extend(cards)
    logger.debug("Total flashcards generated: %d", len(all_cards))
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
You are an expert at creating interactive multiple-choice questions for educational purposes.
Given the transcript below, generate a list of interactive multiple-choice questions.
Each question must be a JSON object with the following keys:
  "question": a string containing the question text.
  "options": an array of strings representing the possible answers.
  "correctAnswer": a string that is exactly one of the options, representing the correct answer.
Optionally, you may include an "explanation" key with a brief explanation.
Ensure that the output is ONLY a valid JSON array of such objects, with no additional commentary or markdown.
{user_instr}
Transcript:
\"\"\"{transcript_chunk}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000,
            timeout=60
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for interactive questions: %s", result_text)
        try:
            questions = json.loads(result_text)
            if isinstance(questions, list):
                return questions
        except Exception as parse_err:
            logger.error("JSON parsing error for interactive questions: %s", parse_err)
            start_idx = result_text.find('[')
            end_idx = result_text.rfind(']')
            if start_idx != -1 and end_idx != -1:
                json_str = result_text[start_idx:end_idx+1]
                try:
                    questions = json.loads(json_str)
                    if isinstance(questions, list):
                        return questions
                except Exception as e:
                    logger.error("Fallback JSON parsing failed for interactive questions: %s", e)
        flash("Failed to generate interactive questions for a chunk. API response: " + result_text)
        return []
    except Exception as e:
        logger.error("OpenAI API error for interactive questions: %s", e)
        flash("OpenAI API error for a chunk: " + str(e))
        return []

def get_all_interactive_questions(transcript, user_preferences="", max_chunk_size=4000, model="gpt-4o"):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk to generate interactive questions.
    Returns a combined list of all questions.
    """
    cleaned_transcript = preprocess_transcript(transcript)
    logger.debug("Cleaned transcript (first 200 chars): %s", cleaned_transcript[:200])
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    all_questions = []
    for i, chunk in enumerate(chunks):
        logger.debug("Processing chunk %d/%d for interactive questions", i+1, len(chunks))
        questions = get_interactive_questions_for_chunk(chunk, user_preferences, model=model)
        logger.debug("Chunk %d produced %d interactive questions.", i+1, len(questions))
        all_questions.extend(questions)
    logger.debug("Total interactive questions generated: %d", len(all_questions))
    return all_questions

# ----------------------------
# Embedded HTML Templates
# ----------------------------
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
  <title>Transcript to Anki Cards or Interactive Game</title>
  <style>
    /* Remove tap highlight on mobile */
    button { -webkit-tap-highlight-color: transparent; }
    /* Remove focus outline */
    button:focus, input:focus { outline: none; }
    body { background-color: #1E1E20; color: #D7DEE9; font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
    textarea, input[type="text"], select {
      width: 80%;
      padding: 10px;
      font-size: 16px;
      margin-bottom: 10px;
      background-color: #2F2F31;
      color: #D7DEE9;
      border: 1px solid #444;
      border-radius: 5px;
    }
    textarea { height: 200px; }
    input[type="submit"] { 
      padding: 10px 20px; 
      font-size: 16px; 
      margin-top: 10px; 
      background-color: #6200ee; 
      color: #fff; 
      border: none; 
      border-radius: 5px; 
      cursor: pointer; 
      transition: background-color 0.3s;
    }
    input[type="submit"]:hover { background-color: #3700b3; }
    .flash { color: red; }
    a { color: #6BB0F5; text-decoration: none; }
    a:hover { text-decoration: underline; }
    #advancedToggle {
      cursor: pointer;
      color: #bb86fc;
      font-weight: bold;
      margin-bottom: 10px;
    }
    #advancedOptions {
      width: 80%;
      margin: 0 auto 20px;
      text-align: left;
      background-color: #2F2F31;
      padding: 10px;
      border: 1px solid #444;
      border-radius: 5px;
    }
    #advancedOptions label { display: block; margin-bottom: 5px; }
    .button-group {
      display: flex;
      flex-direction: column;
      gap: 10px;
      align-items: center;
    }
    .button-group input[type="submit"] {
      width: 80%;
      max-width: 300px;
    }
    /* Loading Overlay Styles */
    #loadingOverlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: #121212;
      display: none;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      z-index: 9999;
    }
  </style>
  <!-- Include Lottie for the loading animation -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.7.6/lottie.min.js"></script>
</head>
<body>
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 300px; height: 300px;"></div>
    <div id="loadingText" style="color: #D7DEE9; margin-top: 20px; font-size: 18px;">Generating. Please wait...</div>
  </div>
  <h1>Transcript to Anki Cards or Interactive Game</h1>
    <p>
      Don't have a transcript? Use the
      <a href="https://tactiq.io/tools/youtube-transcript" target="_blank">
        Tactiq.io transcript tool
      </a>
      or
      <a href="https://www.youtube-transcript.io/" target="_blank">
        Youtube Transcript Generator
      </a>
      to generate one.
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
      <label for="modelSelect">Model:</label>
      <select name="model" id="modelSelect">
        <option value="gpt-4.1-nano">gpt-4.1-nano</option>
        <option value="gpt-4.1-mini">gpt-4.1-mini</option>
        <option value="gpt-4.1" selected>gpt-4.1</option>
        <option value="gpt-4o-mini">gpt-4o-mini</option>
        <option value="gpt-4o">gpt-4o</option>
      </select>
      <br>
      <label for="maxSize">Max Chunk Size (characters):</label>
      <input type="text" name="max_size" id="maxSize" value="10000">
    </div>
    <textarea name="transcript" placeholder="Paste your transcript here" required></textarea>
    <br>
    <input type="text" name="preferences" placeholder="Enter your card preferences (optional)">
    <br>
    <div class="button-group">
      <input type="submit" name="mode" value="Generate Anki Cards">
      <input type="submit" name="mode" value="Generate Game">
    </div>
  </form>
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
    document.getElementById("transcriptForm").addEventListener("submit", function(event) {
      event.preventDefault();
      // Show the loading overlay immediately
      var overlay = document.getElementById("loadingOverlay");
      overlay.style.display = "flex";
      // Delay Lottie initialization slightly so the container is rendered
      setTimeout(function() {
        lottie.loadAnimation({
          container: document.getElementById('lottieContainer'),
          renderer: 'svg',
          loop: true,
          autoplay: true,
          path: 'https://lottie.host/embed/4500dbaf-9ac9-4b2b-b664-692cd9a3ccab/BGvTKQT8Tx.json'
        });
      }, 50);
      var form = event.target;
      var formData = new FormData(form);
      // Use the clicked submit button’s value (if available) to set the mode
      if(event.submitter && event.submitter.value) {
          formData.set("mode", event.submitter.value);
      }
      // Send the form data via fetch to the new /generate endpoint
      fetch("/generate", {
        method: "POST",
        body: formData
      })
      .then(response => response.text())
      .then(html => {
        // Replace the current document with the returned HTML
        document.open();
        document.write(html);
        document.close();
      })
      .catch(error => {
        console.error("Error:", error);
        alert("An error occurred. Please try again.");
        overlay.style.display = "none";
      });
    });
  </script>
  <!-- Keep-alive ping -->
  <script>
    setInterval(() => fetch("/ping").catch(()=>{}), 2 * 60 * 1000);
  </script>
</body>
</html>
"""
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️ INDEX_HTML TEMPLATE ✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️✏️
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>Anki Cloze Review</title>
  <style>
    /* Remove tap highlight on mobile */
    button { -webkit-tap-highlight-color: transparent; }
    /* Remove focus outline */
    button:focus { outline: none; }
    html, body { height: 100%; margin: 0; padding: 0; }
    html { touch-action: manipulation; } /* Add this line */
    body { background-color: #1E1E20; font-family: helvetica, Arial, sans-serif; }
    #reviewContainer {
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 10px;
      min-height: 100vh;
      justify-content: center;
    }
    #progress { width: 100%; max-width: 700px; text-align: center; color: #A6ABB9; margin-bottom: 10px; }
    #kard {
      background-color: #2F2F31;
      border-radius: 5px;
      padding: 20px;
      max-width: 700px;
      width: 100%;
      min-height: 50vh;
      display: flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      word-wrap: break-word;
      margin-bottom: 20px;
    }
    .card { font-size: 20px; color: #D7DEE9; line-height: 1.6em; }
    #editArea { width: 100%; height: 150px; font-size: 20px; padding: 10px; }
    .cloze, .cloze b, .cloze u, .cloze i { font-weight: bold; color: MediumSeaGreen !important; cursor: pointer; }
    #actionControls { display: none; justify-content: space-between; width: 100%; max-width: 700px; margin: 10px auto; }
    .actionButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; }
    .discard { background-color: red; }
    .save { background-color: green; }
    #bottomUndo, #bottomEdit {
      display: flex;
      justify-content: center;
      width: 100%;
      max-width: 700px;
      margin: 5px auto;
      padding: 0 10px;
    }
    .bottomButton {
      padding: 10px 20px;
      font-size: 16px;
      border: none;
      color: #fff;
      border-radius: 5px;
      cursor: pointer;
      flex: 1;
      margin: 0 5px;
      background-color: #6200ee;
      transition: background-color 0.3s;
    }
    .bottomButton:hover { background-color: #3700b3; }
    .undo { }
    .edit { background-color: #FFA500; }
    #editControls { display: none; justify-content: space-between; width: 100%; max-width: 700px; margin: 10px auto; }
    .editButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; }
    .cancelEdit { background-color: gray; }
    .saveEdit { background-color: green; }
    #savedCardsContainer { 
      width: 100%; max-width: 700px; margin: 20px auto; color: #D7DEE9; 
      display: none; 
      /* Ensure saved cards screen is also centered */
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    #savedCardsText {
      width: 100%;
      height: 200px;
      padding: 10px;
      font-size: 16px;
      background-color: #2F2F31;
      border: none;
      border-radius: 5px;
      resize: none;
      color: #D7DEE9;
    }
    #copyButton, #returnButton {
      margin-top: 10px;
      padding: 10px 20px;
      font-size: 16px;
      background-color: #4A90E2;
      color: #fff;
      border: none;
      border-radius: 5px;
      cursor: pointer;
    }
    /* New Download Button Styling */
    #downloadButton {
      margin-top: 10px;
      padding: 10px 20px;
      font-size: 16px;
      background-color: #03A9F4;
      color: #fff;
      border: none;
      border-radius: 5px;
      cursor: pointer;
    }
    #cartContainer {
      display: flex;
      justify-content: center;
      margin: 5px auto;
      width: 100%;
      max-width: 700px;
      padding: 0 10px;
    }
    .cart.bottomButton {
      background-color: #03A9F4;
    }
    .cart.bottomButton:hover {
      background-color: #0288D1;
    }
    /* Loading Overlay Styles */
    #loadingOverlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: #121212;
      display: flex;
      justify-content: center;
      align-items: center;
      z-index: 9999;
    }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.7.6/lottie.min.js"></script>
</head>
<body>
  <!-- Loading Overlay -->
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 300px; height: 300px;"></div>
  </div>
  <div id="reviewContainer" style="display: none;">
    <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
    <div id="kard">
      <div class="card" id="cardContent"></div>
    </div>
    <div id="actionControls">
      <button id="discardButton" class="actionButton discard" onmousedown="event.preventDefault()" ontouchend="this.blur()">Discard</button>
      <button id="saveButton" class="actionButton save" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save</button>
    </div>
    <div id="editControls">
      <button id="cancelEditButton" class="editButton cancelEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Cancel Edit</button>
      <button id="saveEditButton" class="editButton saveEdit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Save Edit</button>
    </div>
    <!-- START: Add this new div and its buttons right AFTER the closing </div id="editControls"> -->
    <div id="clozeEditControls" style="display: none; justify-content: space-around; width: 100%; max-width: 700px; margin: 10px auto;">
      <button id="removeAllClozeButton" class="editButton" style="background-color: #dc3545;" onmousedown="event.preventDefault()" ontouchend="this.blur()">Remove All Cloze</button>
      <button id="addClozeButton" class="editButton" style="background-color: #007bff;" onmousedown="event.preventDefault()" ontouchend="this.blur()">Add Cloze</button>
    </div>
    <!-- END: Add this new div and its buttons -->
    <div id="bottomUndo">
      <button id="undoButton" class="bottomButton undo" onmousedown="event.preventDefault()" ontouchend="this.blur()">Previous Card</button>
    </div>
    <div id="bottomEdit">
      <button id="editButton" class="bottomButton edit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Edit</button>
    </div>
    <div id="cartContainer">
      <button id="cartButton" class="bottomButton cart" onmousedown="event.preventDefault()" ontouchend="this.blur()">Saved Cards</button>
    </div> <!-- This closes cartContainer -->
    
    <!-- START: Add this new div and button for TTS -->
    <div id="ttsContainer" style="display: flex; justify-content: center; margin: 5px auto; width: 100%; max-width: 700px; padding: 0 10px;">
        <button id="ttsToggleButton" class="bottomButton" style="background-color: grey;" onmousedown="event.preventDefault()" ontouchend="this.blur()">TTS: Off</button>
    </div>
    <!-- END: Add this new div and button for TTS -->

    <div id="savedCardsContainer">
        <!-- Rest of the savedCardsContainer content... -->
    <div id="savedCardsContainer">
      <h3 id="finishedHeader" style="text-align:center;">Saved Cards</h3>
      <textarea id="savedCardsText" readonly></textarea>
      <div style="text-align:center;">
        <button id="copyButton" onmousedown="event.preventDefault()" ontouchend="this.blur()">Copy Saved Cards</button>
      </div>
      <div style="text-align:center; margin-top:10px;">
        <button id="returnButton" class="bottomButton return" onmousedown="event.preventDefault()" ontouchend="this.blur()">Return to Card</button>
      </div>
      <div style="text-align:center; margin-top:10px;">
        <button id="downloadButton" class="bottomButton" onmousedown="event.preventDefault()" ontouchend="this.blur()">Download APKG</button>
      </div>
    </div>
  </div>
  <script>
    // Initialize Lottie animation
    var animation = lottie.loadAnimation({
      container: document.getElementById('lottieContainer'),
      renderer: 'svg',
      loop: true,
      autoplay: true,
      path: 'https://lottie.host/embed/4500dbaf-9ac9-4b2b-b664-692cd9a3ccab/BGvTKQT8Tx.json'
    });
    // Once the page has fully loaded, hide the loading overlay and show the review container.
    window.addEventListener('load', function() {
      var overlay = document.getElementById('loadingOverlay');
      var reviewContainer = document.getElementById('reviewContainer');
      overlay.style.transition = 'opacity 0.5s ease';
      overlay.style.opacity = '0';
      setTimeout(function() {
        overlay.style.display = 'none';
        reviewContainer.style.display = 'flex';
      }, 500);
    });
  </script>
  <script>
    const cards = {{ cards_json|safe }};
{% raw %}
    let interactiveCards = [];
    function generateInteractiveCards(cardText) {
      const regex = /{{c(\d+)::(.*?)}}/g;
      const numbers = new Set();
      let m;
      while ((m = regex.exec(cardText)) !== null) {
        numbers.add(m[1]);
      }
      if (numbers.size === 0) {
        return [{ target: null, displayText: cardText, exportText: cardText }];
      }
      const cardsForNote = [];
      Array.from(numbers).sort().forEach(num => {
        const display = processCloze(cardText, num);
        cardsForNote.push({ target: num, displayText: display, exportText: cardText });
      });
      return cardsForNote;
    }
    function processCloze(text, target) {
      // Regex to capture cloze number, answer text, and optional hint
      const regex = /{{c(\d+)::(.*?)(?:::([^}]+))?}}/g; 
      return text.replace(regex, function(match, clozeNum, answer, hint) {
        const hintText = hint ? hint.trim() : ''; // Get hint or empty string
        // Display the hint inside the brackets if it exists, otherwise [...]
        const displayContent = hintText ? `[${hintText}]` : '[...]'; 
        if (clozeNum === target) {
          // Store both answer and hint (even if empty) in data attributes
          return `<span class="cloze" data-answer="${answer.replace(/"/g, '"')}" data-hint="${hintText.replace(/"/g, '"')}">${displayContent}</span>`;
        } else {
          // For non-target clozes, just show the answer text directly
         return answer; 
        }
      });
    }
// END of replacement for processCloze
    cards.forEach(cardText => {
      interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
    });
    // START: Add these new TTS variables and functions
let isTtsEnabled = false; // TTS is off by default
const synth = window.speechSynthesis; // Get the speech synthesis interface

function speakText(text) {
    if (!isTtsEnabled || !text || !text.trim()) return; // Only speak if enabled and text exists
    synth.cancel(); // Stop any previous speech
    const utterance = new SpeechSynthesisUtterance(text);
    // Optional: You could add configurations like language, rate, pitch here
    // utterance.lang = 'en-US';
    // utterance.rate = 1;
    // utterance.pitch = 1;
    synth.speak(utterance);
}

function getFrontTextToSpeak(cardElement) {
    // Clone the card content to avoid modifying the displayed card directly
    const tempDiv = cardElement.cloneNode(true);
    
    // Find all cloze spans within the cloned content
    const clozeSpans = tempDiv.querySelectorAll('.cloze');

    clozeSpans.forEach(span => {
        const hint = span.dataset.hint;
        // Replace the span node with a text node containing the hint or "blank"
        const replacementText = document.createTextNode(" " + (hint ? hint : "blank") + " ");
        span.parentNode.replaceChild(replacementText, span);
    });

    // Get the text content after replacements, clean up whitespace
    let textToSpeak = (tempDiv.textContent || tempDiv.innerText || "").replace(/\s+/g, ' ').trim();
    return textToSpeak;
}

function stopSpeech() {
    if (synth.speaking) {
        synth.cancel();
    }
}
// END: Add these new TTS variables and functions
    let currentIndex = 0;
    let savedCards = [];
    let historyStack = [];
    let inEditMode = false;
    let finished = false;
    let savedCardIndex = null; // For cart functionality

    const currentEl = document.getElementById("current");
    const totalEl = document.getElementById("total");
    const cardContentEl = document.getElementById("cardContent");
    const actionControls = document.getElementById("actionControls");
    const bottomUndo = document.getElementById("bottomUndo");
    const bottomEdit = document.getElementById("bottomEdit");
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
    const cartButton = document.getElementById("cartButton");
    const returnButton = document.getElementById("returnButton");
    const cartContainer = document.getElementById("cartContainer");
    const ttsToggleButton = document.getElementById("ttsToggleButton"); 
    const clozeEditControls = document.getElementById("clozeEditControls");
    const removeAllClozeButton = document.getElementById("removeAllClozeButton");
    const addClozeButton = document.getElementById("addClozeButton");

    totalEl.textContent = interactiveCards.length;

    function updateUndoButtonState() {
      undoButton.disabled = historyStack.length === 0;
    }
    updateUndoButtonState();
    
    document.getElementById("kard").addEventListener("click", function(e) {
      if (inEditMode) return;
      // Only proceed if the answer hasn't been shown yet
      if (actionControls.style.display === "none" || actionControls.style.display === "") { 
        stopSpeech(); // Stop front-side speech if it's still going
        const clozes = document.querySelectorAll("#cardContent .cloze");
        let answersToSpeak = []; // Collect answers to speak
    
        clozes.forEach(span => {
          const answer = span.getAttribute("data-answer");
          answersToSpeak.push(answer); // Add answer for speaking
          span.innerHTML = answer;     // Visually reveal the answer
          // Optional: remove the cloze class to prevent re-clicking/styling issues
          // span.classList.remove('cloze'); 
        });

        actionControls.style.display = "flex"; // Show Save/Discard buttons
    
        // Speak the collected answers (joined by comma-space)
        speakText(answersToSpeak.join(", ")); 
      }
    });
    // START: Add this block to initialize TTS button
    ttsToggleButton.textContent = `TTS: ${isTtsEnabled ? 'On' : 'Off'}`;
    ttsToggleButton.style.backgroundColor = isTtsEnabled ? '#03A9F4' : 'grey';
    // END: Add this block
    function showCard() {
      stopSpeech(); // Stop any speech from previous card/action
      finished = false;
      document.getElementById("progress").textContent = "Card " + (currentIndex+1) + " of " + interactiveCards.length;
      if (!inEditMode) {
        actionControls.style.display = "none";
      }
      // MAKE SURE this line comes BEFORE getFrontTextToSpeak
      cardContentEl.innerHTML = interactiveCards[currentIndex].displayText; 

      // Ensure the card content remains vertically centered.
      document.getElementById("kard").style.display = "flex";
      savedCardsContainer.style.display = "none";
      // Restore buttons if coming back from finished state.
      document.getElementById("bottomEdit").style.display = "flex";
      document.getElementById("cartContainer").style.display = "flex";
      document.getElementById("returnButton").style.display = "none";
      
      // START: Add TTS call for front side
      const frontText = getFrontTextToSpeak(cardContentEl);
      speakText(frontText);
      // END: Add TTS call
    }
    function nextCard() {
      if (currentIndex < interactiveCards.length - 1) {
          currentIndex++;
          showCard();
      } else {
          finished = true;
      }
    }
    // Modify the save and discard button event handlers:
    discardButton.addEventListener("click", function(e) {
      e.stopPropagation();
      stopSpeech(); // ADD THIS LINE
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice(), finished: finished });
      updateUndoButtonState();
      if (currentIndex === interactiveCards.length - 1) {
          finished = true;
          showFinished();
      } else {
          nextCard();
      }
    });
    saveButton.addEventListener("click", function(e) {
      e.stopPropagation();
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice(), finished: finished });
      updateUndoButtonState();
      savedCards.push(interactiveCards[currentIndex].exportText);
      if (currentIndex === interactiveCards.length - 1) {
          finished = true;
          showFinished();
      } else {
          nextCard();
      }
    });

    function showFinished() {
      // Hide card display and action controls, update header and show finish screen.
      document.getElementById("kard").style.display = "none";
      actionControls.style.display = "none";
      finishedHeader.textContent = "Review complete!";
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "flex";
      // Update progress to show "Review Complete"
      document.getElementById("progress").textContent = "Review Complete";
      // Hide buttons that should not appear on the finish screen.
      document.getElementById("bottomEdit").style.display = "none";
      document.getElementById("cartContainer").style.display = "none";
      document.getElementById("returnButton").style.display = "none";
    }

    editButton.addEventListener("click", function(e) {
      e.stopPropagation();
      if (!inEditMode) enterEditMode();
    });
    function enterEditMode() {
      stopSpeech(); // ADD THIS LINE
      inEditMode = true;
      originalCardText = interactiveCards[currentIndex].exportText;
      cardContentEl.innerHTML = '<textarea id="editArea">' + interactiveCards[currentIndex].exportText + '</textarea>';
      actionControls.style.display = "none";
      bottomUndo.style.display = "none";
      bottomEdit.style.display = "none";
      editControls.style.display = "flex";
      clozeEditControls.style.display = "flex"; // Add this line
    }
    saveEditButton.addEventListener("click", function(e) {
      e.stopPropagation();
      const editedText = document.getElementById("editArea").value;
      interactiveCards[currentIndex].exportText = editedText;
      let target = interactiveCards[currentIndex].target;
      if (target) {
        interactiveCards[currentIndex].displayText = processCloze(editedText, target);
      } else {
        interactiveCards[currentIndex].displayText = editedText;
      }
      inEditMode = false;
      editControls.style.display = "none";
      clozeEditControls.style.display = "none"; // Add this line
      bottomUndo.style.display = "flex";
      bottomEdit.style.display = "flex";
      showCard();
    });
    cancelEditButton.addEventListener("click", function(e) {
      e.stopPropagation();
      inEditMode = false;
      editControls.style.display = "none";
      clozeEditControls.style.display = "none"; // Add this line
      bottomUndo.style.display = "flex";
      bottomEdit.style.display = "flex";
      showCard();
    });

// START: Add Cloze Editing Logic

// Function to remove all cloze deletions from the editor
removeAllClozeButton.addEventListener("click", function(e) {
    e.stopPropagation();
    const editArea = document.getElementById("editArea");
    if (!editArea) return; // Should not happen in edit mode

    const currentText = editArea.value;
    // Regex to find {{c<number>::<content>}}
    const clozeRegex = /{{c\d+::(.*?)}}/g;
    // Replace the whole cloze tag with just the content inside
    const cleanedText = currentText.replace(clozeRegex, '$1');

    editArea.value = cleanedText;
});

// Function to add a new cloze deletion around selected text
addClozeButton.addEventListener("click", function(e) {
    e.stopPropagation();
    const editArea = document.getElementById("editArea");
    if (!editArea) return; // Should not happen

    const start = editArea.selectionStart;
    const end = editArea.selectionEnd;
    const selectedText = editArea.value.substring(start, end);

    if (!selectedText) {
        alert("Please select the text you want to hide.");
        return;
    }

    const currentFullText = editArea.value;

    // Find the highest existing cloze number
    const clozeRegex = /{{c(\d+)::.*?}}/g;
    let match;
    let maxClozeNum = 0;
    while ((match = clozeRegex.exec(currentFullText)) !== null) {
        const num = parseInt(match[1], 10);
        if (num > maxClozeNum) {
            maxClozeNum = num;
        }
    }

    const nextClozeNum = maxClozeNum + 1;
    const newClozeText = `{{c${nextClozeNum}::${selectedText}}}`;

    // Reconstruct the text in the textarea
    const textBefore = currentFullText.substring(0, start);
    const textAfter = currentFullText.substring(end);
    editArea.value = textBefore + newClozeText + textAfter;

    // Optional: Keep the newly added cloze selected (or place cursor after it)
    editArea.focus();
    editArea.selectionStart = start + newClozeText.length;
    editArea.selectionEnd = start + newClozeText.length;
});

// END: Add Cloze Editing Logic



    // FIX: Always call showCard() when undoing so that the progress text is updated.
    undoButton.addEventListener("click", function(e) {
      e.stopPropagation();
      if (historyStack.length === 0) {
        alert("No actions to undo.");
        return;
      }
      let snapshot = historyStack.pop();
      currentIndex = snapshot.currentIndex;
      savedCards = snapshot.savedCards.slice();
      finished = snapshot.finished;
      finished = false; // reset finished state
      showCard();  // update entire display including progress
      updateUndoButtonState();
    });

    copyButton.addEventListener("click", function() {
      savedCardsText.select();
      document.execCommand("copy");
      copyButton.textContent = "Copied!";
      setTimeout(function() {
        copyButton.textContent = "Copy Saved Cards";
      }, 2000);
    });

    // START: Add TTS Toggle Button Listener
    ttsToggleButton.addEventListener("click", function(e) {
        e.stopPropagation();
        isTtsEnabled = !isTtsEnabled; // Toggle the state
    
        // Update button appearance
        this.textContent = `TTS: ${isTtsEnabled ? 'On' : 'Off'}`;
        this.style.backgroundColor = isTtsEnabled ? '#03A9F4' : 'grey'; 
    
        if (isTtsEnabled) {
            // If TTS was just turned on, try to speak the current card's front side
            // Check if we are viewing the front of a card (answer not revealed)
            if (!inEditMode && (actionControls.style.display === "none" || actionControls.style.display === "") && !finished) {
                 const frontText = getFrontTextToSpeak(cardContentEl);
                 speakText(frontText);
            }
        } else {
            stopSpeech(); // If turning TTS off, stop any current speech
        }
    });
    // END: Add TTS Toggle Button Listener


    cartButton.addEventListener("click", function(e) {
      e.stopPropagation();
      savedCardIndex = currentIndex;
      document.getElementById("kard").style.display = "none";
      actionControls.style.display = "none";
      bottomUndo.style.display = "none";
      bottomEdit.style.display = "none";
      cartContainer.style.display = "none";
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "flex";
      // Show and update the Return to Card button for non-finished saved cards view.
      document.getElementById("returnButton").style.display = "block";
      document.getElementById("returnButton").textContent = "Return to Card " + (savedCardIndex+1);
    });
    returnButton.addEventListener("click", function(e) {
      e.stopPropagation();
      if (savedCardIndex !== null) {
        currentIndex = savedCardIndex;
      }
      savedCardsContainer.style.display = "none";
      document.getElementById("kard").style.display = "flex";
      actionControls.style.display = "none";
      bottomUndo.style.display = "flex";
      bottomEdit.style.display = "flex";
      cartContainer.style.display = "flex";
      showCard();
    });

    showCard();
// START: Add Keyboard Shortcut Listener
    document.addEventListener('keydown', function(event) {
        // Ignore shortcuts if in edit mode, finished screen, or cart view is active
        if (inEditMode || finished || savedCardsContainer.style.display === 'flex') {
            return; 
        }

        // Determine card state
        const isFrontSide = (actionControls.style.display === "none" || actionControls.style.display === "");
        const isBackSide = !isFrontSide;

        switch (event.code) {
            case 'Space':
                event.preventDefault(); // Prevent page scrolling
                if (isFrontSide) {
                    // Simulate click on card to reveal answer
                    cardContentEl.click(); 
                } else { // isBackSide
                    // Simulate click on Save button
                    saveButton.click(); 
                }
                break;

            case 'ArrowLeft':
                if (isBackSide) {
                    event.preventDefault(); 
                    // Simulate click on Discard button
                    discardButton.click(); 
                }
                // No action on front side for Left Arrow
                break;

            case 'F4':
                event.preventDefault(); // Prevent browser default F4 actions
                
                // --- Get Front Text representation for speaking ---
                // Create a temporary element from the stored display text to process it
                const tempDivFront = document.createElement('div');
                tempDivFront.innerHTML = interactiveCards[currentIndex].displayText; 
                // Use helper on the temp div to get text with hints/"blank"
                const frontTextToSpeak = getFrontTextToSpeak(tempDivFront); 

                if (isFrontSide) {
                    // Replay front audio only
                    speakText(frontTextToSpeak); 
                } else { // isBackSide
                    // Replay front THEN back audio
                    stopSpeech(); // Stop any current speech first

                    // --- Get Back Text representation for speaking ---
                    // Find the revealed answers in the *live* DOM
                    const answerSpans = cardContentEl.querySelectorAll('.cloze[data-answer]'); 
                    let answersToSpeak = [];
                    // Use the data-answer which holds the actual cloze content
                    answerSpans.forEach(span => answersToSpeak.push(span.dataset.answer)); 
                    const backTextToSpeak = answersToSpeak.join(", ");
                    
                    // Create utterance for the front part
                    const utteranceFront = new SpeechSynthesisUtterance(frontTextToSpeak);
                    
                    // Define what happens when the front speech ends
                    utteranceFront.onend = () => {
                        // Check TTS is still enabled and we haven't navigated away
                        if (isTtsEnabled && !inEditMode && !finished && (actionControls.style.display === "flex")) {
                            speakText(backTextToSpeak); // Speak the back text
                        }
                    };

                    // Speak the front part only if TTS is enabled
                    if (isTtsEnabled) {
                        synth.speak(utteranceFront); 
                    }
                }
                break;

            case 'F12':
                 event.preventDefault(); // Prevent browser dev tools opening
                // Simulate click on Undo (Previous Card) button
                 undoButton.click(); 
                break;
        
            // New case: Toggle TTS play/pause when "." is pressed.
                 case 'Period':  // Alternatively, you can check event.key === "."
                 event.preventDefault();
                 if (synth.speaking && !synth.paused) {
                     synth.pause();
                 } else if (synth.paused) {
                     synth.resume();
                 }
                 break;
        }
    });
    // END: Add Keyboard Shortcut Listener

{% endraw %}
    // New event listener for downloading the saved cards as an APKG file using Genanki.
    document.getElementById("downloadButton").addEventListener("click", function() {
        if (savedCards.length === 0) {
            alert("No saved cards to download.");
            return;
        }
        fetch("/download_apkg", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ saved_cards: savedCards })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error("Network response was not ok");
            }
            return response.blob();
        })
        .then(blob => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "saved_cards.apkg";
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(url);
        })
        .catch(error => {
            console.error("Download failed:", error);
            alert("Download failed.");
        });
    });
  </script>
  <!-- Keep-alive ping -->
  <script>
    setInterval(() => fetch("/ping").catch(()=>{}), 2 * 60 * 1000);
  </script>
</body>
</html>
"""
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠 ANKI_HTML TEMPLATE 🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠🧠
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
INTERACTIVE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no">
  <title>Interactive Game</title>
  <style>
    /* Global resets and mobile-friendly properties */
    * {
      -webkit-tap-highlight-color: transparent;
      user-select: none;
    }
    html {
      touch-action: manipulation;
    }
    button {
      -webkit-appearance: none;
      outline: none;
    }
    body {
      background-color: #121212;
      color: #f0f0f0;
      font-family: Arial, sans-serif;
      text-align: center;
      padding: 20px;
    }
    .container {
      max-width: 800px;
      margin: 0 auto;
    }
    /* Header with question progress on left and raw score on right */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
    }
    #questionProgress {
      font-size: 20px;
      text-align: left;
    }
    #rawScore {
      font-size: 20px;
      text-align: right;
    }
    .timer {
      font-size: 24px;
      margin-bottom: 20px;
      /* <!-- ADDED CODE START (1/4) --> */
      cursor: pointer; 
      /* <!-- ADDED CODE END (1/4) --> */
    }
    .question-box {
      background-color: #1e1e1e;
      padding: 20px;
      border: 2px solid #bb86fc;
      border-radius: 10px;
      margin-bottom: 20px;
    }
    .options {
      list-style: none;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 10px;
      align-items: center;
    }
    .options li {
      width: 100%;
      max-width: 300px;
    }
    .option-button {
      position: relative;
      overflow: hidden;
      border: none;
      cursor: pointer;
      background: linear-gradient(135deg, #3700b3, #6200ee);
      color: #f0f0f0;
      font-size: 18px;
      width: 100%;
      padding: 10px 20px;
      border-radius: 10px;
      transition: transform 0.3s ease, background 0.3s ease, box-shadow 0.3s ease;
    }
    @media (hover: hover) {
      .option-button:hover {
        transform: scale(1.05);
      }
    }
    .option-button:active {
      transform: scale(0.95);
    }
    .option-button.correct {
      background: #03dac6 !important;
      box-shadow: 0 0 10px #03dac6;
      color: #fff !important;
    }
    .option-button.incorrect {
      background: #cf6679 !important;
      box-shadow: 0 0 10px #cf6679;
      color: #fff !important;
    }
    .hidden {
      display: none;
    }
    /* Ripple effect */
    .ripple {
      position: absolute;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.4);
      transform: scale(0);
      animation: ripple-animation 0.6s linear;
      pointer-events: none;
    }
    @keyframes ripple-animation {
      to {
        transform: scale(4);
        opacity: 0;
      }
    }
    /* Loading Overlay Styles */
    #loadingOverlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: #121212;
      display: flex;
      justify-content: center;
      align-items: center;
      z-index: 9999;
    }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/bodymovin/5.7.6/lottie.min.js"></script>
</head>
<body>
  <!-- Loading Overlay -->
  <div id="loadingOverlay">
    <div id="lottieContainer" style="width: 300px; height: 300px;"></div>
  </div>
  <div class="container" id="gameContainer" style="display: none;">
    <div class="header">
      <div id="questionProgress">Question 1 of 0</div>
      <div id="rawScore">Score: 0</div>
    </div>
    <div class="timer" id="timer">Time: 15</div>
    <div class="question-box" id="questionBox"></div>
    <div id="optionsWrapper"></div>
    <div id="feedback" class="hidden"></div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.5.1/dist/confetti.browser.min.js"></script>
  <script>
    // Initialize Lottie animation
    var animation = lottie.loadAnimation({
      container: document.getElementById('lottieContainer'),
      renderer: 'svg',
      loop: true,
      autoplay: true,
      path: 'https://lottie.host/embed/4500dbaf-9ac9-4b2b-b664-692cd9a3ccab/BGvTKQT8Tx.json'
    });
    // Once the page has fully loaded, hide the loading overlay and show the game container.
    window.addEventListener('load', function() {
      var overlay = document.getElementById('loadingOverlay');
      var gameContainer = document.getElementById('gameContainer');
      overlay.style.transition = 'opacity 0.5s ease';
      overlay.style.opacity = '0';
      setTimeout(function() {
        overlay.style.display = 'none';
        gameContainer.style.display = 'block';
      }, 500);
    });
  </script>
  <script>
    const questions = {{ questions_json|safe }};
    let currentQuestionIndex = 0;
    let score = 0;
    let timerInterval;
    /* <!-- ADDED CODE START (2/4) --> */
    let isTimingEnabled = true; // Timer is on by default
    /* <!-- ADDED CODE END (2/4) --> */
    const totalQuestions = questions.length;
    const questionProgressEl = document.getElementById('questionProgress');
    const rawScoreEl = document.getElementById('rawScore');
    const timerEl = document.getElementById('timer');
    const questionBox = document.getElementById('questionBox');
    const optionsWrapper = document.getElementById('optionsWrapper');
    const feedbackEl = document.getElementById('feedback');

    /* <!-- ADDED CODE START (3/4) --> */
    function toggleTimer() {
        isTimingEnabled = !isTimingEnabled;
        clearInterval(timerInterval); // Stop any active timer.

        if (isTimingEnabled) {
            // Re-enabling the timer. It will start fresh on the next question.
            timerEl.textContent = 'Timer On';
            timerEl.style.textDecoration = 'none';
        } else {
            // Disabling the timer.
            timerEl.textContent = 'Timer Off';
            timerEl.style.textDecoration = 'line-through';
        }
    }
    /* <!-- ADDED CODE END (3/4) --> */

    function startGame() {
      score = 0;
      currentQuestionIndex = 0;
      updateHeader();
      showQuestion();
    }

    function updateHeader() {
      questionProgressEl.textContent = "Question " + (currentQuestionIndex+1) + " of " + totalQuestions;
      rawScoreEl.textContent = "Score: " + score;
    }

    function startTimer(duration, callback) {
      // If timing is off, update the display and do nothing else.
      if (!isTimingEnabled) {
        timerEl.textContent = "Timer Off";
        timerEl.style.textDecoration = "line-through";
        return; // Exit the function
      }

      // If timing is on, reset styles and start the timer.
      timerEl.style.textDecoration = "none";
      let timeRemaining = duration;
      timerEl.textContent = "Time: " + timeRemaining;
      timerInterval = setInterval(() => {
        timeRemaining--;
        timerEl.textContent = "Time: " + timeRemaining;
        if (timeRemaining <= 0) {
          clearInterval(timerInterval);
          callback();
        }
      }, 1000);
    }

    function showQuestion() {
      feedbackEl.classList.add('hidden');
      if (currentQuestionIndex >= totalQuestions) {
        endGame();
        return;
      }
      const currentQuestion = questions[currentQuestionIndex];
      questionBox.textContent = currentQuestion.question;
      optionsWrapper.innerHTML = "";
      const ul = document.createElement('ul');
      ul.className = 'options';

      const optionsShuffled = currentQuestion.options.slice();
      for (let i = optionsShuffled.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [optionsShuffled[i], optionsShuffled[j]] = [optionsShuffled[j], optionsShuffled[i]];
      }
      optionsShuffled.forEach(option => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.textContent = option;
        button.className = 'option-button';
        button.onmousedown = function(e) { e.preventDefault(); };
        button.setAttribute("ontouchend", "this.blur()");
        button.onclick = () => selectAnswer(option);
        button.addEventListener('click', function(e) {
          const rect = button.getBoundingClientRect();
          const ripple = document.createElement('span');
          ripple.className = 'ripple';
          ripple.style.left = (e.clientX - rect.left) + 'px';
          ripple.style.top = (e.clientY - rect.top) + 'px';
          button.appendChild(ripple);
          setTimeout(() => {
            ripple.remove();
          }, 600);
        });
        li.appendChild(button);
        ul.appendChild(li);
      });
      optionsWrapper.appendChild(ul);
      startTimer(15, () => {
        selectAnswer(null);
      });
      updateHeader();
    }

    function selectAnswer(selectedOption) {
      clearInterval(timerInterval);
      const currentQuestion = questions[currentQuestionIndex];
      const buttons = document.querySelectorAll('.option-button');
      const isCorrect = (selectedOption === currentQuestion.correctAnswer);
      buttons.forEach(button => {
        if (button.textContent === currentQuestion.correctAnswer) {
          button.classList.add('correct');
        } else if (button.textContent === selectedOption) {
          button.classList.add('incorrect');
        }
        button.disabled = true;
      });
      if (isCorrect) {
        score++;
        confetti({
          particleCount: 100,
          spread: 70,
          colors: ['#bb86fc', '#ffd700']
        });
      }
      updateHeader();
      setTimeout(() => {
        currentQuestionIndex++;
        showQuestion();
      }, 2000);
    }

    function endGame() {
      questionBox.textContent = "Game Over!";
      optionsWrapper.innerHTML = "";
      timerEl.textContent = "";
      feedbackEl.classList.remove('hidden');
      // Set up final results with Play Again, Show Anki Cards toggle, and Copy Anki Cards button.
      feedbackEl.innerHTML = "<h2>Your final score is " + score + " out of " + totalQuestions + "</h2>" +
        "<button onclick='startGame()' class='option-button' ontouchend='this.blur()'>Play Again</button>" +
        "<button id='toggleAnkiBtn' class='option-button' ontouchend='this.blur()' style='margin-top:10px;'>Show Anki Cards</button>" +
        "<div id='ankiCardsContainer' style='display:none; margin-top:10px; text-align:left; background-color:#1e1e1e; padding:10px; border:1px solid #bb86fc; border-radius:10px;'></div>" +
        "<button id='copyAnkiBtn' class='option-button' ontouchend='this.blur()' style='display:none; margin-top:10px;'>Copy Anki Cards</button>" +
        "<button id='downloadApkgBtn' class='option-button' ontouchend='this.blur()' style='margin-top:10px;'>Download APKG</button>";  // 🗑️ No removal needed here
      // Add event listeners for the new buttons.
      document.getElementById('toggleAnkiBtn').addEventListener('click', function(){
        let container = document.getElementById('ankiCardsContainer');
        let copyBtn = document.getElementById('copyAnkiBtn');
        if (container.style.display === 'none') {
           {% raw %}
           let content = "";
           questions.forEach(q => {
               content += q.question + "<br><br>" + "{" + "{" + "c1::" + q.correctAnswer + "}" + "}" + "<br><br><br>";
           });
           {% endraw %}
           container.innerHTML = content;
           container.style.display = 'block';
           copyBtn.style.display = 'block';
           this.textContent = "Hide Anki Cards";
        } else {
           container.style.display = 'none';
           copyBtn.style.display = 'none';
           this.textContent = "Show Anki Cards";
        }
      });
      document.getElementById('copyAnkiBtn').addEventListener('click', function(){
         let container = document.getElementById('ankiCardsContainer');
         let tempInput = document.createElement('textarea');
         tempInput.value = container.innerText;
         document.body.appendChild(tempInput);
         tempInput.select();
         document.execCommand('copy');
         document.body.removeChild(tempInput);
         this.textContent = "Copied!";
         setTimeout(() => {
             this.textContent = "Copy Anki Cards";
         }, 2000);
      });
            // 🆕🛠️🚀 New Download APKG button listener
      document.getElementById("downloadApkgBtn").addEventListener("click", function() {
        // ✨ Assemble cloze‑formatted strings from questions
        {% raw %}
        const ankiCards = questions.map(q =>
          `${q.question}<br><br>{{c1::${q.correctAnswer}}}`
        );
        {% endraw %}
        fetch("/download_apkg", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ saved_cards: ankiCards })
        })
        .then(res => {
          if (!res.ok) throw new Error("Download failed");
          return res.blob();
        })
        .then(blob => {
          const url = URL.createObjectURL(blob);
          const a   = document.createElement("a");
          a.href    = url;
          a.download = "game_cards.apkg";
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        })
        .catch(err => {
          console.error(err);
          alert("Could not download APKG.");
        });
      });
    }

    /* <!-- ADDED CODE START (4/4) --> */
    timerEl.addEventListener('click', toggleTimer);
    /* <!-- ADDED CODE END (4/4) --> */

    startGame();
  </script>
  <!-- Keep-alive ping -->
  <script>
    setInterval(() => fetch("/ping").catch(()=>{}), 2 * 60 * 1000);
  </script>
</body>
</html>
"""
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮 INTERACTIVE_HTML TEMPLATE 🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮🎮
# ----------------------------
# Flask Routes
# ----------------------------
@app.route("/ping", methods=["GET"])
def ping():
    # simple keep-alive endpoint
    return "", 200
@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

@app.route("/generate", methods=["POST"])
def generate():
    transcript = request.form.get("transcript")
    if not transcript:
        return "Error: Please paste a transcript.", 400
    user_preferences = request.form.get("preferences", "")
    model = request.form.get("model", "gpt-4o-mini")
    max_size_str = request.form.get("max_size", "10000")
    try:
        max_size = int(max_size_str)
    except ValueError:
        max_size = 10000

    mode = request.form.get("mode", "Generate Anki Cards")
    if mode == "Generate Game":
        questions = get_all_interactive_questions(transcript, user_preferences, max_chunk_size=max_size, model=model)
        logger.debug("Final interactive questions list: %s", questions)
        if not questions:
            return "Failed to generate any interactive questions.", 500
        questions_json = json.dumps(questions)
        return render_template_string(INTERACTIVE_HTML, questions_json=questions_json)
    else:
        cards = get_all_anki_cards(transcript, user_preferences, max_chunk_size=max_size, model=model)
        logger.debug("Final flashcards list: %s", cards)
        if not cards:
            return "Failed to generate any Anki cards.", 500
        cards_json = json.dumps(cards)
        return render_template_string(ANKI_HTML, cards_json=cards_json)

@app.route("/download_apkg", methods=["POST"])
def download_apkg():
    data = request.get_json() or {}

    # Normalize input into a list of {html, tags}
    items = []
    if isinstance(data.get("notes"), list):
        for n in data["notes"]:
            if not isinstance(n, dict):
                continue
            html = n.get("html")
            if not html:
                continue
            raw_tags = n.get("tags") or []
            extra = n.get("extra")
            # Clean tags: dedupe and drop empties while preserving original text
            cleaned = []
            seen = set()
            for t in raw_tags:
                if not t:
                    continue
                ct = str(t).strip()
                if not ct:
                    continue
                if ct not in seen:
                    seen.add(ct)
                    cleaned.append(ct)
            items.append({
                "html": html,
                "extra": "" if extra is None else str(extra),
                "tags": cleaned,
            })
    else:
        # Fallback: simple array of strings (no tags)
        saved_cards = data.get("saved_cards") or []
        for s in saved_cards:
            if s:
                items.append({"html": s, "extra": "", "tags": []})

    if not items:
        return "No saved cards provided", 400

    deck_name = data.get("deck_name", "Saved Cards Deck")

    deck = genanki.Deck(2059400110, deck_name)
    model_id = resolve_sacloze_model_id(data)
    model = genanki.Model(
        model_id,
        SACLOZE_MODEL_NAME,
        fields=SACLOZE_FIELDS,
        templates=SACLOZE_TEMPLATES,
        model_type=genanki.Model.CLOZE,
        css=SACLOZE_CSS,
    )
    for it in items:
        note = genanki.Note(
            model=model,
            fields=[it["html"], it.get("extra", "")],
            tags=it["tags"],
        )
        deck.add_note(note)

    package = genanki.Package(deck)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    temp_file.close()
    package.write_to_file(temp_file.name)

    from flask import after_this_request
    @after_this_request
    def cleanup(response):
        try:
            os.remove(temp_file.name)
        except Exception as e:
            logger.error("Temp cleanup failed: %s", e)
        return response

    return send_file(
        temp_file.name,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name="saved_cards.apkg",
    )


if __name__ == "__main__":
    app.run(debug=True, port=10000)