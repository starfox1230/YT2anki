import os
import re
import json
import logging
from flask import Flask, request, redirect, url_for, flash, render_template_string
from youtube_transcript_api import YouTubeTranscriptApi

# Updated OpenAI API import and initialization using the new formatting.
from openai import OpenAI  # Make sure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize the OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ----------------------------
# Embedded HTML Templates
# ----------------------------

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>YouTube to Anki Cards</title>
  <style>
    body {
      background-color: #1E1E20;
      color: #D7DEE9;
      font-family: Arial, sans-serif;
      text-align: center;
      padding-top: 50px;
    }
    input[type="text"] {
      width: 400px;
      padding: 10px;
      font-size: 16px;
    }
    input[type="submit"] {
      padding: 10px 20px;
      font-size: 16px;
      margin-top: 10px;
    }
    .flash {
      color: red;
    }
  </style>
</head>
<body>
  <h1>YouTube to Anki Cards</h1>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for message in messages %}
        <div class="flash">{{ message }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  <form method="post">
    <input type="text" name="youtube_url" placeholder="Enter YouTube URL" required>
    <br>
    <input type="submit" value="Generate Anki Cards">
  </form>
</body>
</html>
"""

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
    #kard { padding: 0px; max-width: 700px; margin: 20px auto; word-wrap: break-word; }
    .card { font-family: helvetica; font-size: 20px; text-align: center; color: #D7DEE9; line-height: 1.6em; background-color: #2F2F31; padding: 20px; border-radius: 5px; }
    .cloze, .cloze b, .cloze u, .cloze i { font-weight: bold; color: MediumSeaGreen !important; cursor: pointer; }
    #extra, #extra i { font-size: 15px; color:#D7DEE9; font-style: italic; }
    #list { color: #A6ABB9; font-size: 10px; width: 100%; text-align: center; }
    .tags { color: #A6ABB9; opacity: 0; font-size: 10px; text-align: center; text-transform: uppercase; position: fixed; padding: 0px; top:0; right: 0; }
    img { display: block; max-width: 100%; margin: 10px auto; }
    img:active { width: 100%; }
    tr { font-size: 12px; }
    b { color: #C695C6 !important; }
    u { text-decoration: none; color: #5EB3B3; }
    i { color: IndianRed; }
    a { color: LightBlue !important; text-decoration: none; font-size: 14px; }
    ::-webkit-scrollbar { background: #fff; width: 0px; }
    ::-webkit-scrollbar-thumb { background: #bbb; }
    .mobile .card { color: #D7DEE9; background-color: #2F2F31; }
    .iphone .card img { max-width: 100%; }
    .mobile .card img:active { width: inherit; }
    body { background-color: #1E1E20; margin: 0; padding: 0; }
    #progress { text-align: center; font-family: helvetica; color: #A6ABB9; margin-top: 10px; }
    #controls { display: none; justify-content: space-between; max-width: 700px; margin: 20px auto; padding: 0 10px; }
    .controlButton { padding: 10px 20px; font-size: 16px; border: none; color: #fff; border-radius: 5px; cursor: pointer; flex: 1; margin: 0 5px; }
    .discard { background-color: red; }
    .save { background-color: green; }
    .undo { background-color: #4A90E2; }
    #savedCardsContainer { max-width: 700px; margin: 20px auto; font-family: helvetica; color: #D7DEE9; display: none; }
    #savedCardsText { width: 100%; height: 200px; padding: 10px; font-size: 16px; background-color: #2F2F31; color: #D7DEE9; border: none; border-radius: 5px; resize: none; }
    #copyButton { margin-top: 10px; padding: 10px 20px; font-size: 16px; background-color: #4A90E2; color: #fff; border: none; border-radius: 5px; cursor: pointer; }
    #undoContainer { max-width: 700px; margin: 20px auto; text-align: center; }
  </style>
</head>
<body class="mobile">
  <!-- Progress Tracker -->
  <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
  <!-- Card Display -->
  <div id="kard" class="card">
    <div class="tags"></div>
    <div id="cardContent">
      <!-- Processed card content will be injected here -->
    </div>
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
  <!-- Inject generated cards into JS variable -->
  <script>
    const cards = {{ cards_json|safe }};
  </script>
  {% raw %}
  <script>
    /**********************
     * Build Interactive Cards from Input Notes
     **********************/
    let interactiveCards = [];
    function generateInteractiveCards(cardText) {
      const regex = /{{c(\\d+)::(.*?)}}/g;
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
    let generatedCards = [];
    cards.forEach(cardText => {
      generatedCards = generatedCards.concat(generateInteractiveCards(cardText));
    });
    let currentIndex = 0;
    let savedCards = [];
    let historyStack = [];
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
    totalEl.textContent = generatedCards.length;
    function updateUndoButtonState() {
      undoButton.disabled = historyStack.length === 0;
    }
    updateUndoButtonState();
    cardContentEl.addEventListener("click", function(e) {
      if (!controlsEl.style.display || controlsEl.style.display === "none") {
        const clozes = document.querySelectorAll("#cardContent .cloze");
        clozes.forEach(span => {
          span.innerHTML = span.getAttribute("data-answer");
        });
        controlsEl.style.display = "flex";
      }
    });
    function showCard() {
      controlsEl.style.display = "none";
      currentEl.textContent = currentIndex + 1;
      cardContentEl.innerHTML = generatedCards[currentIndex].displayText;
    }
    function nextCard() {
      currentIndex++;
      if (currentIndex >= generatedCards.length) {
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
      savedCards.push(generatedCards[currentIndex].exportText);
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
      document.getElementById("kard").style.display = "block";
      controlsEl.style.display = "none";
      savedCardsContainer.style.display = "none";
      cardContentEl.innerHTML = generatedCards[currentIndex].displayText;
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
    function processCloze(text, target) {
      return text.replace(/{{c(\\d+)::(.*?)}}/g, function(match, clozeNum, answer) {
        if (clozeNum === target) {
          return '<span class="cloze" data-answer="' + answer.replace(/"/g, '&quot;') + '">[...]</span>';
        } else {
          return answer;
        }
      });
    }
    showCard();
  </script>
  {% endraw %}
</body>
</html>
"""

# ----------------------------
# Helper Functions
# ----------------------------

def extract_video_id(url):
    """
    Extract the YouTube video ID from a URL.
    Supports URLs like:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    """
    regex = r"(?:v=|\/)([0-9A-Za-z_-]{11}).*"
    match = re.search(regex, url)
    if match:
        return match.group(1)
    return None

def fetch_transcript(video_id, language="en"):
    """
    Retrieve the transcript using youtube_transcript_api.
    Returns the combined transcript as one string.
    """
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=[language])
        transcript_text = " ".join([entry["text"] for entry in transcript_list])
        return transcript_text
    except Exception as e:
        logger.error("Transcript error: %s", e)
        return None

def get_anki_cards(transcript):
    """
    Build a prompt for ChatGPT that instructs it to generate Anki cloze flashcards.
    Expect output as a JSON array of strings formatted with cloze deletions.
    """
    prompt = f"""
You are an expert at creating study flashcards. Given the transcript below, generate a list of Anki cloze deletion flashcards. Each flashcard should be a string containing a question and its answer in the following format for cloze deletions: {{c1::answer}}.
Output ONLY a valid JSON array of strings (each string is one flashcard) with no additional commentary.

Transcript:
\"\"\"{transcript}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",  # Or your preferred model
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=1500
        )
        result_text = response.choices[0].message.content
        try:
            cards = json.loads(result_text)
            if isinstance(cards, list):
                return cards
        except Exception as e:
            # Try to extract JSON manually if necessary
            start = result_text.find('[')
            end = result_text.rfind(']')
            if start != -1 and end != -1:
                json_str = result_text[start:end+1]
                try:
                    cards = json.loads(json_str)
                    if isinstance(cards, list):
                        return cards
                except:
                    pass
        return None
    except Exception as e:
        logger.error("OpenAI API error: %s", e)
        return None

# ----------------------------
# Flask Routes
# ----------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("youtube_url")
        if not url:
            flash("Please enter a YouTube URL.")
            return redirect(url_for("index"))
        video_id = extract_video_id(url)
        if not video_id:
            flash("Invalid YouTube URL.")
            return redirect(url_for("index"))
        transcript = fetch_transcript(video_id)
        if not transcript:
            flash("Could not retrieve transcript. Make sure the video has captions available.")
            return redirect(url_for("index"))
        cards = get_anki_cards(transcript)
        if not cards:
            flash("Failed to generate Anki cards from the transcript.")
            return redirect(url_for("index"))
        cards_json = json.dumps(cards)
        return render_template_string(ANKI_HTML, cards_json=cards_json)
    return render_template_string(INDEX_HTML)

# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    app.run(debug=True)