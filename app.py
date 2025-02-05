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
9. Summary of Key Rules
   • Keep answers concise (single words or short phrases).
   • Use different C-numbers for unrelated deletions.
   • Ensure only one correct answer per deletion.
   • Focus on college-level or expert-level knowledge.
   • Use HTML formatting for better display.
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

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
  </style>
</head>
<body>
  <h1>Transcript to Anki Cards or Interactive Game</h1>
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
    <!-- Advanced Options Toggle -->
    <div id="advancedToggle" onclick="toggleAdvanced()">Advanced Options &#9660;</div>
    <div id="advancedOptions" style="display: none;">
      <label for="modelSelect">Model:</label>
      <select name="model" id="modelSelect">
        <option value="gpt-4o-mini" selected>gpt-4o-mini</option>
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
          toggle.innerHTML = "Advanced Options &#9650;";
      } else {
          adv.style.display = "none";
          toggle.innerHTML = "Advanced Options &#9660;";
      }
    }
  </script>
</body>
</html>
"""

# Anki template with inline onmousedown and ontouchend attributes added to all <button> elements.
ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anki Cloze Review</title>
  <style>
    /* Remove tap highlight on mobile */
    button { -webkit-tap-highlight-color: transparent; }
    /* Remove focus outline */
    button:focus { outline: none; }
    html, body { height: 100%; margin: 0; padding: 0; }
    body { background-color: #1E1E20; font-family: helvetica, Arial, sans-serif; }
    #reviewContainer {
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 10px;
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
    #savedCardsContainer { width: 100%; max-width: 700px; margin: 20px auto; color: #D7DEE9; display: none; }
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
  </style>
</head>
<body>
  <div id="reviewContainer">
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
    <div id="bottomUndo">
      <button id="undoButton" class="bottomButton undo" onmousedown="event.preventDefault()" ontouchend="this.blur()">Previous Card</button>
    </div>
    <div id="bottomEdit">
      <button id="editButton" class="bottomButton edit" onmousedown="event.preventDefault()" ontouchend="this.blur()">Edit</button>
    </div>
    <div id="cartContainer">
      <button id="cartButton" class="bottomButton cart" onmousedown="event.preventDefault()" ontouchend="this.blur()">Saved Cards</button>
    </div>
    <div id="savedCardsContainer">
      <h3 style="text-align:center;">Saved Cards</h3>
      <textarea id="savedCardsText" readonly></textarea>
      <div style="text-align:center;">
        <button id="copyButton" onmousedown="event.preventDefault()" ontouchend="this.blur()">Copy Saved Cards</button>
      </div>
      <div style="text-align:center; margin-top:10px;">
        <button id="returnButton" class="bottomButton return" onmousedown="event.preventDefault()" ontouchend="this.blur()">Return to Card</button>
      </div>
    </div>
  </div>
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
    let inEditMode = false;
    let originalCardText = "";
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
    const savedCardsText = document.getElementById("savedCardsText");
    const copyButton = document.getElementById("copyButton");
    const cartButton = document.getElementById("cartButton");
    const returnButton = document.getElementById("returnButton");
    const cartContainer = document.getElementById("cartContainer");

    totalEl.textContent = interactiveCards.length;

    function updateUndoButtonState() {
      undoButton.disabled = historyStack.length === 0;
    }
    updateUndoButtonState();
    
    document.getElementById("kard").addEventListener("click", function(e) {
      if (inEditMode) return;
      if (actionControls.style.display === "none" || actionControls.style.display === "") {
        const clozes = document.querySelectorAll("#cardContent .cloze");
        clozes.forEach(span => {
          span.innerHTML = span.getAttribute("data-answer");
        });
        actionControls.style.display = "flex";
      }
    });
    
    function showCard() {
      if (!inEditMode) {
        actionControls.style.display = "none";
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
      actionControls.style.display = "none";
      bottomEdit.style.display = "none";
      document.getElementById("progress").textContent = "Review complete!";
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "block";
    }
    
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
      cardContentEl.innerHTML = '<textarea id="editArea">' + interactiveCards[currentIndex].exportText + '</textarea>';
      actionControls.style.display = "none";
      bottomUndo.style.display = "none";
      bottomEdit.style.display = "none";
      editControls.style.display = "flex";
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
      bottomUndo.style.display = "flex";
      bottomEdit.style.display = "flex";
      showCard();
    });
    cancelEditButton.addEventListener("click", function(e) {
      e.stopPropagation();
      inEditMode = false;
      editControls.style.display = "none";
      bottomUndo.style.display = "flex";
      bottomEdit.style.display = "flex";
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
      actionControls.style.display = "none";
      savedCardsContainer.style.display = "none";
      cartContainer.style.display = "flex";
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

    cartButton.addEventListener("click", function(e) {
      e.stopPropagation();
      savedCardIndex = currentIndex;
      document.getElementById("kard").style.display = "none";
      actionControls.style.display = "none";
      bottomUndo.style.display = "none";
      bottomEdit.style.display = "none";
      cartContainer.style.display = "none";
      savedCardsText.value = savedCards.join("\\n");
      savedCardsContainer.style.display = "block";
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
{% endraw %}
  </script>
</body>
</html>
"""

# Interactive Game template with new button styling (using the same resets, layout, and ripple effect as the provided sample,
# while keeping our original button colors and glowing effects).
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
    h1 { color: #bb86fc; }
    .question-box {
      background-color: #1e1e1e;
      padding: 20px;
      border: 2px solid #bb86fc;
      border-radius: 10px;
      margin-bottom: 20px;
    }
    .score {
      font-size: 20px;
      margin-bottom: 20px;
    }
    .timer {
      font-size: 24px;
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
      /* Keep our original colors but use a gradient layout similar to the sample */
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
      background-color: #03dac6;
      box-shadow: 0 0 10px #03dac6;
    }
    .option-button.incorrect {
      background-color: #cf6679;
      box-shadow: 0 0 10px #cf6679;
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
  </style>
</head>
<body>
  <div class="container">
    <h1>Interactive Game</h1>
    <div class="score" id="score">Score: 0 / 0</div>
    <div class="timer" id="timer">Time: 15</div>
    <div class="question-box" id="questionBox"></div>
    <div id="optionsWrapper"></div>
    <div id="feedback" class="hidden"></div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.5.1/dist/confetti.browser.min.js"></script>
  <script>
    const questions = {{ questions_json|safe }};
    let currentQuestionIndex = 0;
    let score = 0;
    let timerInterval;
    const totalQuestions = questions.length;
    const scoreEl = document.getElementById('score');
    const timerEl = document.getElementById('timer');
    const questionBox = document.getElementById('questionBox');
    const optionsWrapper = document.getElementById('optionsWrapper');
    const feedbackEl = document.getElementById('feedback');

    function startGame() {
      score = 0;
      currentQuestionIndex = 0;
      updateScore();
      showQuestion();
    }

    function updateScore() {
      scoreEl.textContent = `Score: ${score} / ${totalQuestions}`;
    }

    function startTimer(duration, callback) {
      let timeRemaining = duration;
      timerEl.textContent = `Time: ${timeRemaining}`;
      timerInterval = setInterval(() => {
        timeRemaining--;
        timerEl.textContent = `Time: ${timeRemaining}`;
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
      updateScore();
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
      feedbackEl.innerHTML = `<h2>Your final score is ${score} out of ${totalQuestions}</h2><button onclick="startGame()" class="option-button" ontouchend="this.blur()">Play Again</button>`;
    }

    startGame();
  </script>
</body>
</html>
"""

# ----------------------------
# Flask Routes
# ----------------------------

@app.route("/", methods=["GET", "POST", "HEAD"])
def index():
    if request.method == "HEAD":
        return ""
    if request.method == "POST":
        transcript = request.form.get("transcript")
        if not transcript:
            flash("Please paste a transcript.")
            return redirect(url_for("index"))
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
                flash("Failed to generate any interactive questions.")
                return redirect(url_for("index"))
            questions_json = json.dumps(questions)
            return render_template_string(INTERACTIVE_HTML, questions_json=questions_json)
        else:
            cards = get_all_anki_cards(transcript, user_preferences, max_chunk_size=max_size, model=model)
            logger.debug("Final flashcards list: %s", cards)
            if not cards:
                flash("Failed to generate any Anki cards.")
                return redirect(url_for("index"))
            cards_json = json.dumps(cards)
            return render_template_string(ANKI_HTML, cards_json=cards_json)
    return render_template_string(INDEX_HTML)

if __name__ == "__main__":
    app.run(debug=True, port=10000)