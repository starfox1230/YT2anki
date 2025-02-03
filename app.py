import os
import re
import json
import logging
from flask import Flask, request, redirect, url_for, flash, render_template_string

# Updated OpenAI API import and initialization.
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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
    # If double opening braces are missing, add them.
    if "{{" not in card:
        card = card.replace("{c", "{{c")
    # Ensure that the closing braces are doubled.
    card = re.sub(r'(?<!})}(?!})', '}}', card)
    return card

def get_anki_cards_for_chunk(transcript_chunk):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    The API is instructed to output only a valid JSON array of strings, each string formatted as a complete,
    self-contained cloze deletion using the exact format: {{c1::...}}.
    """
    prompt = f"""
You are an expert at creating study flashcards in Anki using cloze deletion.
Given the transcript below, generate a list of flashcards.
Each flashcard should be a complete, self-contained sentence (or sentence fragment) containing a cloze deletion formatted exactly as:
  {{c1::hidden text}}
Ensure that:
- You use double curly braces for the cloze deletion.
- Do not include any extra numbering, labels, or commentary.
- Output ONLY a valid JSON array of strings with no markdown formatting or additional text.

Transcript:
\"\"\"{transcript_chunk}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # Your chosen model.
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000,
            timeout=15
        )
        result_text = response.choices[0].message.content.strip()
        logger.debug("Raw API response for chunk: %s", result_text)
        try:
            cards = json.loads(result_text)
            if isinstance(cards, list):
                # Fix the cloze formatting in each card.
                cards = [fix_cloze_formatting(card) for card in cards]
                return cards
        except Exception as parse_err:
            logger.error("JSON parsing error for chunk: %s", parse_err)
            # Fallback: attempt to extract JSON substring manually.
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

def get_all_anki_cards(transcript, max_chunk_size=2000):
    """
    Preprocesses the transcript, splits it into chunks, and processes each chunk to generate Anki cards.
    Returns a combined list of all flashcards.
    """
    cleaned_transcript = preprocess_transcript(transcript)
    logger.debug("Cleaned transcript (first 200 chars): %s", cleaned_transcript[:200])
    chunks = chunk_text(cleaned_transcript, max_chunk_size)
    all_cards = []
    for i, chunk in enumerate(chunks):
        logger.debug("Processing chunk %d/%d", i+1, len(chunks))
        cards = get_anki_cards_for_chunk(chunk)
        logger.debug("Chunk %d produced %d cards.", i+1, len(cards))
        all_cards.extend(cards)
    logger.debug("Total flashcards generated: %d", len(all_cards))
    return all_cards

# ----------------------------
# Embedded HTML Templates
# ----------------------------

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Transcript to Anki Cards</title>
  <style>
    body { background-color: #1E1E20; color: #D7DEE9; font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
    textarea { width: 80%; height: 200px; padding: 10px; font-size: 16px; }
    input[type="submit"] { padding: 10px 20px; font-size: 16px; margin-top: 10px; }
    .flash { color: red; }
    a { color: #6BB0F5; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Transcript to Anki Cards</h1>
  <p>
    Don't have a transcript? Use the <a href="https://tactiq.io/tools/youtube-transcript" target="_blank">Tactiq.io transcript tool</a> to generate one.
  </p>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for message in messages %}
        <div class="flash">{{ message }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  <form method="post">
    <textarea name="transcript" placeholder="Paste your transcript here" required></textarea>
    <br>
    <input type="submit" value="Generate Anki Cards">
  </form>
</body>
</html>
"""

# The review page uses your provided demo styling and interactive behavior.
# Note the inline JavaScript is wrapped in raw/endraw so that the regex literal and curly braces are not misinterpreted.
ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anki Cloze Review</title>
  <style>
    /* Provided Styling */
    html { overflow: scroll; overflow-x: hidden; }
    #kard { padding: 0px 0px; max-width: 700px; margin: 20px auto; word-wrap: break-word; }
    .card { font-family: helvetica; font-size: 20px; text-align: center; color: #D7DEE9; line-height: 1.6em; background-color: #2F2F31; padding: 20px; border-radius: 5px; }
    /* Cloze deletions styled in MediumSeaGreen. In display, they will show as square brackets with an ellipsis. */
    .cloze, .cloze b, .cloze u, .cloze i { font-weight: bold; color: MediumSeaGreen !important; cursor: pointer; }
    #extra, #extra i { font-size: 15px; color:#D7DEE9; font-style: italic; }
    #list { color: #A6ABB9; font-size: 10px; width: 100%; text-align: center; }
    .tags { color: #A6ABB9; opacity: 0; font-size: 10px; text-align: center; text-transform: uppercase; position: fixed; padding: 0px; top:0; right: 0; }
    img { display: block; max-width: 100%; max-height: none; margin-left: auto; margin: 10px auto; }
    img:active { width: 100%; }
    tr { font-size: 12px; }
    /* Accent Colors */
    b { color: #C695C6 !important; }
    u { text-decoration: none; color: #5EB3B3; }
    i { color: IndianRed; }
    a { color: LightBlue !important; text-decoration: none; font-size: 14px; font-style: normal; }
    ::-webkit-scrollbar { background: #fff; width: 0px; }
    ::-webkit-scrollbar-thumb { background: #bbb; }
    /* Mobile styling */
    .mobile .card { color: #D7DEE9; background-color: #2F2F31; }
    .iphone .card img { max-width: 100%; max-height: none; }
    .mobile .card img:active { width: inherit; max-height: none; }
    /* Additional layout styling */
    body { background-color: #1E1E20; margin: 0; padding: 0; }
    #progress { text-align: center; font-family: helvetica; color: #A6ABB9; margin-top: 10px; }
    /* The control buttons are hidden until a card is revealed */
    #controls { display: none; justify-content: space-between; max-width: 700px; margin: 20px auto; padding: 0 10px; }
    .controlButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; }
    .discard { background-color: red; }
    .save { background-color: green; }
    /* Undo button: use a distinctive blue */
    .undo { background-color: #4A90E2; }
    /* Saved cards output styling */
    #savedCardsContainer { max-width: 700px; margin: 20px auto; font-family: helvetica; color: #D7DEE9; display: none; }
    #savedCardsText { width: 100%; height: 200px; padding: 10px; font-size: 16px; background-color: #2F2F31; color: #D7DEE9; border: none; border-radius: 5px; resize: none; }
    #copyButton { margin-top: 10px; padding: 10px 20px; font-size: 16px; background-color: #4A90E2; color: #fff; border: none; border-radius: 5px; cursor: pointer; }
    /* Undo container styling */
    #undoContainer { max-width: 700px; margin: 20px auto; text-align: center; }
  </style>
</head>
<body class="mobile">
  <!-- Progress Tracker -->
  <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
  <!-- Card Display -->
  <div id="kard" class="card">
    <div class="tags"></div>
    <div id="cardContent"><!-- Processed card content will be injected here --></div>
  </div>
  <!-- Controls (hidden until the card is revealed) -->
  <div id="controls">
    <button id="discardButton" class="controlButton discard">Discard</button>
    <button id="saveButton" class="controlButton save">Save</button>
  </div>
  <!-- Undo Button (always visible) -->
  <div id="undoContainer">
    <button id="undoButton" class="controlButton undo">Undo</button>
  </div>
  <!-- Saved Cards Output -->
  <div id="savedCardsContainer">
    <h3 style="text-align:center;">Saved Cards</h3>
    <textarea id="savedCardsText" readonly></textarea>
    <div style="text-align:center;">
      <button id="copyButton">Copy Saved Cards</button>
    </div>
  </div>
  <script>
    // Inject the generated cards from the backend.
    const cards = {{ cards_json|safe }};
{% raw %}
    /**********************
     * Build Interactive Cards from Generated Notes *
     **********************/
    let interactiveCards = [];
    // Generate interactive card objects from a note.
    // Each object has:
    // - target: the cloze number to hide (e.g. "1" or "2")
    // - displayText: the processed text for display (with target cloze(s) hidden)
    // - exportText: the original note text (with proper curly braces) to be saved for later.
    function generateInteractiveCards(cardText) {
      const regex = /{{c(\d+)::(.*?)}}/g;
      const numbers = new Set();
      let m;
      while ((m = regex.exec(cardText)) !== null) {
        numbers.add(m[1]);
      }
      // If no cloze deletion is found, return the card as is.
      if (numbers.size === 0) {
        return [{ target: null, displayText: cardText, exportText: cardText }];
      }
      const cardsForNote = [];
      // For each unique cloze number, generate a separate interactive card.
      Array.from(numbers).sort().forEach(num => {
        const display = processCloze(cardText, num);
        cardsForNote.push({ target: num, displayText: display, exportText: cardText });
      });
      return cardsForNote;
    }
    // Process a card’s text so that:
    // - For each cloze deletion matching the target number, replace it with a clickable span that initially shows "[...]"
    // - For all other cloze deletions, simply reveal the answer.
    function processCloze(text, target) {
      return text.replace(/{{c(\d+)::(.*?)}}/g, function(match, clozeNum, answer) {
        if (clozeNum === target) {
          return '<span class="cloze" data-answer="' + answer.replace(/"/g, '&quot;') + '">[...]</span>';
        } else {
          return answer;
        }
      });
    }
    // Build the full interactive cards array.
    cards.forEach(cardText => {
      interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
    });
    let currentIndex = 0;
    let savedCards = [];
    // This history stack will save snapshots of state so that an undo can revert an action.
    let historyStack = [];

    /*********************
     * Element Selectors *
     *********************/
    const currentEl = document.getElementById("current");
    const totalEl = document.getElementById("total");
    const cardContentEl = document.getElementById("cardContent");
    const discardButton = document.getElementById("discardButton");
    const saveButton = document.getElementById("saveButton");
    const controlsEl = document.getElementById("controls");
    const savedCardsContainer = document.getElementById("savedCardsContainer");
    const savedCardsText = document.getElementById("savedCardsText");
    const copyButton = document.getElementById("copyButton");
    const undoButton = document.getElementById("undoButton");
    totalEl.textContent = interactiveCards.length;
    // A helper to disable the undo button when there is no history.
    function updateUndoButtonState() {
      undoButton.disabled = historyStack.length === 0;
    }
    updateUndoButtonState();

    /***************************
     * Global Reveal on Touch *
     ***************************/
    // When the user taps anywhere in the card area (except the control buttons), reveal all hidden clozes.
    cardContentEl.addEventListener("click", function(e) {
      // Only reveal if controls are not yet visible.
      if (!controlsEl.style.display || controlsEl.style.display === "none") {
        const clozes = document.querySelectorAll("#cardContent .cloze");
        clozes.forEach(span => {
          // Replace the placeholder "[...]" with the actual answer.
          span.innerHTML = span.getAttribute("data-answer");
        });
        controlsEl.style.display = "flex";
      }
    });

    /***************************
     * Card Display Functions *
     ***************************/
    function showCard() {
      // Hide controls until the card is revealed (via tap)
      controlsEl.style.display = "none";
      currentEl.textContent = currentIndex + 1;
      cardContentEl.innerHTML = interactiveCards[currentIndex].displayText;
    }
    function nextCard() {
      currentIndex++;
      if (currentIndex >= interactiveCards.length) {
        finish();
      } else {
        showCard();
      }
    }
    function finish() {
      document.getElementById("kard").style.display = "none";
      controlsEl.style.display = "none";
      document.getElementById("progress").textContent = "Review complete!";
      // When finished, join saved cards with a newline (each note is one field in the cloze format)
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "block";
    }

    /***********************
     * Button Event Listeners *
     ***********************/
    // Prevent clicks on the buttons from propagating to the cardContent (which would trigger a reveal).
    discardButton.addEventListener("click", function(e) {
      e.stopPropagation();
      // Save snapshot of the current state before moving on.
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice() });
      updateUndoButtonState();
      nextCard();
    });
    saveButton.addEventListener("click", function(e) {
      e.stopPropagation();
      // Save snapshot of the current state before moving on.
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice() });
      updateUndoButtonState();
      // Save the original note text.
      savedCards.push(interactiveCards[currentIndex].exportText);
      nextCard();
    });
    undoButton.addEventListener("click", function(e) {
      e.stopPropagation();
      if (historyStack.length === 0) {
        alert("No actions to undo.");
        return;
      }
      let snapshot = historyStack.pop();
      currentIndex = snapshot.currentIndex;
      savedCards = snapshot.savedCards.slice();
      // Restore card display and progress elements without overwriting the span elements.
      document.getElementById("kard").style.display = "block";
      controlsEl.style.display = "none";
      savedCardsContainer.style.display = "none";
      cardContentEl.innerHTML = interactiveCards[currentIndex].displayText;
      currentEl.textContent = currentIndex + 1;
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

    /***********************
     * Start the Review *
     ***********************/
    showCard();
{% endraw %}
  </script>
</body>
</html>
"""

# ----------------------------
# Flask Routes
# ----------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        transcript = request.form.get("transcript")
        if not transcript:
            flash("Please paste a transcript.")
            return redirect(url_for("index"))
        # Preprocess and generate Anki cards from the transcript.
        cards = get_all_anki_cards(transcript)
        logger.debug("Final flashcards list: %s", cards)
        if not cards:
            flash("Failed to generate any Anki cards.")
            return redirect(url_for("index"))
        cards_json = json.dumps(cards)
        return render_template_string(ANKI_HTML, cards_json=cards_json)
    return render_template_string(INDEX_HTML)

# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    # Bind to port 10000 as recommended by Render
    app.run(debug=True, port=10000)