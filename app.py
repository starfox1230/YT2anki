import os
import re
import json
import logging
import requests
from flask import Flask, request, redirect, url_for, flash, render_template_string
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "default-secret-key")

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ----------------------------
# Helper Functions (Improved)
# ----------------------------

def preprocess_transcript(text):
    """Clean transcript with enhanced preprocessing."""
    text = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}', '', text)  # Remove timestamps
    text = re.sub(r'#.*(?:\n|$)', '', text)  # Remove metadata lines starting with #
    return re.sub(r'\s+', ' ', text).strip()  # Normalize whitespace

def chunk_transcript(transcript, max_chunk_size=1500):
    """Split transcript into context-preserving chunks based on original line structure."""
    lines = transcript.splitlines()
    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        line_length = len(line)
        if current_length + line_length > max_chunk_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += line_length

    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks

def parse_json_response(response_text):
    """Robust JSON parsing with error recovery."""
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass  # Proceed to fallback parsing

    # Attempt to extract JSON array and fix common issues
    start = response_text.find('[')
    end = response_text.rfind(']')
    if start == -1 or end == -1:
        return []

    json_str = response_text[start:end+1]
    json_str = re.sub(r'(?<!\\)".*?(?<!\\)"', lambda m: m.group(0).replace('\n', '\\n'), json_str)
    json_str = re.sub(r',\s*]', ']', json_str)  # Remove trailing commas

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: {str(e)}")
        return []

def get_anki_cards_for_chunk(chunk):
    """Generate flashcards for a transcript chunk with enhanced error handling."""
    prompt = f"""Generate Anki cloze cards from this transcript excerpt. Use format: {{c1::answer}}.
Output ONLY a JSON array of strings. No commentary. Transcript:\n\"\"\"{chunk}\"\"\""""

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a flashcard creation expert."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        result = response.choices[0].message.content
        cards = parse_json_response(result)
        
        if not isinstance(cards, list):
            raise ValueError("Invalid response format")
            
        return [card for card in cards if isinstance(card, str)]

    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

# ----------------------------
# Core Processing
# ----------------------------

def process_transcript(transcript):
    """Main processing pipeline with context-aware chunking."""
    chunks = chunk_transcript(transcript)
    all_cards = []
    
    for idx, chunk in enumerate(chunks):
        logger.debug(f"Processing chunk {idx+1}/{len(chunks)}")
        cleaned = preprocess_transcript(chunk)
        if not cleaned:
            continue
            
        cards = get_anki_cards_for_chunk(cleaned)
        if cards:
            all_cards.extend(cards)
            
    return all_cards

# ----------------------------
# Flask Routes (Unchanged)
# ----------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if not (transcript := request.form.get("transcript")):
            flash("Please paste a transcript")
            return redirect(url_for("index"))

        cards = process_transcript(transcript)
        
        if not cards:
            flash("No cards generated - check transcript format")
            return redirect(url_for("index"))
            
        try:
            return render_template_string(ANKI_HTML, cards_json=json.dumps(cards))
        except Exception as e:
            logger.error(f"Rendering error: {str(e)}")
            flash("Error generating output")
            
    return render_template_string(INDEX_HTML)

# HTML templates remain unchanged from original

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))