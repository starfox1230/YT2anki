import os
import re
import json
import logging
from flask import Flask, request, redirect, url_for, flash, render_template_string
from openai import OpenAI

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "default-secret-key")

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ======================
# HTML Templates
# ======================

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Transcript to Anki Cards</title>
  <style>
    body { background-color: #1E1E20; color: #D7DEE9; 
           font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
    textarea { width: 80%; height: 200px; padding: 10px; font-size: 16px; 
               background: #2F2F31; color: #D7DEE9; border: 1px solid #4A4A4F; }
    input[type="submit"] { padding: 10px 20px; font-size: 16px; margin-top: 10px; 
                           background: #6BB0F5; border: none; border-radius: 4px; 
                           color: #1E1E20; cursor: pointer; }
    .flash { color: #FF6B6B; margin: 10px auto; width: 80%; }
    a { color: #6BB0F5; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Transcript to Anki Cards</h1>
  <p>
    Don't have a transcript? Use the 
    <a href="https://tactiq.io/tools/youtube-transcript" target="_blank">
      Tactiq.io transcript tool
    </a> to generate one.
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
    :root {
      --bg-color: #2F2F31;
      --text-color: #D7DEE9;
      --accent-color: #6BB0F5;
      --border-color: #4A4A4F;
    }
    
    body { background-color: #1E1E20; color: var(--text-color); 
           margin: 0; padding: 20px; font-family: Arial, sans-serif; }
    
    #kard { max-width: 700px; margin: 20px auto; padding: 20px;
            background: var(--bg-color); border-radius: 8px; }
    
    .cloze { color: var(--accent-color); cursor: pointer;
             border-bottom: 1px dashed var(--accent-color); }
    
    #controls { text-align: center; margin: 20px 0; }
    
    button { padding: 10px 20px; margin: 0 10px; border: none;
             border-radius: 4px; cursor: pointer; }
    
    #savedCardsText { width: 80%; height: 150px; margin: 20px auto;
                      background: var(--bg-color); color: var(--text-color);
                      padding: 10px; border: 1px solid var(--border-color); }
  </style>
</head>
<body>
  <div id="kard">
    <div id="cardContent"></div>
  </div>
  
  <div id="controls">
    <button id="showAnswer">Show Answer</button>
    <button id="nextCard">Next Card</button>
  </div>

  <div id="savedCardsContainer">
    <h3>Saved Cards</h3>
    <textarea id="savedCardsText" readonly></textarea>
    <button id="copyButton">Copy to Clipboard</button>
  </div>

  <script>
    const cards = {{ cards_json|safe }};
    let currentCardIndex = 0;
    let savedCards = [];

    function updateCardDisplay() {
      if (currentCardIndex < cards.length) {
        document.getElementById('cardContent').innerHTML = 
          cards[currentCardIndex].replace(/{c1::(.*?)}/g, '<span class="cloze">[...]</span>');
      }
    }

    document.getElementById('showAnswer').addEventListener('click', () => {
      document.getElementById('cardContent').innerHTML = 
        cards[currentCardIndex].replace(/{c1::(.*?)}/g, '<span class="answer">$1</span>');
    });

    document.getElementById('nextCard').addEventListener('click', () => {
      if (currentCardIndex < cards.length - 1) {
        currentCardIndex++;
        updateCardDisplay();
      }
    });

    document.getElementById('copyButton').addEventListener('click', () => {
      navigator.clipboard.writeText(savedCards.join('\n'));
    });

    // Initialize first card
    updateCardDisplay();
  </script>
</body>
</html>
"""

# ======================
# Processing Functions
# ======================

def preprocess_transcript(text):
    """Clean and normalize transcript text."""
    text = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3}', '', text)  # Remove timestamps
    text = re.sub(r'#.*(?:\n|$)', '', text)  # Remove comment lines
    return re.sub(r'\s+', ' ', text).strip()

def chunk_text(text, max_size=1500):
    """Split text into context-aware chunks."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = []
    
    for sentence in sentences:
        if sum(len(s) for s in current_chunk) + len(sentence) > max_size:
            chunks.append(' '.join(current_chunk))
            current_chunk = []
        current_chunk.append(sentence)
    
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    return chunks

def generate_flashcards(chunk):
    """Generate flashcards using OpenAI API."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "system",
                "content": "Generate Anki cloze deletion flashcards. Use format: {c1::answer}. Return ONLY a JSON array of strings."
            }, {
                "role": "user",
                "content": f"Transcript chunk:\n{chunk}"
            }],
            temperature=0.7,
            max_tokens=2000
        )
        
        content = response.choices[0].message.content
        return json.loads(content)
        
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

# ======================
# Flask Routes
# ======================

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        transcript = request.form.get('transcript', '')
        
        if not transcript:
            flash('Please provide a transcript')
            return redirect(url_for('index'))
            
        cleaned = preprocess_transcript(transcript)
        chunks = chunk_text(cleaned)
        all_cards = []
        
        for chunk in chunks:
            try:
                cards = generate_flashcards(chunk)
                if isinstance(cards, list):
                    all_cards.extend(cards)
            except Exception as e:
                logger.error(f"Processing error: {str(e)}")
                
        if not all_cards:
            flash('Could not generate any flashcards')
            return redirect(url_for('index'))
            
        return render_template_string(ANKI_HTML, cards_json=json.dumps(all_cards))
    
    return render_template_string(INDEX_HTML)

# ======================
# Application Startup
# ======================

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))