from flask import Flask, render_template, request, jsonify
import subprocess, json, tempfile, os, sys
from career_timeline_full import timeline_from_args  # refactor the script into a function

app = Flask(__name__)

@app.route("/", methods=["GET"])
def form():
    return render_template("index.html")

@app.route("/timeline", methods=["POST"])
def timeline():
    data = request.get_json()
    # call your function to get a pandas DataFrame
    df = timeline_from_args(**data)
    return df.to_json(orient="records")

if __name__ == "__main__":
    app.run(debug=True)
