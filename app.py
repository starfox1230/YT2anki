import os
import re
import json
import logging
import requests
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
    Don't have a transcript? Use the <a href="https://tactiq.io" target="_blank">Tactiq.io</a> service to generate one.
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

ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anki Cloze Review</title>
  <style>
    html { overflow: scroll; overflow-x: hidden; }
    #kard { padding: 0px; max-width: 700px; margin: 20px auto; word-wrap: break-word; }
    .card { font-family: helvetica; font-size: 20px; text-align: center; color: #D7DEE9; line-height: 1.6em; background-color: #2F2F31; padding: 20px; border-radius: 5px; }
    /* Additional styling omitted for brevity */
  </style>
</head>
<body class="mobile">
  <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
  <div id="kard" class="card">
    <div class="tags"></div>
    <div id="cardContent"></div>
  </div>
  <div id="controls">
    <button id="discardButton" class="controlButton discard">Discard</button>
    <button id="saveButton" class="controlButton save">Save</button>
  </div>
  <div id="undoContainer">
    <button id="undoButton" class="controlButton undo">Undo</button>
  </div>
  <div id="savedCardsContainer">
    <h3 style="text-align:center;">Saved Cards</h3>
    <textarea id="savedCardsText" readonly></textarea>
    <div style="text-align:center;">
      <button id="copyButton">Copy Saved Cards</button>
    </div>
  </div>
  <script>
    const cards = {{ cards_json|safe }};
  </script>
  {% raw %}
  <script>
    // JavaScript for interactive card generation remains unchanged.
    // (Use your existing JavaScript code.)
    function processCloze(text, target) {
      return text.replace(/{{c(\\d+)::(.*?)}}/g, function(match, clozeNum, answer) {
        if (clozeNum === target) {
          return '<span class="cloze" data-answer="' + answer.replace(/"/g, '&quot;') + '">[...]</span>';
        } else {
          return answer;
        }
      });
    }
    // ... rest of your JS code (generateInteractiveCards, showCard, etc.) ...
  </script>
  {% endraw %}
</body>
</html>
"""

# ----------------------------
# Helper Function
# ----------------------------

def get_anki_cards(transcript):
    """
    Build a prompt for ChatGPT that instructs it to generate Anki cloze deletion flashcards.
    Expects output as a JSON array of strings formatted with cloze deletions.
    """
    prompt = f"""
You are an expert at creating study flashcards. Given the transcript below, generate a list of Anki cloze deletion flashcards.
Each flashcard should be a string containing a question and its answer in the format: {{c1::answer}}.
Output ONLY a valid JSON array of strings (each string is one flashcard) with no additional commentary.

Transcript:
\"\"\"{transcript}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Or your preferred model
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        result_text = response.choices[0].message.content
        try:
            cards = json.loads(result_text)
            if isinstance(cards, list):
                return cards
        except Exception:
            start = result_text.find('[')
            end = result_text.rfind(']')
            if start != -1 and end != -1:
                json_str = result_text[start:end+1]
                try:
                    cards = json.loads(json_str)
                    if isinstance(cards, list):
                        return cards
                except Exception:
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
        transcript = request.form.get("transcript")
        if not transcript:
            flash("Please paste a transcript.")
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