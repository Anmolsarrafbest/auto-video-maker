"""Run the finite cloud pilot, keeping its non-secret upload receipts in Git."""

import json
import os
import subprocess

import shorts

PLAN = ("coral-animals", "saturn-rings", "rainbow-split", "moon-distance", "mars-sunset")
PROGRESS = shorts.ROOT / "pilot-progress.json"
CHANNEL_ID = "UC4NykRph22My_R4us6PIAsg"
AUDIENCE = "public"


def checkpoint(receipts, message):
    shorts.save_json(PROGRESS, receipts)
    subprocess.run(["git", "add", "--", PROGRESS.name], cwd=shorts.ROOT, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=shorts.ROOT, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=shorts.ROOT, check=True)


def run():
    receipts = shorts.read_json(PROGRESS) if PROGRESS.is_file() else {}
    if any(not entry.get("video_id") for entry in receipts.values()):
        raise SystemExit("A previous upload needs reconciliation in YouTube Studio before this pilot can continue.")
    pending = [topic_id for topic_id in PLAN if topic_id not in receipts]
    date = shorts.today("Asia/Kolkata")
    remaining = 2 - sum(entry["date"] == date for entry in receipts.values())
    if not pending or remaining <= 0:
        print(f"{len(receipts)}/{len(PLAN)} pilot Shorts uploaded.")
        return

    raw_token = os.environ.pop("YOUTUBE_TOKEN_JSON", "")
    if not raw_token:
        raise SystemExit("The repository needs its YOUTUBE_TOKEN_JSON Actions secret.")
    token = json.loads(raw_token)
    if token.get("channel", {}).get("id") != CHANNEL_ID:
        raise SystemExit("The OAuth secret is for a different YouTube channel.")
    shorts.save_json(shorts.TOKEN, token)
    shorts.OUTPUT.mkdir(exist_ok=True)
    shorts.STATE.mkdir(exist_ok=True)
    shorts.setup_voice()
    topics = {topic["id"]: topic for topic in shorts.read_json(shorts.ROOT / "topics.json")}
    trends = {region: shorts.fetch_trends(region) for region in ("IN", "US")}
    batch = shorts.new_output_batch()
    for topic_id in pending[:remaining]:
        job = batch / topic_id
        shorts.render_job(topics[topic_id], "Cloud pilot", 15, job, trends=trends)
        receipts[topic_id] = {"date": date}
        checkpoint(receipts, f"Reserve pilot upload for {topic_id}")
        shorts.publish(job, AUDIENCE, date, 2)
        receipts[topic_id]["video_id"] = shorts.read_json(job / "upload.json")["video_id"]
        checkpoint(receipts, f"Record pilot upload for {topic_id}")
    print(f"{len(receipts)}/{len(PLAN)} pilot Shorts uploaded.")


if __name__ == "__main__":
    run()
