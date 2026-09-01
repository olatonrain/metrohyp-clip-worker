import os, json, uuid, subprocess, tempfile, shutil, sqlite3, time, re, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Clip Worker")
DATA_DIR = Path("/data/clips")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(str(DATA_DIR / "jobs.db"), check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, video_url TEXT, status TEXT, result TEXT, created_at TEXT)")
DB.commit()

COOKIES_PATH = os.environ.get("YT_COOKIES_FILE", "/data/cookies.txt")

class JobRequest(BaseModel):
    video_url: str
    max_clips: int = 10

def ydl_cmd(video_url: str, out_pattern: str):
    cmd = ["yt-dlp", "-f", "best[height<=1080]/best", "--no-playlist", "-o", out_pattern]
    if Path(COOKIES_PATH).is_file():
        cmd += ["--cookies", COOKIES_PATH]
    cmd += [video_url]
    return cmd

def process_job(job_id: str, video_url: str, max_clips: int):
    update_status(job_id, "downloading")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            print(f"Downloading {video_url}...")
            subprocess.run(ydl_cmd(video_url, f"{tmp}/video.%(ext)s"), check=True, capture_output=True, timeout=1800)
            video = next(tmp.glob("video.*"))
            print(f"Downloaded {video} ({video.stat().st_size / 1024 / 1024:.0f}MB)")
            update_status(job_id, "transcribing")
            import whisper
            model = whisper.load_model("base")
            result = model.transcribe(str(video), word_timestamps=True)
            segments = result.get("segments", [])
            print(f"Transcribed {len(segments)} segments")
            update_status(job_id, "scoring")
            clips = auto_score(segments, max_clips)
            update_status(job_id, "rendering")
            rendered = []
            for i, clip in enumerate(clips):
                out = DATA_DIR / f"{job_id}_{i}.mp4"
                output = cut_clip(str(video), clip["start"], clip["end"], str(out))
                if output:
                    rendered.append({
                        "rank": i + 1, "start": clip["start"], "end": clip["end"],
                        "score": clip["score"], "title": clip.get("title", ""),
                        "hashtags": clip.get("hashtags", ""),
                        "file_url": f"/files/{job_id}_{i}.mp4",
                        "duration": round(clip["end"] - clip["start"], 1)
                    })
            update_result(job_id, {"status": "done", "clips": rendered, "total": len(rendered)})
    except Exception as e:
        update_result(job_id, {"status": "failed", "error": str(e)})

def update_status(job_id, status):
    DB.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    DB.commit()

def update_result(job_id, result):
    DB.execute("UPDATE jobs SET status=?, result=? WHERE id=?", (result["status"], json.dumps(result), job_id))
    DB.commit()

def auto_score(segments, max_clips):
    from groq import Groq
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    transcript = " ".join(s["text"] for s in segments)
    prompt = f"""You are a viral clip finder. Analyze this transcript and find the {max_clips} most viral-worthy moments, each 60 to 175 seconds long. Return JSON array: [{{"start":seconds,"end":seconds,"score":0-100,"title":"hook title","hashtags":"tag1 tag2"}}]. Score based on: hook strength in first 3s, emotional/curiosity spike, self-contained thought, controversy, payoff. Clips must NOT overlap. Transcript:\n\n{transcript[:15000]}"""
    try:
        resp = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role":"user","content":prompt}], temperature=0.7, response_format={"type":"json_object"})
        clips = json.loads(resp.choices[0].message.content).get("clips", [])
        return sorted(clips, key=lambda c: c["score"], reverse=True)[:max_clips]
    except Exception as e:
        print(f"Groq scoring error: {e}")
        return fallback_clips(segments, max_clips)

def fallback_clips(segments, max_clips):
    # Group contiguous segments into 60-175s windows (whisper segments are only ~2-10s each)
    clips, cur_start, cur_end = [], None, None
    for s in segments:
        if cur_start is None:
            cur_start, cur_end = s["start"], s["end"]
        elif s["end"] - cur_start > 175:
            if cur_end - cur_start >= 60:
                clips.append({"start": cur_start, "end": cur_end, "score": 50, "title": "Clip", "hashtags": "viral shorts"})
            cur_start, cur_end = s["start"], s["end"]
        else:
            cur_end = s["end"]
            if cur_end - cur_start >= 60:
                clips.append({"start": cur_start, "end": cur_end, "score": 50, "title": "Clip", "hashtags": "viral shorts"})
                cur_start, cur_end = None, None
        if len(clips) >= max_clips:
            break
    if cur_start is not None and len(clips) < max_clips and cur_end - cur_start >= 60:
        clips.append({"start": cur_start, "end": cur_end, "score": 50, "title": "Clip", "hashtags": "viral shorts"})
    return clips[:max_clips]

def cut_clip(video_path, start, end, output_path):
    duration = end - start
    if duration < 30 or duration > 180:
        return None
    try:
        subprocess.run([
            "ffmpeg", "-i", video_path, "-ss", str(start), "-t", str(duration),
            "-vf", "crop=ih*9/16:ih,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", "-b:a", "128k",
            "-y", str(output_path)
        ], check=True, capture_output=True, timeout=600)
        return str(output_path)
    except Exception as e:
        print(f"ffmpeg error: {e}")
        return None

@app.post("/jobs")
def create_job(req: JobRequest):
    job_id = str(uuid.uuid4())[:8]
    DB.execute("INSERT INTO jobs (id, video_url, status, result, created_at) VALUES (?,?,?,?,?)",
               (job_id, req.video_url, "queued", "{}", time.strftime("%Y-%m-%dT%H:%M:%SZ")))
    DB.commit()
    threading.Thread(target=process_job, args=(job_id, req.video_url, req.max_clips), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}

@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    row = DB.execute("SELECT id, status, result FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row: raise HTTPException(404)
    return {"job_id": row[0], "status": row[1], **json.loads(row[2] or "{}")}

@app.get("/files/{name}")
def get_file(name: str):
    path = DATA_DIR / name
    if not path.exists() or not path.is_file(): raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(str(path), media_type="video/mp4", filename=name)

@app.get("/health")
def health():
    return {"status": "ok", "groq_configured": bool(os.environ.get("GROQ_API_KEY"))}
