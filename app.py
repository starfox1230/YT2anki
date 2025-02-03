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

def get_anki_cards_for_chunk(transcript_chunk):
    """
    Calls the OpenAI API with a transcript chunk and returns a list of Anki cloze deletion flashcards.
    A timeout of 15 seconds is set for the API call.
    The API is instructed to output only a JSON array of strings, each string formatted as a complete
    cloze deletion card in the form: {{c1::...}} with no extra numbering or text.
    """
    prompt = f"""
You are an expert at creating study flashcards in Anki using cloze deletion format.
Given the transcript below, generate a list of flashcards. Each flashcard should be a complete,
self-contained sentence (or sentence fragment) containing a cloze deletion formatted exactly as follows:
  {{c1::hidden text}}
Ensure that:
- You use double curly braces for the cloze deletion.
- You do not include any extra numbering, labels, or commentary.
- Output ONLY a valid JSON array of strings with no markdown or additional text.

Transcript:
\"\"\"{transcript_chunk}\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Using your preferred model.
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

# The review page now displays one card at a time without extra numbering or prefixes.
ANKI_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anki Cloze Review</title>
  <style>
    html { overflow: hidden; }
    body { background-color: #1E1E20; color: #D7DEE9; font-family: Arial, sans-serif; text-align: center; padding: 30px; }
    #kard { margin: 20px auto; padding: 20px; max-width: 700px; background-color: #2F2F31; border-radius: 5px; }
    #kard p { font-size: 24px; }
    #progress { margin-bottom: 20px; font-size: 18px; }
    .controlButton { padding: 10px 20px; margin: 5px; font-size: 16px; cursor: pointer; }
    #controls { margin-top: 20px; }
  </style>
</head>
<body>
  <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
  <div id="kard">
    <div id="cardContent"><p>Loading...</p></div>
  </div>
  <div id="controls">
    <button id="prevButton" class="controlButton">Previous</button>
    <button id="nextButton" class="controlButton">Next</button>
  </div>
  <script>
    // The cards variable is rendered from the server.
    const cards = {{ cards_json|safe }};
    let currentCardIndex = 0;

    function showCard(index) {
      if (index < 0 || index >= cards.length) return;
      const cardContent = document.getElementById("cardContent");
      // Render the card text directly without additional labels.
      cardContent.innerHTML = `<p>${cards[index]}</p>`;
      document.getElementById("current").textContent = index + 1;
    }

    function nextCard() {
      if (currentCardIndex < cards.length - 1) {
        currentCardIndex++;
        showCard(currentCardIndex);
      }
    }

    function prevCard() {
      if (currentCardIndex > 0) {
        currentCardIndex--;
        showCard(currentCardIndex);
      }
    }

    document.addEventListener("DOMContentLoaded", function() {
      document.getElementById("total").textContent = cards.length;
      showCard(currentCardIndex);
      document.getElementById("nextButton").addEventListener("click", nextCard);
      document.getElementById("prevButton").addEventListener("click", prevCard);
    });
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