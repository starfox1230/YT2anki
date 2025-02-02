import os
import re
import json
import logging
import requests
from flask import Flask, request, redirect, url_for, flash, render_template_string
import yt_dlp

# Updated OpenAI API import and initialization.
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

# If an environment variable YOUTUBE_COOKIES is set,
# write its content to a local file so that yt_dlp can use it.
if os.environ.get("YOUTUBE_COOKIES"):
    cookie_content = os.environ.get("YOUTUBE_COOKIES")
    cookie_file_path = "youtube_cookies.txt"
    with open(cookie_file_path, "w") as f:
        f.write(cookie_content)
else:
    cookie_file_path = None

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
    body { background-color: #1E1E20; color: #D7DEE9; font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
    input[type="text"] { width: 400px; padding: 10px; font-size: 16px; }
    input[type="submit"] { padding: 10px 20px; font-size: 16px; margin-top: 10px; }
    .flash { color: red; }
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
    /* Styling omitted for brevity (use your existing CSS) */
    html { overflow: scroll; overflow-x: hidden; }
    #kard { padding: 0px; max-width: 700px; margin: 20px auto; word-wrap: break-word; }
    .card { font-family: helvetica; font-size: 20px; text-align: center; color: #D7DEE9; line-height: 1.6em; background-color: #2F2F31; padding: 20px; border-radius: 5px; }
    /* ... additional styles ... */
  </style>
</head>
<body class="mobile">
  <!-- Progress Tracker and Card Display -->
  <div id="progress">Card <span id="current">0</span> of <span id="total">0</span></div>
  <div id="kard" class="card">
    <div class="tags"></div>
    <div id="cardContent"></div>
  </div>
  <!-- Controls -->
  <div id="controls">
    <button id="discardButton" class="controlButton discard">Discard</button>
    <button id="saveButton" class="controlButton save">Save</button>
  </div>
  <!-- Undo Button -->
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
    // ... rest of the JS code (generateInteractiveCards, showCard, etc.) ...
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
    return match.group(1) if match else None

def parse_srt(srt_text):
    """
    Parse SRT captions and return a plain text transcript.
    Removes numeric indices and timestamp lines.
    """
    lines = srt_text.splitlines()
    transcript_lines = []
    timestamp_pattern = re.compile(r'\d{2}:\d{2}:\d{2},\d{3}')
    for line in lines:
        line = line.strip()
        if line.isdigit() or timestamp_pattern.search(line):
            continue
        if line:
            transcript_lines.append(line)
    return " ".join(transcript_lines)

def fetch_transcript(video_url, language="en"):
    """
    Retrieve the transcript using yt_dlp.
    Extracts video info using yt_dlp (using cookies if available)
    and looks for manual subtitles first, falling back to auto-generated captions.
    Downloads the subtitle file in SRT format and parses it into plain text.
    """
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'subtitleslangs': [language],
        'subtitlesformat': 'srt',
        'quiet': True,
        # Pass the cookies file if it exists
        'cookies': cookie_file_path,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        logger.error("yt_dlp extraction error: %s", e)
        return None

    transcript_url = None
    if 'subtitles' in info and language in info['subtitles']:
        transcript_url = info['subtitles'][language][0]['url']
    elif 'automatic_captions' in info and language in info['automatic_captions']:
        transcript_url = info['automatic_captions'][language][0]['url']

    if not transcript_url:
        logger.error("No captions available for this video.")
        return None

    try:
        r = requests.get(transcript_url)
        if r.status_code != 200:
            logger.error("Failed to download transcript: HTTP %s", r.status_code)
            return None
        srt_text = r.text
        transcript_text = parse_srt(srt_text)
        return transcript_text
    except Exception as e:
        logger.error("Error downloading transcript: %s", e)
        return None

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
        video_url = request.form.get("youtube_url")
        if not video_url:
            flash("Please enter a YouTube URL.")
            return redirect(url_for("index"))
        video_id = extract_video_id(video_url)
        if not video_id:
            flash("Invalid YouTube URL.")
            return redirect(url_for("index"))
        transcript = fetch_transcript(video_url)
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