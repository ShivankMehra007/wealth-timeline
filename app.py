from flask import Flask, render_template, request, jsonify
from career_timeline_full import timeline_from_args

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/timeline", methods=["POST"])
def timeline():
    data = request.get_json()
    # timeline_from_args returns list[dict] already
    out = timeline_from_args(**data)
    return jsonify(out)

if __name__ == "__main__":
    app.run(debug=True)
