import os
import re
import json
import logging
from flask import Flask, render_template, request, redirect, url_for, flash
from youtube_transcript_api import YouTubeTranscriptApi

# Updated OpenAI API import and initialization
from openai import OpenAI  # Ensure you have the correct version installed

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Replace with a secure secret

# Set up logging for debugging purposes
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize the OpenAI client using the new formatting
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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
    Use youtube_transcript_api to retrieve transcript text.
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
            model="gpt-3.5-turbo",  # or use your preferred model
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
            # If JSON parsing fails, try to extract the JSON array manually.
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
            flash("Could not retrieve transcript for the video. Make sure the video has captions available.")
            return redirect(url_for("index"))
        cards = get_anki_cards(transcript)
        if not cards:
            flash("Failed to generate Anki cards from the transcript.")
            return redirect(url_for("index"))
        # Pass the cards (as JSON) into the template that holds the interactive review interface.
        cards_json = json.dumps(cards)
        return render_template("anki.html", cards_json=cards_json)
    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True)
