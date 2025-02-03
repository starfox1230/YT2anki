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

def get_anki_cards_for_chunk(transcript_chunk, user_preferences=""):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    The API is instructed to output only a valid JSON array of strings, each string formatted as a complete,
    self-contained cloze deletion using the exact format: {{c1::...}}.
    
    Additional formatting instructions:

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
       • Focus on college-level or expert knowledge.

    7. Using Cloze Deletion for Definitions
       • Definitions should follow the “is defined as” structure for clarity.
             Example: "A {{c1::pneumothorax}} is defined as {{c2::air in the pleural space}}."
    
    8. Formatting Output in HTML for Readability
       • Use line breaks (<br><br>) to properly space question and answer.
             Example:
             What is the capital of Australia?<br><br>{{c1::Canberra}}
       • This keeps the cloze deletion on a separate line, improving readability.

    9. Summary of Key Rules
       • Keep answers concise (single words or short phrases).
       • Use different C-numbers for unrelated deletions.
       • Ensure only one correct answer per deletion.
       • Focus on college-level or expert-level knowledge.
       • Use HTML formatting for better display.
    
    FEATURE REQUEST – User-Directed Card Generation:
       • Additionally, include the following user preference in your instructions:
         "User Request: <user_preferences>"
       • If the user’s preference is provided, generate cards that focus on that content.
       • If a particular chunk does not contain content relevant to the user’s request, output a dummy card in the following format:
         "User request not found in {{c1::this chunk}}."

    Remember: Output ONLY a valid JSON array of strings, with no extra commentary.
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
9. Summary of Key Rules
   • Keep answers concise (single words or short phrases).
   • Use different C-numbers for unrelated deletions.
   • Ensure only one correct answer per deletion.
   • Focus on college-level or expert-level knowledge.
   • Use HTML formatting for better display.
{user_instr}
Ensure you output ONLY a valid JSON array of strings, with no additional commentary or markdown.
    
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

def get_all_anki_cards(transcript, user_preferences="", max_chunk_size=4000):
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
        cards = get_anki_cards_for_chunk(chunk, user_preferences)
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
    textarea, input[type="text"] { width: 80%; padding: 10px; font-size: 16px; margin-bottom: 10px; }
    textarea { height: 200px; }
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
    <input type="text" name="preferences" placeholder="Enter your card preferences (optional)">
    <br>
    <input type="submit" value="Generate Anki Cards">
  </form>
</body>
</html>
"""

# The review page uses your provided demo styling and interactive behavior.
# The inline JavaScript is wrapped in raw/endraw tags so that regex literals and curly braces are not misinterpreted.
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
    #kard { padding: 0px 0px; max-width: 700px; margin: 20px auto; word-wrap: break-word; position: relative; }
    .card { font-family: helvetica; font-size: 20px; text-align: center; color: #D7DEE9; line-height: 1.6em; background-color: #2F2F31; padding: 20px; border-radius: 5px; }
    /* Edit mode styling for textarea */
    #editArea { width: 100%; height: 150px; font-size: 20px; padding: 10px; }
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
    /* Control buttons container */
    #controls { display: flex; justify-content: space-between; max-width: 700px; margin: 20px auto; padding: 0 10px; }
    .controlButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; }
    .discard { background-color: red; }
    .save { background-color: green; }
    .edit { background-color: #FFA500; } /* Orange for Edit */
    .saveEdit { background-color: green; }
    .cancelEdit { background-color: gray; }
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
  <!-- Controls for non-edit mode -->
  <div id="controls">
    <button id="discardButton" class="controlButton discard">Discard</button>
    <button id="saveButton" class="controlButton save">Save</button>
    <button id="editButton" class="controlButton edit">Edit</button>
  </div>
  <!-- Controls for edit mode (hidden by default) -->
  <div id="editControls" style="display:none; justify-content: space-between; max-width: 700px; margin: 20px auto; padding: 0 10px;">
    <button id="saveEditButton" class="controlButton saveEdit">Save Edit</button>
    <button id="cancelEditButton" class="controlButton cancelEdit">Cancel Edit</button>
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
    // Each card object: { target, displayText, exportText }
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
      return text.replace(/{{c(\d+)::(.*?)}}/g, function(match, clozeNum, answer) {
        if (clozeNum === target) {
          return '<span class="cloze" data-answer="' + answer.replace(/"/g, '&quot;') + '">[...]</span>';
        } else {
          return answer;
        }
      });
    }
    cards.forEach(cardText => {
      interactiveCards = interactiveCards.concat(generateInteractiveCards(cardText));
    });
    let currentIndex = 0;
    let savedCards = [];
    let historyStack = [];
    let inEditMode = false; // Track edit mode
    let originalCardText = ""; // Save original card text for canceling edits

    /*********************
     * Element Selectors *
     *********************/
    const currentEl = document.getElementById("current");
    const totalEl = document.getElementById("total");
    const cardContentEl = document.getElementById("cardContent");
    const discardButton = document.getElementById("discardButton");
    const saveButton = document.getElementById("saveButton");
    const editButton = document.getElementById("editButton");
    const editControls = document.getElementById("editControls");
    const saveEditButton = document.getElementById("saveEditButton");
    const cancelEditButton = document.getElementById("cancelEditButton");
    const controlsEl = document.getElementById("controls");
    const undoButton = document.getElementById("undoButton");
    const savedCardsContainer = document.getElementById("savedCardsContainer");
    const savedCardsText = document.getElementById("savedCardsText");
    const copyButton = document.getElementById("copyButton");
    totalEl.textContent = interactiveCards.length;
    function updateUndoButtonState() {
      undoButton.disabled = historyStack.length === 0;
    }
    updateUndoButtonState();

    /***************************
     * Global Reveal on Touch *
     ***************************/
    cardContentEl.addEventListener("click", function(e) {
      if (!controlsEl.style.display || controlsEl.style.display === "none") {
        const clozes = document.querySelectorAll("#cardContent .cloze");
        clozes.forEach(span => {
          span.innerHTML = span.getAttribute("data-answer");
        });
        controlsEl.style.display = "flex";
      }
    });

    /***************************
     * Card Display Functions *
     ***************************/
    function showCard() {
      if (!inEditMode) {
        controlsEl.style.display = "flex";
      }
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
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "block";
    }

    /***********************
     * Button Event Listeners *
     ***********************/
    discardButton.addEventListener("click", function(e) {
      e.stopPropagation();
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice() });
      updateUndoButtonState();
      nextCard();
    });
    saveButton.addEventListener("click", function(e) {
      e.stopPropagation();
      historyStack.push({ currentIndex: currentIndex, savedCards: savedCards.slice() });
      updateUndoButtonState();
      savedCards.push(interactiveCards[currentIndex].exportText);
      nextCard();
    });
    editButton.addEventListener("click", function(e) {
      e.stopPropagation();
      if (!inEditMode) enterEditMode();
    });
    function enterEditMode() {
      inEditMode = true;
      originalCardText = interactiveCards[currentIndex].exportText;
      // Replace card content with a textarea pre-filled with the current export text.
      cardContentEl.innerHTML = '<textarea id="editArea">' + interactiveCards[currentIndex].exportText + '</textarea>';
      // Hide main controls and show edit-specific controls.
      controlsEl.style.display = "none";
      editControls.style.display = "flex";
    }
    saveEditButton.addEventListener("click", function(e) {
      e.stopPropagation();
      const editedText = document.getElementById("editArea").value;
      // Update the current card object with the edited text.
      interactiveCards[currentIndex].exportText = editedText;
      interactiveCards[currentIndex].displayText = editedText;
      // Exit edit mode and display the updated card.
      inEditMode = false;
      editControls.style.display = "none";
      showCard();
    });
    cancelEditButton.addEventListener("click", function(e) {
      e.stopPropagation();
      // Revert any changes and exit edit mode.
      inEditMode = false;
      editControls.style.display = "none";
      showCard();
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
      document.getElementById("kard").style.display = "block";
      controlsEl.style.display = "flex";
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
        user_preferences = request.form.get("preferences", "")
        cards = get_all_anki_cards(transcript, user_preferences)
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
    app.run(debug=True, port=10000)